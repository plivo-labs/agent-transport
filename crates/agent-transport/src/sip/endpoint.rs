use std::collections::HashMap;
use std::fmt::Display;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};

use crossbeam_channel::{Receiver, Sender};
use tokio::net::UdpSocket;
use tokio::runtime::Runtime;
use tokio_util::sync::CancellationToken;
use tracing::{debug, error, info};

use beep_detector::{BeepDetector, BeepDetectorConfig};
use rsip::message::HasHeaders;
use rsip::prelude::HeadersExt;
use rsipstack::dialog::authenticate::Credential;
use rsipstack::dialog::dialog::{DialogState, DialogStateReceiver};
use rsipstack::dialog::dialog_layer::DialogLayer;
use rsipstack::dialog::invitation::InviteOption;
use rsipstack::dialog::registration::Registration;
use rsipstack::transport::tcp::TcpConnection;
use rsipstack::transport::{SipAddr, TransportLayer};
use rsipstack::EndpointBuilder;

use crate::audio::AudioFrame;
use crate::sip::call::{CallDirection, CallSession, CallState};
use crate::config::EndpointConfig;
use crate::error::{EndpointError, Result};
use crate::events::EndpointEvent;
use crate::recorder::{CallRecorder, RecordingManager};
use crate::sip::audio_buffer::AudioBuffer;
use crate::sip::rtp_transport::RtpTransport;
use crate::sip::sdp;

fn err(e: impl Display) -> EndpointError { EndpointError::Other(e.to_string()) }

// ─── Per-call context ────────────────────────────────────────────────────────

struct CallContext {
    session: CallSession,
    rtp: Option<Arc<RtpTransport>>,
    /// Shared audio buffer — agent voice with backpressure.
    audio_buf: Arc<AudioBuffer>,
    /// Background audio buffer (hold music, ambient) — mixed in send loop.
    bg_audio_buf: Arc<AudioBuffer>,
    incoming_rx: Receiver<AudioFrame>,
    muted: Arc<AtomicBool>,
    paused: Arc<AtomicBool>,
    held: Arc<AtomicBool>,
    playout_notify: Arc<(Mutex<bool>, Condvar)>,
    beep_detector: Arc<Mutex<Option<BeepDetector>>>,
    recorder: Arc<Mutex<Option<Arc<CallRecorder>>>>,
    /// Cached resampler for send_audio — resamples TTS output to pipeline rate.
    /// Stateful (speex anti-aliasing filter) — must persist across frames.
    input_resampler: Arc<Mutex<Option<crate::sip::resampler::Resampler>>>,
    cancel: CancellationToken,
    rtp_tasks: Vec<tokio::task::JoinHandle<()>>,
    client_dialog: Option<rsipstack::dialog::client_dialog::ClientInviteDialog>,
    server_dialog: Option<rsipstack::dialog::server_dialog::ServerInviteDialog>,
    local_sdp: Option<String>,
}

// ─── Shared state ────────────────────────────────────────────────────────────

struct EndpointState {
    registered: bool,
    calls: HashMap<String, CallContext>,
    dialog_layer: Option<Arc<DialogLayer>>,
    credential: Option<Credential>,
    contact_uri: Option<rsip::Uri>,
    /// Address of Record — sip:user@domain — used as From header in outbound calls.
    aor: Option<rsip::Uri>,
    local_addr: Option<SipAddr>,
    public_addr: Option<SocketAddr>,
    /// Resolved SIP server address (pinned for TCP connection reuse).
    sip_server_addr: Option<SocketAddr>,
}

// ─── Shared helpers ──────────────────────────────────────────────────────────

/// Set up RTP transport: parse remote SDP, bind socket, create RtpTransport,
/// start send/recv loops, wire channels into the CallContext.
async fn setup_rtp(
    ctx: &mut CallContext,
    remote_sdp_bytes: &[u8],
    codecs: &[crate::config::Codec],
    public_addr: Option<SocketAddr>,
    local_ip_str: &str,
    etx: &Sender<EndpointEvent>,
    call_id: &str,
    input_sample_rate: u32, output_sample_rate: u32,
) -> Result<(String, SocketAddr)> {
    let answer = sdp::parse_answer(remote_sdp_bytes, codecs)?;
    let remote_rtp = SocketAddr::new(answer.remote_ip, answer.remote_port);

    let rtp_sock = UdpSocket::bind("0.0.0.0:0").await.map_err(err)?;
    let rtp_port = rtp_sock.local_addr().unwrap().port();
    let ip = public_addr.map(|a| a.ip())
        .unwrap_or_else(|| local_ip_str.parse().unwrap_or(std::net::Ipv4Addr::UNSPECIFIED.into()));
    let local_sdp = sdp::build_answer(ip, rtp_port, codecs, &answer);

    let dtmf_pt = answer.dtmf_payload_type.unwrap_or(crate::sip::rtp_transport::DEFAULT_DTMF_PT);
    debug!("Call {} negotiated: codec={:?} dtmf_pt={} ptime={}ms remote_rtp={}", call_id, answer.codec, dtmf_pt, answer.ptime_ms, remote_rtp);
    let rtp = Arc::new(RtpTransport::new(Arc::new(rtp_sock), remote_rtp, answer.codec, ctx.cancel.clone(), dtmf_pt, answer.ptime_ms, input_sample_rate, output_sample_rate));

    let (itx, irx) = crossbeam_channel::unbounded();
    let send_handle = rtp.start_send_loop(ctx.audio_buf.clone(), ctx.bg_audio_buf.clone(), ctx.muted.clone(), ctx.paused.clone(), ctx.playout_notify.clone(), ctx.recorder.clone());
    let recv_handle = rtp.start_recv_loop(itx, etx.clone(), call_id.to_string(), ctx.session.direction, ctx.beep_detector.clone(), ctx.held.clone(), ctx.recorder.clone());
    ctx.rtp_tasks = vec![send_handle, recv_handle];

    ctx.rtp = Some(rtp);
    ctx.incoming_rx = irx;
    ctx.session.state = CallState::Confirmed;
    ctx.local_sdp = Some(local_sdp.clone());

    let _ = etx.try_send(EndpointEvent::CallMediaActive { call_id: call_id.to_string() });
    Ok((local_sdp, remote_rtp))
}

