use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};

use crossbeam_channel::{Receiver, Sender};
use tokio::net::UdpSocket;
use tokio::runtime::Runtime;
use tokio_util::sync::CancellationToken;
use tracing::{debug, error, info, warn};

use beep_detector::{BeepDetector, BeepDetectorConfig};
use rsip::message::HasHeaders;
use rsip::prelude::HeadersExt;
use rsipstack::dialog::authenticate::Credential;
use rsipstack::dialog::dialog::{DialogState, DialogStateReceiver, DialogStateSender};
use rsipstack::dialog::dialog_layer::DialogLayer;
use rsipstack::dialog::invitation::InviteOption;
use rsipstack::dialog::registration::Registration;
use rsipstack::transaction::endpoint::EndpointInnerRef;
use rsipstack::transport::udp::UdpConnection;
use rsipstack::transport::SipAddr;
use rsipstack::transport::TransportLayer;
use rsipstack::EndpointBuilder;

use crate::audio::AudioFrame;
use crate::call::{CallDirection, CallSession, CallState};
use crate::config::{Codec, EndpointConfig};
use crate::error::{EndpointError, Result};
use crate::events::EndpointEvent;
use crate::rtp_transport::RtpTransport;
use crate::sdp;

/// Per-call context.
struct CallContext {
    session: CallSession,
    rtp: Option<Arc<RtpTransport>>,
    outgoing_tx: Sender<Vec<i16>>,
    incoming_rx: Receiver<AudioFrame>,
    muted: Arc<AtomicBool>,
    paused: Arc<AtomicBool>,
    flush_flag: Arc<AtomicBool>,
    playout_notify: Arc<(Mutex<bool>, Condvar)>,
    beep_detector: Arc<Mutex<Option<BeepDetector>>>,
    recorder: Option<WavRecorder>,
    cancel: CancellationToken,
    // rsipstack dialog for SIP signaling
    client_dialog: Option<rsipstack::dialog::client_dialog::ClientInviteDialog>,
    server_dialog: Option<rsipstack::dialog::server_dialog::ServerInviteDialog>,
}

/// WAV file recorder.
struct WavRecorder {
    file: std::fs::File,
    sample_count: u32,
    path: String,
}

impl WavRecorder {
    fn new(path: &str) -> std::io::Result<Self> {
        let mut file = std::fs::File::create(path)?;
        // Write placeholder WAV header (44 bytes), will be finalized on close
        use std::io::Write;
        let header = [0u8; 44];
        file.write_all(&header)?;
        Ok(Self {
            file,
            sample_count: 0,
            path: path.to_string(),
        })
    }

    fn write_samples(&mut self, samples: &[i16]) {
        use std::io::Write;
        for &s in samples {
            let _ = self.file.write_all(&s.to_le_bytes());
        }
        self.sample_count += samples.len() as u32;
    }

    fn finalize(&mut self) {
        use std::io::{Seek, SeekFrom, Write};
        let data_size = self.sample_count * 2;
        let file_size = 36 + data_size;
        let sample_rate = 16000u32;
        let byte_rate = sample_rate * 2;

        let _ = self.file.seek(SeekFrom::Start(0));
        let _ = self.file.write_all(b"RIFF");
        let _ = self.file.write_all(&file_size.to_le_bytes());
        let _ = self.file.write_all(b"WAVE");
        let _ = self.file.write_all(b"fmt ");
        let _ = self.file.write_all(&16u32.to_le_bytes()); // chunk size
        let _ = self.file.write_all(&1u16.to_le_bytes()); // PCM
        let _ = self.file.write_all(&1u16.to_le_bytes()); // mono
        let _ = self.file.write_all(&sample_rate.to_le_bytes());
        let _ = self.file.write_all(&byte_rate.to_le_bytes());
        let _ = self.file.write_all(&2u16.to_le_bytes()); // block align
        let _ = self.file.write_all(&16u16.to_le_bytes()); // bits per sample
        let _ = self.file.write_all(b"data");
        let _ = self.file.write_all(&data_size.to_le_bytes());
    }
}

/// Shared endpoint state.
struct EndpointState {
    registered: bool,
    calls: HashMap<i32, CallContext>,
    next_call_id: i32,
    endpoint_inner: Option<EndpointInnerRef>,
    dialog_layer: Option<Arc<DialogLayer>>,
    credential: Option<Credential>,
    contact_uri: Option<rsip::Uri>,
    local_addr: Option<SipAddr>,
    public_addr: Option<SocketAddr>,
}

