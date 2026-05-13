use std::collections::HashMap;
use std::fmt::Display;
use std::net::{IpAddr, SocketAddr};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

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
use crate::sync::LockExt;

fn err(e: impl Display) -> EndpointError { EndpointError::Other(e.to_string()) }

// ─── Per-call context ────────────────────────────────────────────────────────

struct CallContext {
    session: CallSession,
    rtp: Option<Arc<RtpTransport>>,
    /// Two-state lifecycle (see audio_stream/endpoint.rs `StreamSession::terminated`):
    /// `false` = Active (RTP up), `true` = Terminated (BYE received or local
    /// hangup initiated, but the call still lingers in the `calls` HashMap
    /// until Python releases it via a second `hangup(sid)` call). FFI methods
    /// short-circuit to success-early on terminated, matching LiveKit's
    /// `rtc.AudioSource` behaviour after `_ffi_handle.disposed`.
    terminated: Arc<AtomicBool>,
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

/// Phase 1 of RTP setup: parse the remote SDP and bind a local UDP socket.
/// This is the only async phase. Kept separate from `setup_rtp_attach` so
/// callers can perform the UDP bind OUTSIDE the state mutex, avoiding the
/// "std::sync::Mutex guard held across await" pattern that makes the
/// enclosing future `!Send`.
async fn setup_rtp_bind(
    remote_sdp_bytes: &[u8],
    codecs: &[crate::config::Codec],
    public_addr: Option<SocketAddr>,
    local_ip_str: &str,
) -> Result<(sdp::SdpAnswer, UdpSocket, u16, IpAddr)> {
    let answer = sdp::parse_answer(remote_sdp_bytes, codecs)?;
    let rtp_sock = UdpSocket::bind("0.0.0.0:0").await.map_err(err)?;
    let rtp_port = rtp_sock.local_addr().unwrap().port();
    let ip = public_addr
        .map(|a| a.ip())
        .unwrap_or_else(|| local_ip_str.parse().unwrap_or(std::net::Ipv4Addr::UNSPECIFIED.into()));
    Ok((answer, rtp_sock, rtp_port, ip))
}

/// Phase 2 of RTP setup: attach the bound socket to the call context and
/// start the send/recv loops. Synchronous — safe to run under the state
/// mutex. Fires `CallAnswered` on success.
///
/// Caller builds the appropriate SDP afterwards — `build_offer` for outbound,
/// `build_answer` for inbound — and feeds it back via dialog.accept / dialog
/// ACK processing.
fn setup_rtp_attach(
    ctx: &mut CallContext,
    answer: sdp::SdpAnswer,
    rtp_sock: UdpSocket,
    etx: &Sender<EndpointEvent>,
    call_id: &str,
    input_sample_rate: u32,
    output_sample_rate: u32,
) -> SocketAddr {
    let remote_rtp = SocketAddr::new(answer.remote_ip, answer.remote_port);
    let dtmf_pt = answer
        .dtmf_payload_type
        .unwrap_or(crate::sip::rtp_transport::DEFAULT_DTMF_PT);
    debug!(
        "Call {} negotiated: codec={:?} dtmf_pt={} ptime={}ms remote_rtp={}",
        call_id, answer.codec, dtmf_pt, answer.ptime_ms, remote_rtp
    );
    let rtp = Arc::new(RtpTransport::new(
        Arc::new(rtp_sock),
        remote_rtp,
        answer.codec,
        ctx.cancel.clone(),
        dtmf_pt,
        answer.ptime_ms,
        input_sample_rate,
        output_sample_rate,
    ));

    let (itx, irx) = crossbeam_channel::unbounded();
    let send_handle = rtp.start_send_loop(
        ctx.audio_buf.clone(),
        ctx.bg_audio_buf.clone(),
        ctx.muted.clone(),
        ctx.paused.clone(),
        ctx.playout_notify.clone(),
        ctx.recorder.clone(),
    );
    let recv_handle = rtp.start_recv_loop(
        itx,
        etx.clone(),
        call_id.to_string(),
        ctx.session.direction,
        ctx.beep_detector.clone(),
        ctx.held.clone(),
        ctx.recorder.clone(),
    );
    ctx.rtp_tasks = vec![send_handle, recv_handle];

    ctx.rtp = Some(rtp);
    ctx.incoming_rx = irx;
    ctx.session.state = CallState::Confirmed;

    // CallAnswered carries the full session so adapters don't need a
    // secondary lookup to learn the remote URI / extra headers / direction.
    let _ = etx.try_send(EndpointEvent::CallAnswered {
        session: ctx.session.clone(),
    });
    remote_rtp
}

/// Convenience wrapper that runs both phases in sequence. Used by the
/// outbound `call_with_from` path which is inside `runtime.block_on` and
/// therefore permits holding `std::sync::Mutex` across an await. The
/// inbound auto-answer path uses the two-phase split directly so its
/// enclosing future stays `Send`.
async fn setup_rtp(
    ctx: &mut CallContext,
    remote_sdp_bytes: &[u8],
    codecs: &[crate::config::Codec],
    public_addr: Option<SocketAddr>,
    local_ip_str: &str,
    etx: &Sender<EndpointEvent>,
    call_id: &str,
    input_sample_rate: u32,
    output_sample_rate: u32,
) -> Result<(IpAddr, u16, sdp::SdpAnswer, SocketAddr)> {
    let (answer, rtp_sock, rtp_port, ip) =
        setup_rtp_bind(remote_sdp_bytes, codecs, public_addr, local_ip_str).await?;
    let answer_copy = answer.clone();
    let remote_rtp = setup_rtp_attach(
        ctx,
        answer,
        rtp_sock,
        etx,
        call_id,
        input_sample_rate,
        output_sample_rate,
    );
    Ok((ip, rtp_port, answer_copy, remote_rtp))
}

/// Two-state SIP call termination: mark `CallContext.terminated`, cancel
/// per-call tasks, emit `CallTerminated`. Idempotent — returns true if
/// the call was newly transitioned (i.e. terminated flipped false→true).
/// Does NOT remove the call from `EndpointState.calls`; that's reserved
/// for Python's `hangup(call_id)` (the equivalent of LiveKit's
/// `_ffi_handle.dispose()`).
fn terminate_sip_call(
    call_id: &str,
    st: &Arc<Mutex<EndpointState>>,
    etx: &Sender<EndpointEvent>,
    reason: String,
) -> bool {
    let (do_emit, session_for_event) = {
        let st_g = st.lock_or_recover();
        let Some(ctx) = st_g.calls.get(call_id) else { return false; };
        if ctx.terminated.swap(true, Ordering::AcqRel) {
            (false, None)
        } else {
            // Cancel per-call tasks immediately so the RTP loop exits
            // promptly; AudioBuffer Drop fires on the actual HashMap
            // removal (which happens later via `hangup`).
            ctx.cancel.cancel();
            (true, Some(ctx.session.clone()))
        }
    };
    if let (true, Some(session)) = (do_emit, session_for_event) {
        let _ = etx.try_send(EndpointEvent::CallTerminated { session, reason });
    }
    do_emit
}

/// Classify a `TerminatedReason` + call direction into a human-readable
/// "locally" / "by remote" / "by timeout" / "by proxy" label.
///
/// Pulled out so the classification is unit-testable without spawning
/// a real dialog watcher task.
fn classify_termination(
    reason: &rsipstack::dialog::dialog::TerminatedReason,
    direction: CallDirection,
) -> &'static str {
    use rsipstack::dialog::dialog::TerminatedReason;
    let is_uac = match reason {
        TerminatedReason::UacBye
        | TerminatedReason::UacCancel
        | TerminatedReason::UacBusy
        | TerminatedReason::UacOther(_) => Some(true),
        TerminatedReason::UasBye
        | TerminatedReason::UasBusy
        | TerminatedReason::UasDecline
        | TerminatedReason::UasOther(_) => Some(false),
        TerminatedReason::Timeout
        | TerminatedReason::ProxyError(_)
        | TerminatedReason::ProxyAuthRequired => None,
    };
    match (is_uac, direction) {
        (Some(true), CallDirection::Outbound) => "locally",
        (Some(true), CallDirection::Inbound) => "by remote",
        (Some(false), CallDirection::Outbound) => "by remote",
        (Some(false), CallDirection::Inbound) => "locally",
        (None, _) => match reason {
            TerminatedReason::Timeout => "by timeout",
            _ => "by proxy",
        },
    }
}