/// Watch dialog state for remote BYE and emit CallTerminated.
fn spawn_dialog_watcher(
    mut dr: DialogStateReceiver,
    call_id: String,
    st: Arc<Mutex<EndpointState>>,
    etx: Sender<EndpointEvent>,
    cc: CancellationToken,
) {
    tokio::spawn(async move {
        while let Some(ds) = dr.recv().await {
            if let DialogState::Terminated(_, reason) = ds {
                info!("Call {} terminated by remote: {:?}", call_id, reason);
                let sess = st.lock().unwrap().calls.remove(&call_id).map(|c| { c.cancel.cancel(); c.session });
                if let Some(s) = sess { let _ = etx.try_send(EndpointEvent::CallTerminated { session: s, reason: format!("{:?}", reason) }); }
                cc.cancel();
                break;
            }
        }
    });
}

/// Start session timer refresh (periodic Re-INVITE).
fn start_session_timer(
    session_expires: Option<u32>,
    call_id: String,
    st: Arc<Mutex<EndpointState>>,
    cc: CancellationToken,
) {
    let Some(secs) = session_expires else { return; };
    let refresh = (secs / 2).max(30) as u64;
    info!("Session timer: {}s (refresh every {}s)", secs, refresh);
    let handle = tokio::runtime::Handle::current();
    std::thread::spawn(move || {
        loop {
            std::thread::sleep(std::time::Duration::from_secs(refresh));
            if cc.is_cancelled() { break; }
            // Extract data and dialog refs from lock, then drop before blocking
            // on reinvite to avoid holding the mutex across an async operation.
            let reinvite_info = {
                let s = st.lock().unwrap();
                let Some(ctx) = s.calls.get(&call_id) else { break; };
                let Some(ref sdp) = ctx.local_sdp else { break; };
                let body = sdp.clone().into_bytes();
                let cd = ctx.client_dialog.clone();
                let sd = ctx.server_dialog.clone();
                (body, cd, sd)
            };
            let hdrs = vec![rsip::Header::Other("Content-Type".into(), "application/sdp".into())];
            if let Some(ref d) = reinvite_info.1 {
                let _ = handle.block_on(d.reinvite(Some(hdrs), Some(reinvite_info.0)));
            } else if let Some(ref d) = reinvite_info.2 {
                let _ = handle.block_on(d.reinvite(Some(hdrs), Some(reinvite_info.0)));
            }
            debug!("Session timer refresh for call {}", call_id);
        }
    });
}

fn new_call_context(call_id: &str, direction: CallDirection, cc: CancellationToken, output_sample_rate: u32) -> (CallContext, CallSession) {
    let session = CallSession::new(call_id.to_string(), direction);
    let (_itx, irx) = crossbeam_channel::unbounded();
    let ctx = CallContext {
        session: session.clone(), rtp: None,
        audio_buf: Arc::new(AudioBuffer::with_queue_size(200, output_sample_rate)),
        bg_audio_buf: Arc::new(AudioBuffer::with_queue_size(200, output_sample_rate)),
        incoming_rx: irx,
        muted: Arc::new(AtomicBool::new(false)), paused: Arc::new(AtomicBool::new(false)),
        held: Arc::new(AtomicBool::new(false)),
        playout_notify: Arc::new((Mutex::new(false), Condvar::new())),
        beep_detector: Arc::new(Mutex::new(None)), recorder: Arc::new(Mutex::new(None)),
        input_resampler: Arc::new(Mutex::new(None)), cancel: cc,
        rtp_tasks: Vec::new(),
        client_dialog: None, server_dialog: None, local_sdp: None,
    };
    (ctx, session)
}

fn extract_x_headers(resp: &rsip::Response, session: &mut CallSession) {
    for h in resp.headers().iter() { if let rsip::Header::Other(n, v) = h { if n.starts_with("X-") || n.starts_with("x-") { session.extra_headers.insert(n.clone(), v.clone()); } } }
    if session.call_uuid.is_none() { session.call_uuid = session.extra_headers.get("X-CallUUID").or(session.extra_headers.get("X-Plivo-CallUUID")).cloned(); }
}

fn extract_x_headers_from_request(req: &rsip::Request, session: &mut CallSession) {
    for h in req.headers().iter() { if let rsip::Header::Other(n, v) = h { if n.starts_with("X-") || n.starts_with("x-") { session.extra_headers.insert(n.clone(), v.clone()); } } }
    if session.call_uuid.is_none() { session.call_uuid = session.extra_headers.get("X-CallUUID").or(session.extra_headers.get("X-Plivo-CallUUID")).cloned(); }
}

/// Parse Session-Expires header from SIP response.
fn parse_session_expires(resp: &rsip::Response) -> Option<u32> {
    for h in resp.headers().iter() {
        if let rsip::Header::Other(n, v) = h {
            if n.eq_ignore_ascii_case("Session-Expires") {
                return v.split(';').next()?.trim().parse().ok();
            }
        }
    }
    None
}

// ─── SipEndpoint ─────────────────────────────────────────────────────────────

pub struct SipEndpoint {
    config: EndpointConfig,
    runtime: Runtime,
    state: Arc<Mutex<EndpointState>>,
    event_tx: Sender<EndpointEvent>,
    event_rx: Receiver<EndpointEvent>,
    cancel: CancellationToken,
    recording_mgr: Arc<RecordingManager>,
}