/// The SIP endpoint — pure Rust, no C dependencies.
pub struct SipEndpoint {
    config: EndpointConfig,
    runtime: Runtime,
    state: Arc<Mutex<EndpointState>>,
    event_tx: Sender<EndpointEvent>,
    event_rx: Receiver<EndpointEvent>,
    cancel: CancellationToken,
}

impl SipEndpoint {
    /// Create and initialize a new SIP endpoint.
    pub fn new(config: EndpointConfig) -> Result<Self> {
        let runtime = Runtime::new()
            .map_err(|e| EndpointError::Other(format!("tokio runtime: {}", e)))?;

        let (event_tx, event_rx) = crossbeam_channel::unbounded();
        let cancel = CancellationToken::new();

        let state = Arc::new(Mutex::new(EndpointState {
            registered: false,
            calls: HashMap::new(),
            next_call_id: 0,
            endpoint_inner: None,
            dialog_layer: None,
            credential: None,
            contact_uri: None,
            local_addr: None,
            public_addr: None,
        }));

        // Initialize rsipstack endpoint and transport
        let state_clone = state.clone();
        let cancel_clone = cancel.clone();
        let event_tx_clone = event_tx.clone();
        let local_port = config.local_port;
        let user_agent = config.user_agent.clone();

        runtime.block_on(async {
            let bind_addr: SocketAddr = format!("0.0.0.0:{}", local_port).parse().unwrap();

            let udp_conn = UdpConnection::create_connection(
                bind_addr,
                None,
                Some(cancel_clone.clone()),
            )
            .await
            .map_err(|e| EndpointError::Other(format!("UDP transport: {}", e)))?;

            let local_addr = udp_conn.get_addr().clone();

            let transport_layer = TransportLayer::new(cancel_clone.clone());
            transport_layer.add_transport(udp_conn.into());

            let mut builder = EndpointBuilder::new();
            builder
                .with_cancel_token(cancel_clone.clone())
                .with_transport_layer(transport_layer)
                .with_user_agent(&user_agent);

            let endpoint = builder.build();
            let endpoint_inner = endpoint.inner.clone();
            let dialog_layer = Arc::new(DialogLayer::new(endpoint_inner.clone()));

            // Get incoming transactions receiver BEFORE moving endpoint into serve()
            let incoming_rx = endpoint.incoming_transactions()
                .map_err(|e| EndpointError::Other(format!("incoming transactions: {}", e)))?;

            // Spawn the endpoint serve loop
            let endpoint_cancel = cancel_clone.clone();
            tokio::spawn(async move {
                tokio::select! {
                    _ = endpoint.serve() => {}
                    _ = endpoint_cancel.cancelled() => {}
                }
            });

            // Spawn incoming transaction handler
            let dl = dialog_layer.clone();
            let evt_tx = event_tx_clone.clone();
            let st = state_clone.clone();
            {
                let mut rx = incoming_rx;
                tokio::spawn(async move {
                    while let Some(tx) = rx.recv().await {
                        let method = tx.original.method.clone();
                        match method {
                            rsip::Method::Invite => {
                                handle_incoming_invite(&dl, &st, &evt_tx, tx).await;
                            }
                            _ => {
                                debug!("Ignoring incoming {} transaction", method);
                            }
                        }
                    }
                });
            }

            let mut s = state_clone.lock().unwrap();
            s.endpoint_inner = Some(endpoint_inner);
            s.dialog_layer = Some(dialog_layer);
            s.local_addr = Some(local_addr);

            Ok::<_, EndpointError>(())
        })?;

        info!("Agent transport initialized (pure Rust)");
        Ok(Self {
            config,
            runtime,
            state,
            event_tx,
            event_rx,
            cancel,
        })
    }