/// Watch dialog state for BYE/termination and emit CallTerminated.
///
/// rsipstack's `TerminatedReason` uses `Uac*` / `Uas*` variants that are
/// *role-based*, not direction-based. The UAC is whoever sent the original
/// INVITE and the UAS is whoever received it, so:
///
/// * **Outbound call** (we are UAC): `UacBye` means *we* sent BYE,
///   `UasBye` means the *peer* sent BYE.
/// * **Inbound call** (we are UAS): `UacBye` means the *peer* sent BYE,
///   `UasBye` means *we* sent BYE.
///
/// We therefore need the `CallDirection` to produce an accurate log line.
fn spawn_dialog_watcher(
    mut dr: DialogStateReceiver,
    call_id: String,
    direction: CallDirection,
    st: Arc<Mutex<EndpointState>>,
    etx: Sender<EndpointEvent>,
    cc: CancellationToken,
    global_cancel: CancellationToken,
) {
    tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = global_cancel.cancelled() => {
                    debug!("Dialog watcher for {} exiting (global cancel)", call_id);
                    break;
                }
                _ = cc.cancelled() => {
                    debug!("Dialog watcher for {} exiting (per-call cancel)", call_id);
                    break;
                }
                ds = dr.recv() => {
                    let Some(ds) = ds else { break; };
                    if let DialogState::Terminated(_, reason) = ds {
                        let side = classify_termination(&reason, direction);
                        info!("Call {} terminated {}: {:?}", call_id, side, reason);
                        // Two-state transition: mark terminated + cancel
                        // RTP/dialog tasks + emit CallTerminated, but DO NOT
                        // remove from the HashMap. Python's `hangup(call_id)`
                        // (called after `_run_call.finally` runs
                        // `session.aclose()`) is the canonical release.
                        terminate_sip_call(&call_id, &st, &etx, format!("{:?}", reason));
                        cc.cancel();
                        break;
                    }
                }
            }
        }
    });
}

#[cfg(test)]
mod termination_tests {
    use super::*;
    use rsipstack::dialog::dialog::TerminatedReason;
    use rsip::StatusCode;

    #[test]
    fn outbound_uac_bye_is_local() {
        // We dialed out (UAC), we sent BYE → local.
        assert_eq!(classify_termination(&TerminatedReason::UacBye, CallDirection::Outbound), "locally");
    }

    #[test]
    fn outbound_uas_bye_is_remote() {
        // We dialed out (UAC), peer (UAS) sent BYE → remote.
        assert_eq!(classify_termination(&TerminatedReason::UasBye, CallDirection::Outbound), "by remote");
    }

    #[test]
    fn inbound_uac_bye_is_remote() {
        // Regression: peer dialed us (peer=UAC, us=UAS); peer sent BYE
        // which rsipstack reports as UacBye. We are NOT the UAC, so this
        // is "by remote", not "locally".
        assert_eq!(classify_termination(&TerminatedReason::UacBye, CallDirection::Inbound), "by remote");
    }

    #[test]
    fn inbound_uas_bye_is_local() {
        // Peer dialed us (we=UAS); we sent BYE → local.
        assert_eq!(classify_termination(&TerminatedReason::UasBye, CallDirection::Inbound), "locally");
    }

    #[test]
    fn outbound_uac_cancel_is_local() {
        assert_eq!(classify_termination(&TerminatedReason::UacCancel, CallDirection::Outbound), "locally");
    }

    #[test]
    fn inbound_uas_decline_is_local() {
        // We rejected/declined the inbound call.
        assert_eq!(classify_termination(&TerminatedReason::UasDecline, CallDirection::Inbound), "locally");
    }

    #[test]
    fn outbound_uas_busy_is_remote() {
        assert_eq!(classify_termination(&TerminatedReason::UasBusy, CallDirection::Outbound), "by remote");
    }

    #[test]
    fn timeout_classified_separately() {
        assert_eq!(classify_termination(&TerminatedReason::Timeout, CallDirection::Outbound), "by timeout");
        assert_eq!(classify_termination(&TerminatedReason::Timeout, CallDirection::Inbound), "by timeout");
    }