impl SipEndpoint {
    pub fn new(config: EndpointConfig) -> Result<Self> {
        if config.input_sample_rate == 0 || config.output_sample_rate == 0 { return Err(EndpointError::Other("sample_rate must be > 0".into())); }
        let rt = Runtime::new().map_err(err)?;
        let (etx, erx) = crossbeam_channel::unbounded();
        let cancel = CancellationToken::new();
        let state = Arc::new(Mutex::new(EndpointState {
            registered: false, calls: HashMap::new(),
            dialog_layer: None, credential: None, contact_uri: None, aor: None,
            local_addr: None, public_addr: None, sip_server_addr: None,
        }));

        let (st, cc, etx2, ua, isr, osr) = (state.clone(), cancel.clone(), etx.clone(), config.user_agent.clone(), config.input_sample_rate, config.output_sample_rate);
        let sip_server = config.sip_server.clone();
        let sip_port = config.sip_port;
        rt.block_on(async {
            // SIP signaling over TCP with Via alias (RFC 5923) for NAT traversal.
            // The proxy reuses the TCP connection to send INVITEs back to us.
            let remote_addr: SocketAddr = tokio::net::lookup_host(format!("{}:{}", sip_server, sip_port)).await
                .map_err(|e| err(format!("DNS resolve {}:{}: {}", sip_server, sip_port, e)))?
                .next().ok_or_else(|| err(format!("DNS returned no results for {}", sip_server)))?;
            info!("SIP server {} resolved to {} (TCP)", sip_server, remote_addr);
            let remote_sip = SipAddr::new(rsip::Transport::Tcp, format!("{}:{}", remote_addr.ip(), remote_addr.port()).try_into().map_err(|e| err(format!("{:?}", e)))?);
            let tcp = TcpConnection::connect(&remote_sip, Some(cc.clone())).await.map_err(|e| err(format!("TCP connect to {}: {}", remote_addr, e)))?;
            let la = tcp.inner.local_addr.clone();
            info!("TCP connected to {} (local: {})", remote_addr, la);

            let tl = TransportLayer::new(cc.clone());
            let tcp_conn: rsipstack::transport::SipConnection = tcp.into();
            // add_connection: stores by remote_addr for lookup (reuses existing connection).
            // Also starts serve_connection internally (read loop for incoming SIP).
            tl.add_connection(tcp_conn);

            let mut b = EndpointBuilder::new();
            let mut ep_option = rsipstack::transaction::endpoint::EndpointOption::default();
            ep_option.callid_suffix = Some("agent-transport".to_string());
            b.with_cancel_token(cc.clone()).with_transport_layer(tl).with_user_agent(&ua).with_option(ep_option);
            let ep = b.build();
            let ei = ep.inner.clone();
            let dl = Arc::new(DialogLayer::new(ei.clone()));
            let rx = ep.incoming_transactions().map_err(err)?;
            let cc2 = cc.clone();
            tokio::spawn(async move { tokio::select! { _ = ep.serve() => {}, _ = cc2.cancelled() => {} } });

            // Incoming transaction handler
            let (dl2, st2, etx3) = (dl.clone(), st.clone(), etx2.clone());
            let mut rx = rx;
            tokio::spawn(async move {
                while let Some(mut tx) = rx.recv().await {
                    if tx.original.method == rsip::Method::Invite {
                        handle_incoming(&dl2, &st2, &etx3, tx, isr, osr).await;
                    } else if let Some(dialog) = dl2.match_dialog(&tx) {
                        match dialog {
                            rsipstack::dialog::dialog::Dialog::ServerInvite(mut d) => { let _ = d.handle(&mut tx).await; }
                            rsipstack::dialog::dialog::Dialog::ClientInvite(mut d) => { let _ = d.handle(&mut tx).await; }
                            _ => {}
                        }
                    }
                }
            });

            let mut s = st.lock().unwrap();
            s.dialog_layer = Some(dl);
            s.local_addr = Some(la);
            s.sip_server_addr = Some(remote_addr);
            Ok::<_, EndpointError>(())
        })?;

        info!("Agent transport initialized");
        Ok(Self { config, runtime: rt, state, event_tx: etx, event_rx: erx, cancel, recording_mgr: RecordingManager::new() })
    }