    /// Register with the SIP server using digest authentication.
    pub fn register(&self, username: &str, password: &str) -> Result<()> {
        let sip_server = self.config.sip_server.clone();
        let sip_port = self.config.sip_port;
        let expires = self.config.register_expires;
        let stun_server = self.config.stun_server.clone();
        let username = username.to_string();
        let password = password.to_string();
        let state = self.state.clone();
        let event_tx = self.event_tx.clone();
        let cancel = self.cancel.clone();

        self.runtime.block_on(async {
            let credential = Credential {
                username: username.clone(),
                password: password.clone(),
                realm: None,
            };

            let endpoint_inner = {
                let s = state.lock().unwrap();
                s.endpoint_inner.clone()
                    .ok_or(EndpointError::NotInitialized)?
            };

            let local_addr = {
                let s = state.lock().unwrap();
                s.local_addr.clone()
                    .ok_or(EndpointError::NotInitialized)?
            };

            // Discover public IP via STUN
            let public_addr = match sdp::stun_binding(&stun_server) {
                Ok(addr) => {
                    info!("Public address discovered via STUN: {}", addr);
                    Some(addr)
                }
                Err(e) => {
                    warn!("STUN binding failed ({}), using local address", e);
                    None
                }
            };

            // Build contact URI using public or local address
            let local_host = local_addr.addr.host.to_string();
            let local_port = local_addr.addr.port.map(|p| u16::from(p)).unwrap_or(5060);
            let (contact_host, contact_port) = match public_addr {
                Some(a) => (a.ip().to_string(), a.port()),
                None => (local_host.clone(), local_port),
            };

            let contact_uri: rsip::Uri = format!(
                "sip:{}@{}:{}",
                username, contact_host, contact_port,
            ).try_into().map_err(|e| EndpointError::Other(format!("contact URI: {:?}", e)))?;

            let contact_hp: rsip::HostWithPort = format!("{}:{}", contact_host, contact_port)
                .try_into()
                .map_err(|e| EndpointError::Other(format!("contact host: {:?}", e)))?;

            let contact = Registration::create_nat_aware_contact(
                &username,
                Some(contact_hp),
                &local_addr,
            );

            let mut registration = Registration::new(
                endpoint_inner,
                Some(credential.clone()),
            );
            registration.contact = Some(contact);

            let server_uri: rsip::Uri = format!("sip:{}", sip_server)
                .try_into()
                .map_err(|e| EndpointError::Other(format!("server URI: {:?}", e)))?;

            let resp = registration
                .register(server_uri.clone(), Some(expires))
                .await
                .map_err(|e| EndpointError::Other(format!("registration: {}", e)))?;

            if resp.status_code == rsip::StatusCode::OK {
                info!("Registered as {}@{}", username, sip_server);
                let mut s = state.lock().unwrap();
                s.registered = true;
                s.credential = Some(credential);
                s.contact_uri = Some(contact_uri);
                s.public_addr = public_addr;
                let _ = event_tx.try_send(EndpointEvent::Registered);

                // Spawn re-registration loop
                let reg_expires = registration.expires().max(50) as u64;
                let st = state.clone();
                let evt = event_tx.clone();
                tokio::spawn(async move {
                    loop {
                        tokio::select! {
                            _ = cancel.cancelled() => break,
                            _ = tokio::time::sleep(std::time::Duration::from_secs(reg_expires)) => {}
                        }
                        match registration.register(server_uri.clone(), Some(expires)).await {
                            Ok(r) if r.status_code == rsip::StatusCode::OK => {
                                debug!("Re-registered successfully");
                            }
                            Ok(r) => {
                                warn!("Re-registration failed: {}", r.status_code);
                                st.lock().unwrap().registered = false;
                                let _ = evt.try_send(EndpointEvent::RegistrationFailed {
                                    error: format!("SIP {}", r.status_code),
                                });
                            }
                            Err(e) => {
                                warn!("Re-registration error: {}", e);
                                st.lock().unwrap().registered = false;
                                let _ = evt.try_send(EndpointEvent::RegistrationFailed {
                                    error: e.to_string(),
                                });
                            }
                        }
                    }
                });

                Ok(())
            } else {
                let err = format!("SIP {}", resp.status_code);
                let _ = event_tx.try_send(EndpointEvent::RegistrationFailed {
                    error: err.clone(),
                });
                Err(EndpointError::Sip {
                    code: u16::from(resp.status_code) as i32,
                    message: err,
                })
            }
        })
    }

    /// Unregister from the SIP server.
    pub fn unregister(&self) -> Result<()> {
        let mut s = self.state.lock().unwrap();
        s.registered = false;
        s.credential = None;
        let _ = self.event_tx.try_send(EndpointEvent::Unregistered);
        Ok(())
    }

    /// Check if currently registered.
    pub fn is_registered(&self) -> bool {
        self.state.lock().unwrap().registered
    }