    #[test]
    fn proxy_error_classified_as_proxy() {
        assert_eq!(
            classify_termination(&TerminatedReason::ProxyError(StatusCode::ServerInternalError), CallDirection::Outbound),
            "by proxy"
        );
        assert_eq!(
            classify_termination(&TerminatedReason::ProxyAuthRequired, CallDirection::Outbound),
            "by proxy"
        );
    }

    #[test]
    fn uas_other_and_uac_other_classified_by_direction() {
        // UacOther with outbound = local (we sent the odd response).
        assert_eq!(
            classify_termination(&TerminatedReason::UacOther(StatusCode::Unauthorized), CallDirection::Outbound),
            "locally"
        );
        // UasOther with outbound = remote.
        assert_eq!(
            classify_termination(&TerminatedReason::UasOther(StatusCode::ServerInternalError), CallDirection::Outbound),
            "by remote"
        );
    }
}

/// Start session timer refresh (periodic Re-INVITE).
///
/// Runs as a tokio task so shutdown (cancel) is observed within one tick,
/// instead of waiting for the full refresh interval (up to 15 minutes).
fn start_session_timer(
    session_expires: Option<u32>,
    call_id: String,
    st: Arc<Mutex<EndpointState>>,
    cc: CancellationToken,
) {
    let Some(secs) = session_expires else { return; };
    let refresh = (secs / 2).max(30) as u64;
    info!("Session timer: {}s (refresh every {}s)", secs, refresh);
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(refresh));
        interval.tick().await; // consume immediate first tick
        loop {
            tokio::select! {
                _ = cc.cancelled() => break,
                _ = interval.tick() => {}
            }
            // Extract data and dialog refs from lock, then drop before the await
            // to avoid holding the mutex across an async operation.
            let reinvite_info = {
                let s = st.lock_or_recover();
                let Some(ctx) = s.calls.get(&call_id) else { break; };
                let Some(ref sdp) = ctx.local_sdp else { break; };
                let body = sdp.clone().into_bytes();
                let cd = ctx.client_dialog.clone();
                let sd = ctx.server_dialog.clone();
                (body, cd, sd)
            };
            let hdrs = vec![rsip::Header::ContentType("application/sdp".into())];
            if let Some(ref d) = reinvite_info.1 {
                let _ = d.reinvite(Some(hdrs), Some(reinvite_info.0)).await;
            } else if let Some(ref d) = reinvite_info.2 {
                let _ = d.reinvite(Some(hdrs), Some(reinvite_info.0)).await;
            }
            debug!("Session timer refresh for call {}", call_id);
        }
    });
}