    pub fn register(&self, username: &str, password: &str) -> Result<()> {
        let (srv, exp, stun) = (self.config.sip_server.clone(), self.config.register_expires, self.config.stun_server.clone());
        let (user, pass) = (username.to_string(), password.to_string());
        let (st, etx, cc) = (self.state.clone(), self.event_tx.clone(), self.cancel.clone());

        self.runtime.block_on(async {
            let cred = Credential { username: user.clone(), password: pass.clone(), realm: None };
            let (ei, la) = { let s = st.lock().unwrap(); (s.dialog_layer.as_ref().unwrap().endpoint.clone(), s.local_addr.clone().unwrap()) };

            // STUN for SDP/RTP (media path still uses UDP)
            let pa = sdp::stun_binding(&stun).ok();
            if let Some(a) = pa { info!("STUN: public {} (for RTP/SDP)", a); }

            // Contact with transport=tcp — proxy sends INVITEs over our TCP connection
            let (ch, cp) = pa.map(|a| (a.ip().to_string(), a.port())).unwrap_or((la.addr.host.to_string(), la.addr.port.map(u16::from).unwrap_or(5060)));
            let contact_uri: rsip::Uri = format!("sip:{}@{}:{};transport=tcp", user, ch, cp).try_into().map_err(|e| err(format!("{:?}", e)))?;
            let mut reg = Registration::new(ei, Some(cred.clone()));
            let stun_hp: rsip::HostWithPort = format!("{}:{}", ch, cp).try_into().map_err(|e| err(format!("{:?}", e)))?;
            reg.public_address = Some(stun_hp);
            // Set explicit Contact with transport=tcp so it's preserved through 401 auth retry
            reg.contact = Some(rsip::typed::Contact {
                display_name: None,
                uri: contact_uri.clone(),
                params: vec![],
            });

            // server_uri with transport=tcp so rsipstack routes via TCP
            let server_uri: rsip::Uri = format!("sip:{};transport=tcp", srv).try_into().map_err(|e| err(format!("{:?}", e)))?;
            // Pin re-registration to the resolved IP so it reuses the existing TCP connection.
            // Without this, DNS re-resolution may return a different IP, causing lookup() to
            // open a new TCP connection and breaking the Via alias mapping.
            let sip_addr = st.lock().unwrap().sip_server_addr;
            if let Some(addr) = sip_addr {
                reg.outbound_proxy = Some(addr);
            }
            let aor: rsip::Uri = format!("sip:{}@{}", user, srv).try_into().map_err(|e| err(format!("{:?}", e)))?;
            let resp = reg.register(server_uri.clone(), Some(exp)).await.map_err(err)?;

            if resp.status_code == rsip::StatusCode::OK {
                let discovered = reg.discovered_public_address();
                if let Some(ref hp) = discovered {
                    info!("Registered {}@{} (TCP, discovered: {})", user, srv, hp);
                } else {
                    info!("Registered {}@{} (TCP)", user, srv);
                }
                { let mut s = st.lock().unwrap(); s.registered = true; s.credential = Some(cred); s.contact_uri = Some(contact_uri.clone()); s.aor = Some(aor.clone()); s.public_addr = pa; }
                let _ = etx.try_send(EndpointEvent::Registered);

                // Re-registration loop (TCP keepalive is handled by the persistent connection)
                let re = reg.expires().max(50) as u64;
                let (st2, etx2) = (st.clone(), etx.clone());
                tokio::spawn(async move {
                    loop {
                        tokio::select! { _ = cc.cancelled() => break, _ = tokio::time::sleep(std::time::Duration::from_secs(re)) => {} }
                        match reg.register(server_uri.clone(), Some(exp)).await {
                            Ok(r) if r.status_code == rsip::StatusCode::OK => debug!("Re-registered"),
                            Ok(r) => { st2.lock().unwrap().registered = false; let _ = etx2.try_send(EndpointEvent::RegistrationFailed { error: format!("{}", r.status_code) }); }
                            Err(e) => { st2.lock().unwrap().registered = false; let _ = etx2.try_send(EndpointEvent::RegistrationFailed { error: e.to_string() }); }
                        }
                    }
                });

                Ok(())
            } else {
                let e = format!("SIP {}", resp.status_code);
                let _ = etx.try_send(EndpointEvent::RegistrationFailed { error: e.clone() });
                Err(EndpointError::Sip { code: u16::from(resp.status_code) as i32, message: e })
            }
        })
    }

    pub fn unregister(&self) -> Result<()> {
        let (srv, st, etx) = (self.config.sip_server.clone(), self.state.clone(), self.event_tx.clone());
        self.runtime.block_on(async {
            let (ei, cred) = {
                let s = st.lock().unwrap();
                (s.dialog_layer.as_ref().cloned(), s.credential.clone())
            };
            if let (Some(ei), Some(cred)) = (ei, cred) {
                let sip_addr = st.lock().unwrap().sip_server_addr;
                let mut reg = Registration::new(ei.endpoint.clone(), Some(cred));
                if let Some(addr) = sip_addr {
                    reg.outbound_proxy = Some(addr);
                }
                let server_uri: rsip::Uri = format!("sip:{};transport=tcp", srv)
                    .try_into().unwrap_or_default();
                // REGISTER with Expires: 0 to unregister
                match reg.register(server_uri, Some(0)).await {
                    Ok(r) => debug!("Unregistered: {}", r.status_code),
                    Err(e) => debug!("Unregister failed: {}", e),
                }
            }
        });
        let mut s = st.lock().unwrap();
        s.registered = false;
        let _ = etx.try_send(EndpointEvent::Unregistered);
        Ok(())
    }

    pub fn is_registered(&self) -> bool { self.state.lock().unwrap().registered }

    // ─── Outbound call ───────────────────────────────────────────────────────

    pub fn call(&self, dest_uri: &str, headers: Option<HashMap<String, String>>) -> Result<String> {
        self.call_with_from(dest_uri, None, headers, None)
    }