    /// Make an outbound call. Returns the call ID.
    pub fn call(
        &self,
        dest_uri: &str,
        headers: Option<HashMap<String, String>>,
    ) -> Result<i32> {
        let dest_uri = dest_uri.to_string();
        let config = self.config.clone();
        let state = self.state.clone();
        let event_tx = self.event_tx.clone();
        let cancel = self.cancel.clone();

        self.runtime.block_on(async {
            let (dialog_layer, credential, contact_uri, local_addr, public_addr) = {
                let s = state.lock().unwrap();
                let dl = s.dialog_layer.clone()
                    .ok_or(EndpointError::NotInitialized)?;
                let cred = s.credential.clone()
                    .ok_or(EndpointError::NotRegistered)?;
                let contact = s.contact_uri.clone()
                    .ok_or(EndpointError::NotRegistered)?;
                let la = s.local_addr.clone()
                    .ok_or(EndpointError::NotInitialized)?;
                let pa = s.public_addr;
                (dl, cred, contact, la, pa)
            };

            // Bind RTP socket
            let rtp_socket = UdpSocket::bind("0.0.0.0:0").await
                .map_err(|e| EndpointError::Other(format!("RTP bind: {}", e)))?;
            let rtp_local = rtp_socket.local_addr()
                .map_err(|e| EndpointError::Other(format!("RTP local addr: {}", e)))?;

            // Use public IP for SDP if available
            let sdp_ip = public_addr
                .map(|a| a.ip())
                .unwrap_or_else(|| local_addr.addr.host.to_string().parse().unwrap_or(std::net::Ipv4Addr::UNSPECIFIED.into()));
            let sdp_port = rtp_local.port();

            let sdp_offer = sdp::build_offer(sdp_ip, sdp_port, &config.codecs);
            debug!("SDP offer:\n{}", sdp_offer);

            // Build custom headers
            let custom_headers = headers.map(|h| {
                h.into_iter()
                    .map(|(k, v)| {
                        rsip::Header::Other(k, v)
                    })
                    .collect::<Vec<_>>()
            });

            let caller_uri: rsip::Uri = contact_uri.clone();
            let callee_uri: rsip::Uri = dest_uri.clone().try_into()
                .map_err(|e| EndpointError::Other(format!("dest URI: {:?}", e)))?;

            let invite_option = InviteOption {
                caller: caller_uri,
                callee: callee_uri,
                contact: contact_uri,
                credential: Some(credential),
                offer: Some(sdp_offer.into_bytes()),
                content_type: Some("application/sdp".into()),
                headers: custom_headers,
                ..Default::default()
            };

            let (ds, dr) = dialog_layer.new_dialog_state_channel();
            let (dialog, resp) = dialog_layer
                .do_invite(invite_option, ds)
                .await
                .map_err(|e| EndpointError::Other(format!("INVITE: {}", e)))?;

            // Allocate call ID
            let call_id = {
                let mut s = state.lock().unwrap();
                let id = s.next_call_id;
                s.next_call_id += 1;
                id
            };

            let mut session = CallSession::new(call_id, CallDirection::Outbound);
            session.remote_uri = dest_uri.clone();

            // Emit initial state
            let _ = event_tx.try_send(EndpointEvent::CallStateChanged {
                session: session.clone(),
            });

            // Process response
            let resp = resp.ok_or_else(|| EndpointError::Other("no INVITE response".into()))?;

            let status_code = resp.status_code.clone();
            if status_code != rsip::StatusCode::OK {
                let code_str = format!("{}", status_code);
                session.state = CallState::Failed(format!("SIP {}", code_str));
                let _ = event_tx.try_send(EndpointEvent::CallTerminated {
                    session,
                    reason: format!("SIP {}", code_str),
                });
                return Err(EndpointError::Sip {
                    code: u16::from(status_code) as i32,
                    message: format!("Call rejected"),
                });
            }

            // Parse SDP answer
            let sdp_answer = sdp::parse_answer(resp.body(), &config.codecs)?;
            let remote_rtp = SocketAddr::new(sdp_answer.remote_ip, sdp_answer.remote_port);

            info!("Call {} connected, RTP: {} -> {}", call_id, rtp_local, remote_rtp);

            // Extract X-* headers from response
            extract_x_headers_from_response(&resp, &mut session);

            session.state = CallState::Confirmed;

            // Create RTP transport and audio channels
            let call_cancel = CancellationToken::new();
            let rtp = Arc::new(RtpTransport::new(
                Arc::new(rtp_socket),
                remote_rtp,
                sdp_answer.codec,
                call_cancel.clone(),
            ));

            let (outgoing_tx, outgoing_rx) = crossbeam_channel::bounded(50);
            let (incoming_tx, incoming_rx) = crossbeam_channel::bounded(50);
            let muted = Arc::new(AtomicBool::new(false));
            let paused = Arc::new(AtomicBool::new(false));
            let flush_flag = Arc::new(AtomicBool::new(false));
            let playout_notify = Arc::new((Mutex::new(false), Condvar::new()));
            let beep_detector: Arc<Mutex<Option<BeepDetector>>> = Arc::new(Mutex::new(None));

            // Start RTP loops
            rtp.start_send_loop(
                outgoing_rx,
                muted.clone(),
                paused.clone(),
                flush_flag.clone(),
                playout_notify.clone(),
            );
            rtp.start_recv_loop(
                incoming_tx,
                event_tx.clone(),
                call_id,
                beep_detector.clone(),
            );

            let _ = event_tx.try_send(EndpointEvent::CallMediaActive { call_id });

            // Spawn dialog state watcher
            let evt = event_tx.clone();
            let st = state.clone();
            let cc = call_cancel.clone();
            tokio::spawn(async move {
                let mut dr = dr;
                while let Some(ds) = dr.recv().await {
                    match ds {
                        DialogState::Terminated(_, reason) => {
                            debug!("Call {} terminated: {:?}", call_id, reason);
                            let session = {
                                let mut s = st.lock().unwrap();
                                s.calls.remove(&call_id)
                                    .map(|c| c.session)
                            };
                            if let Some(session) = session {
                                let _ = evt.try_send(EndpointEvent::CallTerminated {
                                    session,
                                    reason: format!("{:?}", reason),
                                });
                            }
                            cc.cancel();
                            break;
                        }
                        _ => {}
                    }
                }
            });

            // Store call context
            let ctx = CallContext {
                session: session.clone(),
                rtp: Some(rtp),
                outgoing_tx,
                incoming_rx,
                muted,
                paused,
                flush_flag,
                playout_notify,
                beep_detector,
                recorder: None,
                cancel: call_cancel,
                client_dialog: Some(dialog),
                server_dialog: None,
            };

            state.lock().unwrap().calls.insert(call_id, ctx);
            let _ = event_tx.try_send(EndpointEvent::CallStateChanged {
                session,
            });

            Ok(call_id)
        })
    }