fn new_call_context(
    call_id: &str,
    direction: CallDirection,
    cc: CancellationToken,
    output_sample_rate: u32,
    event_tx: Sender<EndpointEvent>,
) -> (CallContext, CallSession) {
    let session = CallSession::new(call_id.to_string(), direction);
    let (_itx, irx) = crossbeam_channel::unbounded();
    let ctx = CallContext {
        session: session.clone(),
        rtp: None,
        terminated: Arc::new(AtomicBool::new(false)),
        audio_buf: Arc::new(AudioBuffer::with_queue_size(
            200,
            output_sample_rate,
            call_id.to_string(),
            event_tx.clone(),
        )),
        bg_audio_buf: Arc::new(AudioBuffer::with_queue_size(
            200,
            output_sample_rate,
            format!("{}-bg", call_id),
            event_tx,
        )),
        incoming_rx: irx,
        muted: Arc::new(AtomicBool::new(false)),
        paused: Arc::new(AtomicBool::new(false)),
        held: Arc::new(AtomicBool::new(false)),
        playout_notify: Arc::new((Mutex::new(false), Condvar::new())),
        beep_detector: Arc::new(Mutex::new(None)),
        recorder: Arc::new(Mutex::new(None)),
        input_resampler: Arc::new(Mutex::new(None)),
        cancel: cc,
        rtp_tasks: Vec::new(),
        client_dialog: None,
        server_dialog: None,
        local_sdp: None,
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
    /// Monotonic counter for allocating async_ids returned by
    /// `send_audio_async` / `wait_for_playout_async`. See AudioStreamEndpoint
    /// for full rationale — same pattern, same event channel.
    async_id_counter: Arc<std::sync::atomic::AtomicU64>,
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

        let (st, cc, etx2, ua) = (state.clone(), cancel.clone(), etx.clone(), config.user_agent.clone());
        let config_inner = config.clone();
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

            // Incoming transaction handler — must watch the cancel token
            // explicitly. The `rx` receiver lives off `EndpointInner` which
            // we hold via `dl` (Arc) inside EndpointState.dialog_layer, so the
            // sender side is alive until SipEndpoint itself drops. Without
            // the explicit `select!`, the task would only terminate when the
            // tokio runtime is destroyed (forced kill of the parked task)
            // instead of cooperating cleanly with shutdown().
            let (dl2, st2, etx3, cc4) = (dl.clone(), st.clone(), etx2.clone(), cc.clone());
            let cfg2 = config_inner;
            let mut rx = rx;
            tokio::spawn(async move {
                loop {
                    tokio::select! {
                        _ = cc4.cancelled() => {
                            debug!("Incoming transaction handler exiting on cancel");
                            break;
                        }
                        msg = rx.recv() => {
                            let Some(mut tx) = msg else { break; };
                            if tx.original.method == rsip::Method::Invite {
                                handle_incoming(&dl2, &st2, &etx3, tx, cc4.clone(), cfg2.clone()).await;
                            } else if let Some(dialog) = dl2.match_dialog(&tx) {
                                match dialog {
                                    rsipstack::dialog::dialog::Dialog::ServerInvite(mut d) => { let _ = d.handle(&mut tx).await; }
                                    rsipstack::dialog::dialog::Dialog::ClientInvite(mut d) => { let _ = d.handle(&mut tx).await; }
                                    _ => {}
                                }
                            }
                        }
                    }
                }
            });

            let mut s = st.lock_or_recover();
            s.dialog_layer = Some(dl);
            s.local_addr = Some(la);
            s.sip_server_addr = Some(remote_addr);
            Ok::<_, EndpointError>(())
        })?;

        info!("Agent transport initialized");
        Ok(Self {
            config,
            runtime: rt,
            state,
            event_tx: etx,
            event_rx: erx,
            cancel,
            recording_mgr: RecordingManager::new(),
            async_id_counter: Arc::new(std::sync::atomic::AtomicU64::new(1)),
        })
    }

    pub fn register(&self, username: &str, password: &str) -> Result<()> {
        let (srv, exp, stun) = (self.config.sip_server.clone(), self.config.register_expires, self.config.stun_server.clone());
        let (user, pass) = (username.to_string(), password.to_string());
        let (st, etx, cc) = (self.state.clone(), self.event_tx.clone(), self.cancel.clone());

        let handle = self.runtime.handle().clone();
        let jh = self.runtime.spawn_blocking(move || {
        handle.block_on(async {
            let cred = Credential { username: user.clone(), password: pass.clone(), realm: None };
            let (ei, la) = { let s = st.lock_or_recover(); (s.dialog_layer.as_ref().unwrap().endpoint.clone(), s.local_addr.clone().unwrap()) };

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
            let sip_addr = st.lock_or_recover().sip_server_addr;
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
                { let mut s = st.lock_or_recover(); s.registered = true; s.credential = Some(cred); s.contact_uri = Some(contact_uri.clone()); s.aor = Some(aor.clone()); s.public_addr = pa; }
                let _ = etx.try_send(EndpointEvent::Registered);

                // Re-registration loop (TCP keepalive is handled by the persistent connection)
                let re = reg.expires().max(50) as u64;
                let (st2, etx2) = (st.clone(), etx.clone());
                tokio::spawn(async move {
                    loop {
                        tokio::select! { _ = cc.cancelled() => break, _ = tokio::time::sleep(std::time::Duration::from_secs(re)) => {} }
                        match reg.register(server_uri.clone(), Some(exp)).await {
                            Ok(r) if r.status_code == rsip::StatusCode::OK => debug!("Re-registered"),
                            Ok(r) => { st2.lock_or_recover().registered = false; let _ = etx2.try_send(EndpointEvent::RegistrationFailed { error: format!("{}", r.status_code) }); }
                            Err(e) => { st2.lock_or_recover().registered = false; let _ = etx2.try_send(EndpointEvent::RegistrationFailed { error: e.to_string() }); }
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
        });
        self.runtime.block_on(jh).map_err(|e| EndpointError::Other(e.to_string()))?
    }

    pub fn unregister(&self) -> Result<()> {
        let (srv, st, etx) = (self.config.sip_server.clone(), self.state.clone(), self.event_tx.clone());
        let st2 = st.clone();
        let handle = self.runtime.handle().clone();
        let jh = self.runtime.spawn_blocking(move || {
        handle.block_on(async {
            let (ei, cred) = {
                let s = st.lock_or_recover();
                (s.dialog_layer.as_ref().cloned(), s.credential.clone())
            };
            if let (Some(ei), Some(cred)) = (ei, cred) {
                let sip_addr = st.lock_or_recover().sip_server_addr;
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
        })
        });
        let _ = self.runtime.block_on(jh);
        let mut s = st2.lock_or_recover();
        s.registered = false;
        let _ = etx.try_send(EndpointEvent::Unregistered);
        Ok(())
    }

    pub fn is_registered(&self) -> bool { self.state.lock_or_recover().registered }

    // ─── Outbound call ───────────────────────────────────────────────────────

    pub fn call(&self, dest_uri: &str, headers: Option<HashMap<String, String>>) -> Result<String> {
        self.call_with_from(dest_uri, None, headers, None)
    }

    /// Make an outbound call with an optional From URI.
    /// If `from_uri` is None, uses the AOR (sip:user@domain) from registration.
    pub fn call_with_from(&self, dest_uri: &str, from_uri: Option<&str>, headers: Option<HashMap<String, String>>, external_call_id: Option<String>) -> Result<String> {
        let (dest, cfg, st, etx, global_cancel) = (dest_uri.to_string(), self.config.clone(), self.state.clone(), self.event_tx.clone(), self.cancel.clone());
        let from_override = from_uri.map(|s| s.to_string());
        let handle = self.runtime.handle().clone();
        let jh = self.runtime.spawn_blocking(move || {
        handle.block_on(async {
            let (dl, cred, contact, aor, la, pa) = {
                let s = st.lock_or_recover();
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
            let (mut ctx, mut session) = new_call_context(&call_id, CallDirection::Outbound, cc.clone(), cfg.output_sample_rate, etx.clone());
            session.remote_uri = dest;
            extract_x_headers(&resp, &mut session);
            ctx.session = session.clone();
            ctx.client_dialog = Some(dialog);

            // Set up RTP — for outbound, the offer was already sent in the INVITE.
            // If setup_rtp fails AFTER 200 OK was received (rsipstack already
            // auto-sent ACK so the dialog is confirmed), we must send BYE to
            // tear down the peer's call leg. Otherwise the peer hangs waiting
            // for our RTP until its own inactivity timer fires (~30-60s).
            let setup_result = setup_rtp(&mut ctx, resp.body(), &cfg.codecs, pa, &la_str, &etx, &call_id, cfg.input_sample_rate, cfg.output_sample_rate).await;
            let (ip, rtp_port, _negotiated, remote_rtp) = match setup_result {
                Ok(v) => v,
                Err(e) => {
                    error!("Outbound call {} RTP setup failed: {} — sending BYE", call_id, e);
                    if let Some(ref d) = ctx.client_dialog {
                        let _ = d.hangup().await;
                    }
                    ctx.cancel.cancel();
                    let _ = etx.try_send(EndpointEvent::CallTerminated {
                        session: ctx.session.clone(),
                        reason: format!("rtp_setup_failed: {}", e),
                    });
                    return Err(e);
                }
            };
            ctx.local_sdp = Some(sdp::build_offer(ip, rtp_port, &cfg.codecs));

            // Watch for remote BYE
            spawn_dialog_watcher(dr, call_id.clone(), CallDirection::Outbound, st.clone(), etx.clone(), cc.clone(), global_cancel.clone());

            // Session timer
            let session_expires = parse_session_expires(&resp);

            st.lock_or_recover().calls.insert(call_id.clone(), ctx);
            let _ = etx.try_send(EndpointEvent::CallStateChanged { session });
            info!("Call {} connected to {}", call_id, remote_rtp);

            start_session_timer(session_expires, call_id.clone(), st.clone(), cc);
            Ok(call_id)
        })
        });
        self.runtime.block_on(jh).map_err(|e| EndpointError::Other(e.to_string()))?
    }

    // ─── Inbound answer (deprecated — Rust auto-answers) ────────────────────

    /// Deprecated: inbound calls are now auto-answered by Rust's
    /// `handle_incoming` between the `CallRinging` and `CallAnswered`
    /// events. This method is retained as a no-op for backwards
    /// compatibility — adapters should stop calling it.
    ///
    /// Returns `Ok(())` silently: by the time any adapter calls this,
    /// the call is either already answered (do nothing) or has failed
    /// (nothing to do). For rejection, use `reject(code)` instead.
    pub fn answer(&self, call_id: &str, _code: u16) -> Result<()> {
        debug!(
            "SipEndpoint::answer({}) is a no-op: Rust now auto-answers inbound calls",
            call_id
        );
        Ok(())
    }

    // ─── Call control ────────────────────────────────────────────────────────

    pub fn reject(&self, call_id: &str, code: u16) -> Result<()> {
        let mut s = self.state.lock_or_recover();
        let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
        if let Some(ref d) = ctx.server_dialog { let _ = d.reject(Some(rsip::StatusCode::from(code)), None); }
        if let Some(ctx) = s.calls.remove(call_id) {
            let _ = self.event_tx.try_send(EndpointEvent::CallTerminated { session: ctx.session, reason: format!("Rejected {}", code) });
        }
        Ok(())
    }

    pub fn hangup(&self, call_id: &str) -> Result<()> {
        let (st, etx) = (self.state.clone(), self.event_tx.clone());
        let call_id = call_id.to_string();
        let handle = self.runtime.handle().clone();
        let jh = self.runtime.spawn_blocking(move || {
        handle.block_on(async {
            // First transition: if still Active, mark terminated + cleanup +
            // emit CallTerminated. Idempotent — no-op if the dialog watcher
            // already terminated the call.
            terminate_sip_call(&call_id, &st, &etx, "local hangup".into());
            // Second transition: remove from the calls HashMap. This is the
            // "release the FFI handle" step that lets the underlying audio
            // buffer Drop and frees per-call resources.
            let ctx = st.lock_or_recover().calls.remove(&call_id);
            if let Some(ctx) = ctx {
                // Send BYE if the dialog is still up. If terminate_sip_call
                // ran first, cancel was already cancelled and per-call tasks
                // are exiting; the dialog handle may still be alive though,
                // so we send BYE explicitly.
                if let Some(ref d) = ctx.client_dialog { let _ = d.hangup().await; }
                else if let Some(ref d) = ctx.server_dialog { let _ = d.bye().await; }
            }
            Ok(())
        })
        });
        self.runtime.block_on(jh).map_err(|e| EndpointError::Other(e.to_string()))?
    }

    pub fn send_dtmf(&self, call_id: &str, digits: &str) -> Result<()> { self.send_dtmf_with_method(call_id, digits, "rfc2833") }

    pub fn send_dtmf_with_method(&self, call_id: &str, digits: &str, method: &str) -> Result<()> {
        let st = self.state.clone();
        let digits = digits.to_string();
        let method = method.to_string();
        let call_id = call_id.to_string();
        let handle = self.runtime.handle().clone();
        let jh = self.runtime.spawn_blocking(move || {
        handle.block_on(async {
            let s = st.lock_or_recover();
            let ctx = s.calls.get(&call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
            for d in digits.chars() {
                match method.as_str() {
                    "sip_info" | "info" => {
                        let body = format!("Signal={}\r\nDuration=160\r\n", d);
                        let hdrs = vec![rsip::Header::ContentType("application/dtmf-relay".into())];
                        if let Some(ref dl) = ctx.client_dialog { let _ = dl.info(Some(hdrs), Some(body.into_bytes())).await; }
                        else if let Some(ref dl) = ctx.server_dialog { let _ = dl.info(Some(hdrs), Some(body.into_bytes())).await; }
                    }
                    _ => { if let Some(ref rtp) = ctx.rtp { let _ = rtp.send_dtmf_event(d, 200).await; } }
                }
            }
            Ok(())
        })
        });
        self.runtime.block_on(jh).map_err(|e| EndpointError::Other(e.to_string()))?
    }

    pub fn send_info(&self, call_id: &str, content_type: &str, body: &str) -> Result<()> {
        let st = self.state.clone();
        let ct = content_type.to_string();
        let b = body.to_string();
        let call_id = call_id.to_string();
        let handle = self.runtime.handle().clone();
        let jh = self.runtime.spawn_blocking(move || {
        handle.block_on(async {
            let (cd, sd) = {
                let s = st.lock_or_recover();
                let ctx = s.calls.get(&call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
                (ctx.client_dialog.clone(), ctx.server_dialog.clone())
            };
            let hdrs = vec![rsip::Header::ContentType(ct.into())];
            if let Some(d) = cd { d.info(Some(hdrs), Some(b.into_bytes())).await.map_err(err)?; }
            else if let Some(d) = sd { d.info(Some(hdrs), Some(b.into_bytes())).await.map_err(err)?; }
            Ok(())
        })
        });
        self.runtime.block_on(jh).map_err(|e| EndpointError::Other(e.to_string()))?
    }

    pub fn transfer(&self, call_id: &str, dest_uri: &str) -> Result<()> {
        let (st, dest) = (self.state.clone(), dest_uri.to_string());
        let call_id = call_id.to_string();
        let handle = self.runtime.handle().clone();
        let jh = self.runtime.spawn_blocking(move || {
        handle.block_on(async {
            let (cd, sd) = {
                let s = st.lock_or_recover();
                let ctx = s.calls.get(&call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
                (ctx.client_dialog.clone(), ctx.server_dialog.clone())
            };
            let uri: rsip::Uri = dest.try_into().map_err(|e| err(format!("{:?}", e)))?;
            if let Some(d) = cd { d.refer(uri, None, None).await.map_err(err)?; }
            else if let Some(d) = sd { d.refer(uri, None, None).await.map_err(err)?; }
            Ok(())
        })
        });
        self.runtime.block_on(jh).map_err(|e| EndpointError::Other(e.to_string()))?
    }

    pub fn transfer_attended(&self, _: &str, _: &str) -> Result<()> {
        Err(EndpointError::Other("attended transfer not supported".into()))
    }

    pub fn hold(&self, call_id: &str) -> Result<()> {
        let st = self.state.clone();
        let call_id = call_id.to_string();
        let handle = self.runtime.handle().clone();
        let jh = self.runtime.spawn_blocking(move || {
        handle.block_on(async {
            let (cd, sd, sdp, held) = {
                let s = st.lock_or_recover();
                let ctx = s.calls.get(&call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
                if ctx.held.load(Ordering::Acquire) { return Ok(()); }
                let sdp = ctx.local_sdp.as_ref().ok_or(EndpointError::Other("no SDP".into()))?
                    .replace("a=sendrecv", "a=sendonly");
                (ctx.client_dialog.clone(), ctx.server_dialog.clone(), sdp, ctx.held.clone())
            };
            let hdrs = vec![rsip::Header::ContentType("application/sdp".into())];
            if let Some(d) = cd { d.reinvite(Some(hdrs), Some(sdp.into_bytes())).await.map_err(err)?; }
            else if let Some(d) = sd { d.reinvite(Some(hdrs), Some(sdp.into_bytes())).await.map_err(err)?; }
            held.store(true, Ordering::Release);
            info!("Call {} held (sendonly)", call_id);
            Ok(())
        })
        });
        self.runtime.block_on(jh).map_err(|e| EndpointError::Other(e.to_string()))?
    }

    pub fn unhold(&self, call_id: &str) -> Result<()> {
        let st = self.state.clone();
        let call_id = call_id.to_string();
        let handle = self.runtime.handle().clone();
        let jh = self.runtime.spawn_blocking(move || {
        handle.block_on(async {
            let (cd, sd, sdp, held) = {
                let s = st.lock_or_recover();
                let ctx = s.calls.get(&call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
                if !ctx.held.load(Ordering::Acquire) { return Ok(()); }
                let sdp = ctx.local_sdp.as_ref().ok_or(EndpointError::Other("no SDP".into()))?
                    .replace("a=sendonly", "a=sendrecv")
                    .replace("a=inactive", "a=sendrecv");
                (ctx.client_dialog.clone(), ctx.server_dialog.clone(), sdp, ctx.held.clone())
            };
            let hdrs = vec![rsip::Header::ContentType("application/sdp".into())];
            if let Some(d) = cd { d.reinvite(Some(hdrs), Some(sdp.into_bytes())).await.map_err(err)?; }
            else if let Some(d) = sd { d.reinvite(Some(hdrs), Some(sdp.into_bytes())).await.map_err(err)?; }
            held.store(false, Ordering::Release);
            info!("Call {} unheld (sendrecv)", call_id);
            Ok(())
        })
        });
        self.runtime.block_on(jh).map_err(|e| EndpointError::Other(e.to_string()))?
    }

    // ─── Audio control ───────────────────────────────────────────────────────

    fn with_call<F, R>(&self, call_id: &str, f: F) -> Result<R> where F: FnOnce(&CallContext) -> R {
        let s = self.state.lock_or_recover();
        let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
        Ok(f(ctx))
    }

    fn with_call_mut<F, R>(&self, call_id: &str, f: F) -> Result<R> where F: FnOnce(&mut CallContext) -> R {
        let mut s = self.state.lock_or_recover();
        let ctx = s.calls.get_mut(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
        Ok(f(ctx))
    }

    /// Like `with_call`, but on a terminated call returns ``Ok(default())``
    /// instead of running the closure. Used by FFI methods that should
    /// no-op after the underlying RTP transport is gone (mute, pause,
    /// clear_buffer, etc.) — mirrors LiveKit's
    /// ``_ffi_handle.disposed`` short-circuit in
    /// ``rtc.AudioSource.capture_frame``.
    fn with_active_call<F, R>(&self, call_id: &str, default: R, f: F) -> Result<R>
    where F: FnOnce(&CallContext) -> R {
        let s = self.state.lock_or_recover();
        let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
        if ctx.terminated.load(Ordering::Acquire) {
            return Ok(default);
        }
        Ok(f(ctx))
    }

    pub fn mute(&self, call_id: &str) -> Result<()> { self.with_active_call(call_id, (), |c| c.muted.store(true, Ordering::Release)) }
    pub fn unmute(&self, call_id: &str) -> Result<()> { self.with_active_call(call_id, (), |c| c.muted.store(false, Ordering::Release)) }
    pub fn pause(&self, call_id: &str) -> Result<()> { self.with_active_call(call_id, (), |c| c.paused.store(true, Ordering::Release)) }
    pub fn resume(&self, call_id: &str) -> Result<()> { self.with_active_call(call_id, (), |c| c.paused.store(false, Ordering::Release)) }
    /// Reset playout flag so next wait_for_playout blocks until audio buffer drains.
    /// Call this before wait_for_playout to ensure accurate playout tracking.
    /// Does NOT clear buffered audio — use clear_buffer for that.
    pub fn flush(&self, call_id: &str) -> Result<()> { self.with_active_call(call_id, (), |c| { if let Ok(mut d) = c.playout_notify.0.lock() { *d = false; } }) }
    pub fn clear_buffer(&self, call_id: &str) -> Result<()> {
        debug!("clear_buffer: call={} clearing audio buffer", call_id);
        self.with_active_call(call_id, (), |c| {
            c.audio_buf.clear();
            // Reset the input resampler — stale filter state from the previous speech
            // segment would produce a click/tick at the start of the next segment.
            *c.input_resampler.lock_or_recover() = None;
        })
    }

    pub fn wait_for_playout(&self, call_id: &str, timeout_ms: u64) -> Result<bool> {
        self.with_call(call_id, |c| {
            let (lock, cvar) = &*c.playout_notify;
            let guard = lock.lock_or_recover();
            !cvar.wait_timeout_while(guard, std::time::Duration::from_millis(timeout_ms), |done| !*done).unwrap().1.timed_out()
        })
    }

    /// Push audio samples and return the `async_id` the caller must
    /// await on `EndpointEvent::AudioCaptureComplete` (or
    /// `AudioCaptureError`).
    ///
    /// **Always returns `Ok(async_id)` on success** — the completion
    /// event always fires (immediately if buffer ended at-or-below
    /// threshold, deferred if above). Mirrors LiveKit's
    /// `capture_audio_frame` invariant.
    ///
    /// Returns `Err` synchronously only for misuse (call not active,
    /// buffer overflow); no event is emitted on that path.
    pub fn send_audio_async(&self, call_id: &str, frame: &AudioFrame) -> Result<u64> {
        let async_id = self.async_id_counter.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let (audio_buf, resampler) = {
            let s = self.state.lock_or_recover();
            let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
            // Terminated call: short-circuit with immediate completion event,
            // mirroring LiveKit's `rtc.AudioSource.capture_frame:119`
            // `_ffi_handle.disposed` short-circuit. See
            // `CallContext.terminated` doc.
            if ctx.terminated.load(Ordering::Acquire) {
                let _ = self.event_tx.try_send(EndpointEvent::AudioCaptureComplete {
                    session_id: call_id.to_string(),
                    async_id,
                });
                return Ok(async_id);
            }
            (ctx.audio_buf.clone(), ctx.input_resampler.clone())
        };
        let target_rate = self.config.output_sample_rate;
        let result = if frame.sample_rate != 0 && frame.sample_rate != target_rate {
            let mut guard = resampler.lock_or_recover();
            // Recreate the resampler if the source rate has changed (e.g. TTS
            // switched from 24kHz to 16kHz) — reusing stale filter state would
            // corrupt the output.
            let needs_new = match guard.as_ref() {
                None => true,
                Some(r) => r.from_rate() != frame.sample_rate,
            };
            if needs_new {
                info!("send_audio: resampling {}Hz -> {}Hz (first frame: {} samples)", frame.sample_rate, target_rate, frame.data.len());
                *guard = crate::sip::resampler::Resampler::new_voip(frame.sample_rate, target_rate);
            }
            if let Some(ref mut r) = *guard {
                let resampled = r.process(&frame.data).to_vec();
                debug!("send_audio: resampled {} -> {} samples", frame.data.len(), resampled.len());
                audio_buf.push(&resampled, async_id)
            } else {
                info!("send_audio: resampler init failed, pushing raw {} samples at {}Hz", frame.data.len(), frame.sample_rate);
                audio_buf.push(&frame.data, async_id)
            }
        } else {
            audio_buf.push(&frame.data, async_id)
        };
        result.map_err(|e| EndpointError::Other(e.into()))?;
        Ok(async_id)
    }

    /// Convenience: push audio without an async_id wait.
    pub fn send_audio(&self, call_id: &str, frame: &AudioFrame) -> Result<()> {
        let _ = self.send_audio_async(call_id, frame)?;
        Ok(())
    }

    /// Send audio without backpressure — push directly, drop if full.
    /// Used by Node.js adapter where napi ThreadsafeFunction callbacks
    /// don't reliably fire from tokio threads.
    pub fn send_audio_no_backpressure(&self, call_id: &str, frame: &AudioFrame) -> Result<()> {
        // Terminated: silent drop (matches send_audio_async behaviour).
        let audio_buf = match self.with_active_call(call_id, None, |c| Some(c.audio_buf.clone()))? {
            Some(buf) => buf,
            None => return Ok(()),
        };
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
        // Terminated call: silently drop. See audio_stream/endpoint.rs's
        // send_background_audio for the rationale (BackgroundAudioPlayer keeps
        // pumping after BYE; we no-op to match LiveKit's track-auto-unpublish
        // behaviour).
        let bg_buf = match self.with_active_call(call_id, None, |c| Some(c.bg_audio_buf.clone()))? {
            Some(buf) => buf,
            None => return Ok(()),
        };
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

    /// Register an async_id for "buffer drained to empty" notification.
    ///
    /// **Always returns `Ok(async_id)`** — the completion event always
    /// fires (immediately if buffer already empty, deferred if not).
    /// Multiple concurrent waiters supported. Pause-aware (RTP loop
    /// doesn't drain while paused).
    pub fn wait_for_playout_async(&self, call_id: &str) -> Result<u64> {
        let async_id = self.async_id_counter.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let audio_buf = {
            let s = self.state.lock_or_recover();
            let ctx = s.calls.get(call_id).ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
            // Terminated call: short-circuit with immediate completion.
            // See send_audio_async above for the rationale.
            if ctx.terminated.load(Ordering::Acquire) {
                let _ = self.event_tx.try_send(EndpointEvent::AudioPlayoutComplete {
                    session_id: call_id.to_string(),
                    async_id,
                });
                return Ok(async_id);
            }
            ctx.audio_buf.clone()
        };
        audio_buf.add_pending_playout(async_id);
        Ok(async_id)
    }

    pub fn start_recording(&self, call_id: &str, path: &str, stereo: bool) -> Result<()> {
        let mode = if stereo { crate::recorder::RecordingMode::Stereo } else { crate::recorder::RecordingMode::Mono };
        let rec = self.recording_mgr.start(call_id, path, mode, self.config.output_sample_rate);
        self.with_call(call_id, |c| {
            *c.recorder.lock_or_recover() = Some(rec.clone());
        })
    }

    pub fn stop_recording(&self, call_id: &str) -> Result<()> {
        // Idempotent — matches audio_stream::Endpoint::stop_recording. The
        // authoritative file writer lives on recording_mgr; the per-call
        // `recorder` field is just a reference for the RTP path. If the call
        // was already torn down by remote BYE, the mgr.stop() call still
        // finalizes the file and we silently skip the per-call cleanup.
        self.recording_mgr.stop(call_id);
        if let Some(c) = self.state.lock_or_recover().calls.get(call_id) {
            *c.recorder.lock_or_recover() = None;
        }
        Ok(())
    }

    pub fn detect_beep(&self, call_id: &str, config: BeepDetectorConfig) -> Result<()> {
        self.with_call(call_id, |c| *c.beep_detector.lock_or_recover() = Some(BeepDetector::new(config)))
    }

    pub fn cancel_beep_detection(&self, call_id: &str) -> Result<()> {
        self.with_call(call_id, |c| {
            if c.beep_detector.lock_or_recover().take().is_some() { Ok(()) }
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
        let ids: Vec<String> = self.state.lock_or_recover().calls.keys().cloned().collect();
        for id in ids { let _ = self.hangup(&id); }
        // Push a Shutdown sentinel so any thread blocked on wait_for_event
        // wakes immediately instead of waiting for the next poll timeout.
        let _ = self.event_tx.try_send(EndpointEvent::Shutdown);
        info!("Agent transport shut down");
        Ok(())
    }
}

impl Drop for SipEndpoint { fn drop(&mut self) { let _ = self.shutdown(); } }

// ─── Incoming call handler ───────────────────────────────────────────────────

async fn handle_incoming(
    dl: &Arc<DialogLayer>,
    st: &Arc<Mutex<EndpointState>>,
    etx: &Sender<EndpointEvent>,
    tx: rsipstack::transaction::transaction::Transaction,
    global_cancel: CancellationToken,
    cfg: EndpointConfig,
) {
    let (ds, dr) = dl.new_dialog_state_channel();
    let cred = st.lock_or_recover().credential.clone();
    let contact = st.lock_or_recover().contact_uri.clone();
    let dialog = match dl.get_or_create_server_invite(&tx, ds, cred, contact) {
        Ok(d) => d,
        Err(e) => {
            error!("server invite: {}", e);
            return;
        }
    };

    // Send 180 Ringing to keep the transaction alive while we auto-answer.
    if let Err(e) = dialog.ringing(None, None) {
        error!("failed to send ringing: {}", e);
        return;
    }

    let call_id = format!("c{:016x}", rand::random::<u64>());
    let cc = CancellationToken::new();
    let (mut ctx, mut session) = new_call_context(
        &call_id,
        CallDirection::Inbound,
        cc.clone(),
        cfg.output_sample_rate,
        etx.clone(),
    );

    let req = dialog.initial_request();
    if let Ok(from) = req.from_header() {
        session.remote_uri = from.to_string();
    }
    extract_x_headers_from_request(&req, &mut session);
    // Save remote SDP now — we'll need it below but `dialog` will be moved
    // into `ctx.server_dialog` on the next line.
    let remote_sdp = req.body().to_vec();
    ctx.session = session.clone();
    ctx.server_dialog = Some(dialog);

    info!(
        "Incoming call {} from {} (uuid={:?})",
        call_id, session.remote_uri, session.call_uuid
    );

    st.lock_or_recover().calls.insert(call_id.clone(), ctx);

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

    // Watch dialog state for remote BYE/CANCEL while we auto-answer.
    // Inbound calls — this side is the UAS.
    spawn_dialog_watcher(
        dr,
        call_id.clone(),
        CallDirection::Inbound,
        st.clone(),
        etx.clone(),
        cc,
        global_cancel,
    );

    // ── Pre-answer event ─────────────────────────────────────────────────
    // CallRinging is fired AFTER 180 Ringing is sent and state is set up,
    // but BEFORE auto-answer runs. Adapters can observe caller info here
    // (e.g., to emit a user-facing `on_ringing` / room event). Rejection
    // during this window is a future enhancement — for now it's purely
    // observational and the auto-answer proceeds regardless.
    let _ = etx.try_send(EndpointEvent::CallRinging {
        session: session.clone(),
    });

    // ── Auto-answer ──────────────────────────────────────────────────────
    // Runs setup_rtp + build_answer + dialog.accept(200). On success
    // setup_rtp internally emits CallAnswered. On failure we tear down the
    // SIP dialog with a 500, remove the call from state, and emit
    // CallTerminated so the adapter's standard termination handler cleans
    // up. No "answered but no media" limbo state.
    match auto_answer_inbound(st, &cfg, &call_id, &remote_sdp, etx).await {
        Ok(()) => {
            info!("Inbound call {} auto-answered", call_id);
        }
        Err(e) => {
            error!("Inbound call {} auto-answer failed: {}", call_id, e);
            // Pull ctx out, tear down, emit CallTerminated.
            let removed = st.lock_or_recover().calls.remove(&call_id);
            if let Some(ctx) = removed {
                if let Some(ref d) = ctx.server_dialog {
                    let _ = d.reject(Some(rsip::StatusCode::ServerInternalError), None);
                }
                ctx.cancel.cancel();
                let _ = etx.try_send(EndpointEvent::CallTerminated {
                    session: ctx.session,
                    reason: format!("auto_answer_failed: {}", e),
                });
            }
        }
    }
}

/// Run setup_rtp + build_answer + dialog.accept(200) on an inbound call.
/// Called from `handle_incoming` (which runs inside a `tokio::spawn` and
/// therefore requires the returned future to be `Send`).
///
/// **Why two-phase RTP setup:** `std::sync::MutexGuard` is `!Send`, so it
/// cannot be held across `.await` inside a `Send` future. We do the
/// blocking/async phase (`UdpSocket::bind`) with NO lock held, then
/// re-acquire the lock for the synchronous phase that mutates `ctx`.
async fn auto_answer_inbound(
    st: &Arc<Mutex<EndpointState>>,
    cfg: &EndpointConfig,
    call_id: &str,
    remote_sdp: &[u8],
    etx: &Sender<EndpointEvent>,
) -> Result<()> {
    // Phase 0: snapshot the bits we need under a short read lock
    let (pa, la_str) = {
        let s = st.lock_or_recover();
        (
            s.public_addr,
            s.local_addr
                .as_ref()
                .map(|a| a.addr.host.to_string())
                .unwrap_or("0.0.0.0".into()),
        )
    };

    // Phase 1: bind UDP socket (the only async step) WITHOUT the state lock
    let (answer, rtp_sock, rtp_port, ip) =
        setup_rtp_bind(remote_sdp, &cfg.codecs, pa, &la_str).await?;

    // Phase 2: re-acquire lock, attach to ctx, send 200 OK — all synchronous
    let mut s = st.lock_or_recover();
    let ctx = s
        .calls
        .get_mut(call_id)
        .ok_or_else(|| EndpointError::CallNotActive(call_id.to_string()))?;
    if ctx.server_dialog.is_none() {
        return Err(EndpointError::Other("no server_dialog".into()));
    }
    let negotiated = answer.clone();
    let _remote_rtp = setup_rtp_attach(
        ctx,
        answer,
        rtp_sock,
        etx,
        call_id,
        cfg.input_sample_rate,
        cfg.output_sample_rate,
    );
    let local_sdp = sdp::build_answer(ip, rtp_port, &negotiated);
    ctx.local_sdp = Some(local_sdp.clone());
    let hdrs = vec![rsip::Header::ContentType("application/sdp".into())];
    ctx.server_dialog
        .as_ref()
        .unwrap()
        .accept(Some(hdrs), Some(local_sdp.into_bytes()))
        .map_err(err)?;
    Ok(())
}