    /// Make an outbound call with an optional From URI.
    /// If `from_uri` is None, uses the AOR (sip:user@domain) from registration.
    pub fn call_with_from(&self, dest_uri: &str, from_uri: Option<&str>, headers: Option<HashMap<String, String>>, external_call_id: Option<String>) -> Result<String> {
        let (dest, cfg, st, etx) = (dest_uri.to_string(), self.config.clone(), self.state.clone(), self.event_tx.clone());
        let from_override = from_uri.map(|s| s.to_string());
        self.runtime.block_on(async {
            let (dl, cred, contact, aor, la, pa) = {
                let s = st.lock().unwrap();
                (s.dialog_layer.clone().ok_or(EndpointError::NotInitialized)?,
                 s.credential.clone().ok_or(EndpointError::NotRegistered)?,
                 s.contact_uri.clone().ok_or(EndpointError::NotRegistered)?,
                 s.aor.clone().ok_or(EndpointError::NotRegistered)?,
                 s.local_addr.clone().ok_or(EndpointError::NotInitialized)?,
                 s.public_addr)
            };
            let la_str = la.addr.host.to_string();

            // Resolve the From/caller URI — AOR (sip:user@domain), not Contact (sip:user@nat-ip:port)
            let caller: rsip::Uri = if let Some(ref from) = from_override {
                from.clone().try_into().map_err(|e| err(format!("invalid from_uri: {:?}", e)))?
            } else {
                aor
            };

            // SDP offer for the INVITE
            let rtp_sock = UdpSocket::bind("0.0.0.0:0").await.map_err(err)?;
            let rtp_port = rtp_sock.local_addr().unwrap().port();
            let sdp_ip = pa.map(|a| a.ip()).unwrap_or_else(|| la_str.parse().unwrap_or(std::net::Ipv4Addr::UNSPECIFIED.into()));
            let offer = sdp::build_offer(sdp_ip, rtp_port, &cfg.codecs);

            let custom_hdrs = headers.map(|h| h.into_iter().map(|(k, v)| rsip::Header::Other(k, v)).collect());
            // Add transport=tcp to callee URI so the INVITE routes over TCP
            let dest_tcp = if !dest.contains("transport=") {
                format!("{};transport=tcp", dest)
            } else { dest.clone() };
            let callee: rsip::Uri = dest_tcp.try_into().map_err(|e| err(format!("{:?}", e)))?;
            let opt = InviteOption { caller, callee, contact, credential: Some(cred), offer: Some(offer.into_bytes()), content_type: Some("application/sdp".into()), headers: custom_hdrs, ..Default::default() };

            let (ds, dr) = dl.new_dialog_state_channel();
            let (dialog, resp) = dl.do_invite(opt, ds).await.map_err(err)?;
            let call_id = external_call_id.unwrap_or_else(|| format!("c{:016x}", rand::random::<u64>()));

            let resp = resp.ok_or_else(|| EndpointError::Other("no response".into()))?;
            let sc = resp.status_code.clone();
            if sc != rsip::StatusCode::OK {
                let e = format!("SIP {}", sc);
                let _ = etx.try_send(EndpointEvent::CallTerminated { session: CallSession::new(call_id, CallDirection::Outbound), reason: e.clone() });
                return Err(EndpointError::Sip { code: u16::from(sc) as i32, message: e });
            }

            let cc = CancellationToken::new();
            let (mut ctx, mut session) = new_call_context(&call_id, CallDirection::Outbound, cc.clone(), cfg.output_sample_rate);
            session.remote_uri = dest;
            extract_x_headers(&resp, &mut session);
            ctx.session = session.clone();
            ctx.client_dialog = Some(dialog);

            // Set up RTP (shared with inbound)
            let (_, remote_rtp) = setup_rtp(&mut ctx, resp.body(), &cfg.codecs, pa, &la_str, &etx, &call_id, cfg.input_sample_rate, cfg.output_sample_rate).await?;

            // Watch for remote BYE
            spawn_dialog_watcher(dr, call_id.clone(), st.clone(), etx.clone(), cc.clone());

            // Session timer
            let session_expires = parse_session_expires(&resp);

            st.lock().unwrap().calls.insert(call_id.clone(), ctx);
            let _ = etx.try_send(EndpointEvent::CallStateChanged { session });
            info!("Call {} connected to {}", call_id, remote_rtp);

            start_session_timer(session_expires, call_id.clone(), st.clone(), cc);
            Ok(call_id)
        })
    }

    // ─── Inbound answer ──────────────────────────────────────────────────────