    /// Answer an incoming call.
    pub fn answer(&self, call_id: i32, code: u16) -> Result<()> {
        let state = self.state.clone();
        let config = self.config.clone();
        let event_tx = self.event_tx.clone();

        self.runtime.block_on(async {
            // Extract values before mutable borrow
            let (public_addr, local_addr_str) = {
                let s = state.lock().unwrap();
                let pa = s.public_addr;
                let la = s.local_addr.as_ref()
                    .map(|a| a.addr.host.to_string())
                    .unwrap_or_else(|| "0.0.0.0".into());
                (pa, la)
            };

            let mut s = state.lock().unwrap();
            let ctx = s.calls.get_mut(&call_id)
                .ok_or(EndpointError::CallNotActive(call_id))?;

            if let Some(ref dialog) = ctx.server_dialog {
                if code >= 200 && code < 300 {
                    let rtp_socket = UdpSocket::bind("0.0.0.0:0").await
                        .map_err(|e| EndpointError::Other(format!("RTP bind: {}", e)))?;
                    let rtp_local = rtp_socket.local_addr()
                        .map_err(|e| EndpointError::Other(format!("RTP addr: {}", e)))?;

                    let sdp_ip = public_addr
                        .map(|a| a.ip())
                        .unwrap_or_else(|| local_addr_str.parse().unwrap_or(std::net::Ipv4Addr::UNSPECIFIED.into()));

                    let sdp_answer = sdp::build_offer(sdp_ip, rtp_local.port(), &config.codecs);

                    dialog.accept(None, Some(sdp_answer.into_bytes()))
                        .map_err(|e| EndpointError::Other(format!("accept: {}", e)))?;

                    ctx.session.state = CallState::Confirmed;
                    let _ = event_tx.try_send(EndpointEvent::CallMediaActive { call_id });

                    info!("Call {} answered with {}", call_id, code);
                } else if code >= 100 && code < 200 {
                    dialog.ringing(None, None)
                        .map_err(|e| EndpointError::Other(format!("ringing: {}", e)))?;
                }
            }
            Ok(())
        })
    }