    pub fn answer(&self, call_id: &str, code: u16) -> Result<()> {
        let (cfg, st, etx) = (self.config.clone(), self.state.clone(), self.event_tx.clone());
        self.runtime.block_on(async {
            let (pa, la_str) = { let s = st.lock().unwrap(); (s.public_addr, s.local_addr.as_ref().map(|a| a.addr.host.to_string()).unwrap_or("0.0.0.0".into())) };

            let cancel = {
                let mut s = st.lock().unwrap();
                let ctx = s.calls.get_mut(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;

                if ctx.server_dialog.is_none() {
                    error!("answer: no server_dialog for call {}", call_id);
                    None
                } else if code >= 100 && code < 200 {
                    ctx.server_dialog.as_ref().unwrap().ringing(None, None).map_err(err)?;
                    None
                } else if code >= 200 && code < 300 {
                    let remote_sdp = ctx.server_dialog.as_ref().unwrap().initial_request().body().to_vec();
                    // Note: setup_rtp awaits UdpSocket::bind (microseconds). Lock held briefly.
                    let (local_sdp, remote_rtp) = setup_rtp(ctx, &remote_sdp, &cfg.codecs, pa, &la_str, &etx, &call_id, cfg.input_sample_rate, cfg.output_sample_rate).await?;
                    let hdrs = vec![rsip::Header::Other("Content-Type".into(), "application/sdp".into())];
                    ctx.server_dialog.as_ref().unwrap().accept(Some(hdrs), Some(local_sdp.into_bytes())).map_err(err)?;
                    info!("Inbound call {} connected to {}", call_id, remote_rtp);
                    Some(ctx.cancel.clone())
                } else { None }
            }; // MutexGuard dropped here

            // Session timer (outside the lock)
            if let Some(cc) = cancel {
                start_session_timer(None, call_id.to_string(), st, cc);
            }
            Ok(())
        })
    }

    // ─── Call control ────────────────────────────────────────────────────────

    pub fn reject(&self, call_id: &str, code: u16) -> Result<()> {
        let mut s = self.state.lock().unwrap();
        let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
        if let Some(ref d) = ctx.server_dialog { let _ = d.reject(Some(rsip::StatusCode::from(code)), None); }
        if let Some(ctx) = s.calls.remove(call_id) {
            let _ = self.event_tx.try_send(EndpointEvent::CallTerminated { session: ctx.session, reason: format!("Rejected {}", code) });
        }
        Ok(())
    }

    pub fn hangup(&self, call_id: &str) -> Result<()> {
        let (st, etx) = (self.state.clone(), self.event_tx.clone());
        self.runtime.block_on(async {
            let ctx = st.lock().unwrap().calls.remove(call_id);
            if let Some(ctx) = ctx {
                ctx.cancel.cancel();
                if let Some(ref d) = ctx.client_dialog { let _ = d.hangup().await; }
                else if let Some(ref d) = ctx.server_dialog { let _ = d.bye().await; }
                let _ = etx.try_send(EndpointEvent::CallTerminated { session: ctx.session, reason: "local hangup".into() });
            }
            Ok(())
        })
    }

    pub fn send_dtmf(&self, call_id: &str, digits: &str) -> Result<()> { self.send_dtmf_with_method(call_id, digits, "rfc2833") }

    pub fn send_dtmf_with_method(&self, call_id: &str, digits: &str, method: &str) -> Result<()> {
        let st = self.state.clone();
        let digits = digits.to_string();
        let method = method.to_string();
        self.runtime.block_on(async {
            let s = st.lock().unwrap();
            let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
            for d in digits.chars() {
                match method.as_str() {
                    "sip_info" | "info" => {
                        let body = format!("Signal={}\r\nDuration=160\r\n", d);
                        let hdrs = vec![rsip::Header::Other("Content-Type".into(), "application/dtmf-relay".into())];
                        if let Some(ref dl) = ctx.client_dialog { let _ = dl.info(Some(hdrs), Some(body.into_bytes())).await; }
                        else if let Some(ref dl) = ctx.server_dialog { let _ = dl.info(Some(hdrs), Some(body.into_bytes())).await; }
                    }
                    _ => { if let Some(ref rtp) = ctx.rtp { let _ = rtp.send_dtmf_event(d, 200).await; } }
                }
            }
            Ok(())
        })
    }

    pub fn send_info(&self, call_id: &str, content_type: &str, body: &str) -> Result<()> {
        let st = self.state.clone();
        let ct = content_type.to_string();
        let b = body.to_string();
        self.runtime.block_on(async {
            let (cd, sd) = {
                let s = st.lock().unwrap();
                let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
                (ctx.client_dialog.clone(), ctx.server_dialog.clone())
            };
            let hdrs = vec![rsip::Header::Other("Content-Type".into(), ct)];
            if let Some(d) = cd { d.info(Some(hdrs), Some(b.into_bytes())).await.map_err(err)?; }
            else if let Some(d) = sd { d.info(Some(hdrs), Some(b.into_bytes())).await.map_err(err)?; }
            Ok(())
        })
    }

    pub fn transfer(&self, call_id: &str, dest_uri: &str) -> Result<()> {
        let (st, dest) = (self.state.clone(), dest_uri.to_string());
        self.runtime.block_on(async {
            let (cd, sd) = {
                let s = st.lock().unwrap();
                let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
                (ctx.client_dialog.clone(), ctx.server_dialog.clone())
            };
            let uri: rsip::Uri = dest.try_into().map_err(|e| err(format!("{:?}", e)))?;
            if let Some(d) = cd { d.refer(uri, None, None).await.map_err(err)?; }
            else if let Some(d) = sd { d.refer(uri, None, None).await.map_err(err)?; }
            Ok(())
        })
    }

    pub fn transfer_attended(&self, _: &str, _: &str) -> Result<()> {
        Err(EndpointError::Other("attended transfer not supported".into()))
    }

    pub fn hold(&self, call_id: &str) -> Result<()> {
        let st = self.state.clone();
        self.runtime.block_on(async {
            let (cd, sd, sdp, held) = {
                let s = st.lock().unwrap();
                let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
                if ctx.held.load(Ordering::Relaxed) { return Ok(()); }
                let sdp = ctx.local_sdp.as_ref().ok_or(EndpointError::Other("no SDP".into()))?
                    .replace("a=sendrecv", "a=sendonly");
                (ctx.client_dialog.clone(), ctx.server_dialog.clone(), sdp, ctx.held.clone())
            };
            let hdrs = vec![rsip::Header::Other("Content-Type".into(), "application/sdp".into())];
            if let Some(d) = cd { d.reinvite(Some(hdrs), Some(sdp.into_bytes())).await.map_err(err)?; }
            else if let Some(d) = sd { d.reinvite(Some(hdrs), Some(sdp.into_bytes())).await.map_err(err)?; }
            held.store(true, Ordering::Relaxed);
            info!("Call {} held (sendonly)", call_id);
            Ok(())
        })
    }

    pub fn unhold(&self, call_id: &str) -> Result<()> {
        let st = self.state.clone();
        self.runtime.block_on(async {
            let (cd, sd, sdp, held) = {
                let s = st.lock().unwrap();
                let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
                if !ctx.held.load(Ordering::Relaxed) { return Ok(()); }
                let sdp = ctx.local_sdp.as_ref().ok_or(EndpointError::Other("no SDP".into()))?
                    .replace("a=sendonly", "a=sendrecv")
                    .replace("a=inactive", "a=sendrecv");
                (ctx.client_dialog.clone(), ctx.server_dialog.clone(), sdp, ctx.held.clone())
            };
            let hdrs = vec![rsip::Header::Other("Content-Type".into(), "application/sdp".into())];
            if let Some(d) = cd { d.reinvite(Some(hdrs), Some(sdp.into_bytes())).await.map_err(err)?; }
            else if let Some(d) = sd { d.reinvite(Some(hdrs), Some(sdp.into_bytes())).await.map_err(err)?; }
            held.store(false, Ordering::Relaxed);
            info!("Call {} unheld (sendrecv)", call_id);
            Ok(())
        })
    }

    // ─── Audio control ───────────────────────────────────────────────────────

    fn with_call<F, R>(&self, call_id: &str, f: F) -> Result<R> where F: FnOnce(&CallContext) -> R {
        let s = self.state.lock().unwrap();
        let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
        Ok(f(ctx))
    }

    fn with_call_mut<F, R>(&self, call_id: &str, f: F) -> Result<R> where F: FnOnce(&mut CallContext) -> R {
        let mut s = self.state.lock().unwrap();
        let ctx = s.calls.get_mut(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
        Ok(f(ctx))
    }

    pub fn mute(&self, call_id: &str) -> Result<()> { self.with_call(call_id, |c| c.muted.store(true, Ordering::Relaxed)) }
    pub fn unmute(&self, call_id: &str) -> Result<()> { self.with_call(call_id, |c| c.muted.store(false, Ordering::Relaxed)) }
    pub fn pause(&self, call_id: &str) -> Result<()> { self.with_call(call_id, |c| c.paused.store(true, Ordering::Relaxed)) }
    pub fn resume(&self, call_id: &str) -> Result<()> { self.with_call(call_id, |c| c.paused.store(false, Ordering::Relaxed)) }
    /// Reset playout flag so next wait_for_playout blocks until audio buffer drains.
    /// Call this before wait_for_playout to ensure accurate playout tracking.
    /// Does NOT clear buffered audio — use clear_buffer for that.
    pub fn flush(&self, call_id: &str) -> Result<()> { self.with_call(call_id, |c| { if let Ok(mut d) = c.playout_notify.0.lock() { *d = false; } }) }
    pub fn clear_buffer(&self, call_id: &str) -> Result<()> {
        info!("clear_buffer: call={} clearing audio buffer", call_id);
        self.with_call(call_id, |c| {
            c.audio_buf.clear();
            // Reset the input resampler — stale filter state from the previous speech
            // segment would produce a click/tick at the start of the next segment.
            *c.input_resampler.lock().unwrap() = None;
        })
    }

    pub fn wait_for_playout(&self, call_id: &str, timeout_ms: u64) -> Result<bool> {
        self.with_call(call_id, |c| {
            let (lock, cvar) = &*c.playout_notify;
            let guard = lock.lock().unwrap();
            !cvar.wait_timeout_while(guard, std::time::Duration::from_millis(timeout_ms), |done| !*done).unwrap().1.timed_out()
        })
    }

    /// Push audio samples into the shared AudioBuffer.
    ///
    /// If buffer is below threshold (1s), the completion callback fires immediately
    /// and this returns quickly. If above threshold, the callback is deferred until
    /// the RTP send loop drains below threshold.
    ///
    /// The `on_complete` callback is called from the RTP send loop's tokio thread.
    /// It should use loop.call_soon_threadsafe to signal Python asynchronously.
    ///
    /// Matches WebRTC C++ InternalSource::capture_frame exactly.
    pub fn send_audio_with_callback(&self, call_id: &str, frame: &AudioFrame, on_complete: crate::sip::audio_buffer::CompletionCallback) -> Result<()> {
        let (audio_buf, resampler) = {
            let s = self.state.lock().unwrap();
            let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
            (ctx.audio_buf.clone(), ctx.input_resampler.clone())
        };
        let target_rate = self.config.output_sample_rate;
        if frame.sample_rate != 0 && frame.sample_rate != target_rate {
            let mut guard = resampler.lock().unwrap();
            if guard.is_none() {
                info!("send_audio: resampling {}Hz -> {}Hz (first frame: {} samples)", frame.sample_rate, target_rate, frame.data.len());
                *guard = crate::sip::resampler::Resampler::new_voip(frame.sample_rate, target_rate);
            }
            if let Some(ref mut r) = *guard {
                let resampled = r.process(&frame.data).to_vec();
                debug!("send_audio: resampled {} -> {} samples", frame.data.len(), resampled.len());
                audio_buf.push(&resampled, on_complete)
                    .map_err(|e| EndpointError::Other(e.into()))
            } else {
                info!("send_audio: resampler init failed, pushing raw {} samples at {}Hz", frame.data.len(), frame.sample_rate);
                audio_buf.push(&frame.data, on_complete)
                    .map_err(|e| EndpointError::Other(e.into()))
            }
        } else {
            audio_buf.push(&frame.data, on_complete)
                .map_err(|e| EndpointError::Other(e.into()))
        }
    }

    /// Simple send_audio without callback — for backward compatibility.
    pub fn send_audio(&self, call_id: &str, frame: &AudioFrame) -> Result<()> {
        self.send_audio_with_callback(call_id, frame, Box::new(|| {}))
    }

    /// Send audio without backpressure — push directly, drop if full.
    /// Used by Node.js adapter where napi ThreadsafeFunction callbacks
    /// don't reliably fire from tokio threads.
    pub fn send_audio_no_backpressure(&self, call_id: &str, frame: &AudioFrame) -> Result<()> {
        let audio_buf = self.with_call(call_id, |c| c.audio_buf.clone())?;
        let target_rate = self.config.output_sample_rate;
        if frame.sample_rate != 0 && frame.sample_rate != target_rate {
            let resampled = crate::sip::resampler::Resampler::new_voip(frame.sample_rate, target_rate)
                .map(|mut r| r.process(&frame.data).to_vec())
                .unwrap_or_else(|| frame.data.clone());
            audio_buf.push_no_backpressure(&resampled);
        } else {
            audio_buf.push_no_backpressure(&frame.data);
        }
        Ok(())
    }

    /// Send background audio to be mixed with agent voice in the RTP send loop.
    /// Used by publish_track (background audio, hold music, etc.).
    pub fn send_background_audio(&self, call_id: &str, frame: &AudioFrame) -> Result<()> {
        let bg_buf = self.with_call(call_id, |c| c.bg_audio_buf.clone())?;
        let target_rate = self.config.output_sample_rate;
        if frame.sample_rate != 0 && frame.sample_rate != target_rate {
            let resampled = crate::sip::resampler::Resampler::new_voip(frame.sample_rate, target_rate)
                .map(|mut r| r.process(&frame.data).to_vec())
                .unwrap_or_else(|| frame.data.clone());
            bg_buf.push_no_backpressure(&resampled);
        } else {
            bg_buf.push_no_backpressure(&frame.data);
        }
        Ok(())
    }

    pub fn recv_audio(&self, call_id: &str) -> Result<Option<AudioFrame>> {
        self.with_call(call_id, |c| c.incoming_rx.try_recv().ok())
    }

    pub fn recv_audio_blocking(&self, call_id: &str, timeout_ms: u64) -> Result<Option<AudioFrame>> {
        let rx = self.with_call(call_id, |c| c.incoming_rx.clone())?;
        Ok(rx.recv_timeout(std::time::Duration::from_millis(timeout_ms)).ok())
    }

    pub fn incoming_rx(&self, call_id: &str) -> Result<Receiver<AudioFrame>> {
        self.with_call(call_id, |c| c.incoming_rx.clone())
    }

    pub fn playout_notify(&self, call_id: &str) -> Result<Arc<(Mutex<bool>, Condvar)>> {
        self.with_call(call_id, |c| c.playout_notify.clone())
    }

    pub fn queued_frames(&self, call_id: &str) -> Result<usize> {
        let spf = (self.config.output_sample_rate * 20 / 1000) as usize; // samples per 20ms frame
        self.with_call(call_id, |c| c.audio_buf.len() / spf)
    }

    /// Get queued audio duration in milliseconds (real buffer state).
    /// Matches WebRTC's audioSource.queuedDuration.
    pub fn queued_duration_ms(&self, call_id: &str) -> Result<f64> {
        self.with_call(call_id, |c| c.audio_buf.queued_duration_ms(self.config.output_sample_rate))
    }

    /// Set a callback to fire when buffer drains to empty (playout complete).
    /// Matches WebRTC's audioSource.waitForPlayout() — truly async, pause-aware.
    pub fn wait_for_playout_notify(&self, call_id: &str, on_complete: crate::sip::audio_buffer::CompletionCallback) -> Result<()> {
        let audio_buf = self.with_call(call_id, |c| c.audio_buf.clone())?;
        audio_buf.set_playout_callback(on_complete);
        Ok(())
    }

    pub fn start_recording(&self, call_id: &str, path: &str, stereo: bool) -> Result<()> {
        let mode = if stereo { crate::recorder::RecordingMode::Stereo } else { crate::recorder::RecordingMode::Mono };
        let rec = self.recording_mgr.start(call_id, path, mode, self.config.output_sample_rate);
        self.with_call(call_id, |c| {
            *c.recorder.lock().unwrap() = Some(rec.clone());
        })
    }

    pub fn stop_recording(&self, call_id: &str) -> Result<()> {
        self.recording_mgr.stop(call_id);
        self.with_call(call_id, |c| {
            *c.recorder.lock().unwrap() = None;
        })
    }

    pub fn detect_beep(&self, call_id: &str, config: BeepDetectorConfig) -> Result<()> {
        self.with_call(call_id, |c| *c.beep_detector.lock().unwrap() = Some(BeepDetector::new(config)))
    }

    pub fn cancel_beep_detection(&self, call_id: &str) -> Result<()> {
        self.with_call(call_id, |c| {
            if c.beep_detector.lock().unwrap().take().is_some() { Ok(()) }
            else { Err(EndpointError::Other("no beep detection".into())) }
        })?
    }

    pub fn input_sample_rate(&self) -> u32 { self.config.input_sample_rate }
    pub fn output_sample_rate(&self) -> u32 { self.config.output_sample_rate }
    pub fn events(&self) -> Receiver<EndpointEvent> { self.event_rx.clone() }

    pub fn shutdown(&self) -> Result<()> {
        if self.cancel.is_cancelled() { return Ok(()); }
        // Unregister before shutdown to clear stale registrations at the proxy.
        // Without this, the proxy keeps the old Contact for up to 120s,
        // causing 486 BusyHere for new registrations with the same AOR.
        let _ = self.unregister();
        self.cancel.cancel();
        let ids: Vec<String> = self.state.lock().unwrap().calls.keys().cloned().collect();
        for id in ids { let _ = self.hangup(&id); }
        info!("Agent transport shut down");
        Ok(())
    }
}

impl Drop for SipEndpoint { fn drop(&mut self) { let _ = self.shutdown(); } }

// ─── Incoming call handler ───────────────────────────────────────────────────

async fn handle_incoming(dl: &Arc<DialogLayer>, st: &Arc<Mutex<EndpointState>>, etx: &Sender<EndpointEvent>, tx: rsipstack::transaction::transaction::Transaction, _input_sample_rate: u32, output_sample_rate: u32) {
    let (ds, dr) = dl.new_dialog_state_channel();
    let cred = st.lock().unwrap().credential.clone();
    let contact = st.lock().unwrap().contact_uri.clone();
    let dialog = match dl.get_or_create_server_invite(&tx, ds, cred, contact) { Ok(d) => d, Err(e) => { error!("server invite: {}", e); return; } };

    // Send 180 Ringing to keep the transaction alive until Python calls answer()
    if let Err(e) = dialog.ringing(None, None) { error!("failed to send ringing: {}", e); return; }

    let call_id = format!("c{:016x}", rand::random::<u64>());
    let cc = CancellationToken::new();
    let (mut ctx, mut session) = new_call_context(&call_id, CallDirection::Inbound, cc.clone(), output_sample_rate);

    let req = dialog.initial_request();
    if let Ok(from) = req.from_header() { session.remote_uri = from.to_string(); }
    extract_x_headers_from_request(&req, &mut session);
    ctx.session = session.clone();
    ctx.server_dialog = Some(dialog);

    info!("Incoming call {} from {} (uuid={:?})", call_id, session.remote_uri, session.call_uuid);

    st.lock().unwrap().calls.insert(call_id.clone(), ctx);

    // Spawn the transaction receive loop — keeps tu_receiver alive so
    // dialog can send responses (180 Ringing, 200 OK) via tu_sender.
    let cc3 = cc.clone();
    tokio::spawn(async move {
        let mut tx = tx;
        loop {
            tokio::select! {
                msg = tx.receive() => { if msg.is_none() { break; } }
                _ = cc3.cancelled() => { break; }
            }
        }
    });

    let _ = etx.try_send(EndpointEvent::IncomingCall { session });

    // Watch dialog state for remote BYE (same helper as outbound)
    spawn_dialog_watcher(dr, call_id.clone(), st.clone(), etx.clone(), cc);
}