    /// Reject an incoming call.
    pub fn reject(&self, call_id: i32, code: u16) -> Result<()> {
        let mut s = self.state.lock().unwrap();
        let ctx = s.calls.get_mut(&call_id)
            .ok_or(EndpointError::CallNotActive(call_id))?;

        if let Some(ref dialog) = ctx.server_dialog {
            let status = rsip::StatusCode::from(code);
            dialog.reject(Some(status), None)
                .map_err(|e| EndpointError::Other(format!("reject: {}", e)))?;
        }

        let session = s.calls.remove(&call_id).map(|c| c.session);
        if let Some(session) = session {
            let _ = self.event_tx.try_send(EndpointEvent::CallTerminated {
                session,
                reason: format!("Rejected with {}", code),
            });
        }
        Ok(())
    }

    /// Hang up an active call.
    pub fn hangup(&self, call_id: i32) -> Result<()> {
        let state = self.state.clone();
        let event_tx = self.event_tx.clone();

        self.runtime.block_on(async {
            let ctx = {
                let mut s = state.lock().unwrap();
                s.calls.remove(&call_id)
            };

            if let Some(ctx) = ctx {
                ctx.cancel.cancel();

                if let Some(ref d) = ctx.client_dialog {
                    let _ = d.hangup().await;
                } else if let Some(ref d) = ctx.server_dialog {
                    let _ = d.bye().await;
                }

                let _ = event_tx.try_send(EndpointEvent::CallTerminated {
                    session: ctx.session,
                    reason: "local hangup".into(),
                });
                info!("Call {} hung up", call_id);
            }
            Ok(())
        })
    }

    /// Send DTMF digits (default: RFC 4733 / RFC 2833).
    pub fn send_dtmf(&self, call_id: i32, digits: &str) -> Result<()> {
        self.send_dtmf_with_method(call_id, digits, "rfc2833")
    }

    /// Send DTMF digits with explicit method selection.
    pub fn send_dtmf_with_method(&self, call_id: i32, digits: &str, method: &str) -> Result<()> {
        let state = self.state.clone();

        self.runtime.block_on(async {
            let s = state.lock().unwrap();
            let ctx = s.calls.get(&call_id)
                .ok_or(EndpointError::CallNotActive(call_id))?;

            for digit in digits.chars() {
                match method {
                    "sip_info" | "sipinfo" | "info" => {
                        // SIP INFO method
                        let body = format!("Signal={}\r\nDuration=160\r\n", digit);
                        let headers = vec![
                            rsip::Header::Other("Content-Type".into(), "application/dtmf-relay".into()),
                        ];
                        if let Some(ref d) = ctx.client_dialog {
                            let _ = d.info(Some(headers), Some(body.into_bytes())).await;
                        } else if let Some(ref d) = ctx.server_dialog {
                            let _ = d.info(Some(headers), Some(body.into_bytes())).await;
                        }
                    }
                    _ => {
                        // RFC 4733 (default)
                        if let Some(ref rtp) = ctx.rtp {
                            let _ = rtp.send_dtmf_event(digit, 1600).await; // 200ms at 8kHz
                        }
                    }
                }
            }
            info!("DTMF sent on call {} ({}): {}", call_id, method, digits);
            Ok(())
        })
    }

    /// Blind transfer via SIP REFER.
    pub fn transfer(&self, call_id: i32, dest_uri: &str) -> Result<()> {
        let dest_uri = dest_uri.to_string();
        let state = self.state.clone();

        self.runtime.block_on(async {
            let s = state.lock().unwrap();
            let ctx = s.calls.get(&call_id)
                .ok_or(EndpointError::CallNotActive(call_id))?;

            let refer_to: rsip::Uri = dest_uri.try_into()
                .map_err(|e| EndpointError::Other(format!("refer URI: {:?}", e)))?;

            if let Some(ref d) = ctx.client_dialog {
                d.refer(refer_to.clone(), None, None).await
                    .map_err(|e| EndpointError::Other(format!("REFER: {}", e)))?;
            } else if let Some(ref d) = ctx.server_dialog {
                d.refer(refer_to, None, None).await
                    .map_err(|e| EndpointError::Other(format!("REFER: {}", e)))?;
            }
            info!("Call {} transferred", call_id);
            Ok(())
        })
    }

    /// Attended transfer (not supported in pure-Rust backend).
    pub fn transfer_attended(&self, _call_id: i32, _target_call_id: i32) -> Result<()> {
        Err(EndpointError::Other(
            "attended transfer not supported with pure-Rust backend".into(),
        ))
    }

    /// Mute outgoing audio.
    pub fn mute(&self, call_id: i32) -> Result<()> {
        let s = self.state.lock().unwrap();
        let ctx = s.calls.get(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        ctx.muted.store(true, Ordering::Relaxed);
        debug!("Call {} muted", call_id);
        Ok(())
    }

    /// Unmute outgoing audio.
    pub fn unmute(&self, call_id: i32) -> Result<()> {
        let s = self.state.lock().unwrap();
        let ctx = s.calls.get(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        ctx.muted.store(false, Ordering::Relaxed);
        debug!("Call {} unmuted", call_id);
        Ok(())
    }

    /// Send an audio frame into the active call.
    pub fn send_audio(&self, call_id: i32, frame: &AudioFrame) -> Result<()> {
        let s = self.state.lock().unwrap();
        let ctx = s.calls.get(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        ctx.outgoing_tx
            .try_send(frame.data.clone())
            .map_err(|_| EndpointError::Other("audio buffer full".into()))
    }

    /// Receive the next audio frame (non-blocking).
    pub fn recv_audio(&self, call_id: i32) -> Result<Option<AudioFrame>> {
        let s = self.state.lock().unwrap();
        let ctx = s.calls.get(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        Ok(ctx.incoming_rx.try_recv().ok())
    }

    /// Mark the current playback segment as complete.
    pub fn flush(&self, call_id: i32) -> Result<()> {
        let s = self.state.lock().unwrap();
        let ctx = s.calls.get(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        if let Ok(mut done) = ctx.playout_notify.0.lock() {
            *done = false;
        }
        Ok(())
    }

    /// Clear all queued outgoing audio (barge-in).
    pub fn clear_buffer(&self, call_id: i32) -> Result<()> {
        let s = self.state.lock().unwrap();
        let ctx = s.calls.get(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        ctx.flush_flag.store(true, Ordering::Relaxed);
        debug!("Cleared audio buffer on call {}", call_id);
        Ok(())
    }

    /// Block until all queued outgoing audio has finished playing.
    pub fn wait_for_playout(&self, call_id: i32, timeout_ms: u64) -> Result<bool> {
        let s = self.state.lock().unwrap();
        let ctx = s.calls.get(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        let (lock, cvar) = &*ctx.playout_notify;
        let guard = lock.lock().unwrap();
        let result = cvar
            .wait_timeout_while(guard, std::time::Duration::from_millis(timeout_ms), |done| !*done)
            .unwrap();
        Ok(!result.1.timed_out())
    }

    /// Pause audio playback.
    pub fn pause(&self, call_id: i32) -> Result<()> {
        let s = self.state.lock().unwrap();
        let ctx = s.calls.get(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        ctx.paused.store(true, Ordering::Relaxed);
        Ok(())
    }

    /// Resume audio playback.
    pub fn resume(&self, call_id: i32) -> Result<()> {
        let s = self.state.lock().unwrap();
        let ctx = s.calls.get(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        ctx.paused.store(false, Ordering::Relaxed);
        Ok(())
    }

    /// Start recording a call to a WAV file.
    pub fn start_recording(&self, call_id: i32, path: &str) -> Result<()> {
        let mut s = self.state.lock().unwrap();
        let ctx = s.calls.get_mut(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        let recorder = WavRecorder::new(path)
            .map_err(|e| EndpointError::Other(format!("create WAV: {}", e)))?;
        ctx.recorder = Some(recorder);
        info!("Recording call {} to {}", call_id, path);
        Ok(())
    }

    /// Stop recording a call and finalize the WAV file.
    pub fn stop_recording(&self, call_id: i32) -> Result<()> {
        let mut s = self.state.lock().unwrap();
        let ctx = s.calls.get_mut(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        if let Some(mut recorder) = ctx.recorder.take() {
            recorder.finalize();
            info!("Stopped recording call {}", call_id);
            Ok(())
        } else {
            Err(EndpointError::Other("no active recording".into()))
        }
    }

    /// Start beep detection on a call.
    pub fn detect_beep(&self, call_id: i32, config: BeepDetectorConfig) -> Result<()> {
        let s = self.state.lock().unwrap();
        let ctx = s.calls.get(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        let detector = BeepDetector::new(config);
        *ctx.beep_detector.lock().unwrap() = Some(detector);
        info!("Beep detection started on call {}", call_id);
        Ok(())
    }

    /// Cancel beep detection on a call.
    pub fn cancel_beep_detection(&self, call_id: i32) -> Result<()> {
        let s = self.state.lock().unwrap();
        let ctx = s.calls.get(&call_id).ok_or(EndpointError::CallNotActive(call_id))?;
        if ctx.beep_detector.lock().unwrap().take().is_some() {
            info!("Beep detection cancelled on call {}", call_id);
            Ok(())
        } else {
            Err(EndpointError::Other("no beep detection active".into()))
        }
    }

    /// Get the event receiver channel.
    pub fn events(&self) -> Receiver<EndpointEvent> {
        self.event_rx.clone()
    }

    /// Shut down the endpoint.
    pub fn shutdown(&self) -> Result<()> {
        self.cancel.cancel();
        // Hang up all calls
        let call_ids: Vec<i32> = {
            let s = self.state.lock().unwrap();
            s.calls.keys().copied().collect()
        };
        for id in call_ids {
            let _ = self.hangup(id);
        }
        info!("Agent transport shut down");
        Ok(())
    }
}

impl Drop for SipEndpoint {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/// Handle an incoming INVITE transaction.
async fn handle_incoming_invite(
    dialog_layer: &Arc<DialogLayer>,
    state: &Arc<Mutex<EndpointState>>,
    event_tx: &Sender<EndpointEvent>,
    tx: rsipstack::transaction::transaction::Transaction,
) {
    let (ds, dr) = dialog_layer.new_dialog_state_channel();

    let credential = state.lock().unwrap().credential.clone();
    let contact_uri = state.lock().unwrap().contact_uri.clone();

    let dialog = match dialog_layer.get_or_create_server_invite(
        &tx,
        ds,
        credential,
        contact_uri,
    ) {
        Ok(d) => d,
        Err(e) => {
            error!("Failed to create server invite dialog: {}", e);
            return;
        }
    };

    // Allocate call ID
    let call_id = {
        let mut s = state.lock().unwrap();
        let id = s.next_call_id;
        s.next_call_id += 1;
        id
    };

    let mut session = CallSession::new(call_id, CallDirection::Inbound);

    // Extract remote URI and headers from the INVITE
    let request = dialog.initial_request();
    if let Ok(from) = request.from_header() {
        session.remote_uri = from.to_string();
    }

    // Extract X-* headers
    for header in request.headers().iter() {
        if let rsip::Header::Other(name, value) = header {
            if name.starts_with("X-") || name.starts_with("x-") {
                session.extra_headers.insert(name.clone(), value.clone());
            }
        }
    }

    // Set call_uuid from known headers
    if let Some(uuid) = session.extra_headers.get("X-CallUUID") {
        session.call_uuid = Some(uuid.clone());
    } else if let Some(uuid) = session.extra_headers.get("X-Plivo-CallUUID") {
        session.call_uuid = Some(uuid.clone());
    }

    info!(
        "Incoming call from {} (call_id={}, uuid={:?})",
        session.remote_uri, call_id, session.call_uuid
    );

    // Create call context with server dialog
    let call_cancel = CancellationToken::new();
    let (outgoing_tx, _outgoing_rx) = crossbeam_channel::bounded(50);
    let (_incoming_tx, incoming_rx) = crossbeam_channel::bounded(50);

    let ctx = CallContext {
        session: session.clone(),
        rtp: None,
        outgoing_tx,
        incoming_rx,
        muted: Arc::new(AtomicBool::new(false)),
        paused: Arc::new(AtomicBool::new(false)),
        flush_flag: Arc::new(AtomicBool::new(false)),
        playout_notify: Arc::new((Mutex::new(false), Condvar::new())),
        beep_detector: Arc::new(Mutex::new(None)),
        recorder: None,
        cancel: call_cancel,
        client_dialog: None,
        server_dialog: Some(dialog),
    };

    state.lock().unwrap().calls.insert(call_id, ctx);
    let _ = event_tx.try_send(EndpointEvent::IncomingCall { session });
}

/// Extract X-* headers from a SIP response.
fn extract_x_headers_from_response(
    resp: &rsip::Response,
    session: &mut CallSession,
) {
    for header in resp.headers().iter() {
        if let rsip::Header::Other(name, value) = header {
            if name.starts_with("X-") || name.starts_with("x-") {
                session.extra_headers.insert(name.clone(), value.clone());
            }
        }
    }
    // Set call_uuid from known headers
    if session.call_uuid.is_none() {
        if let Some(uuid) = session.extra_headers.get("X-CallUUID") {
            session.call_uuid = Some(uuid.clone());
        } else if let Some(uuid) = session.extra_headers.get("X-Plivo-CallUUID") {
            session.call_uuid = Some(uuid.clone());
        }
    }
}
