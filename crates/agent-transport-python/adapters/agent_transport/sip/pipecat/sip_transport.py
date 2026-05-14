"""Pipecat BaseTransport adapter for SIP transport.

Replaces Pipecat's WebsocketServerTransport for SIP calls.
All audio codec/resampling/pacing is handled in Rust. Python only bridges frames.

100% compatible with Pipecat's transport interface. Exposes all SIP features:
- Event handlers: on_client_connected, on_client_disconnected, on_beep_detected, on_beep_timeout
- Session metadata: session_id, remote_uri, direction, extra_headers
- SIP call control: transfer, hold, reject, beep detection
- Audio: mute, recording, background audio, flush/playout

Frame handling:
- OutputAudioRawFrame → send_audio_notify (Rust backpressure + 20ms RTP pacing)
- InterruptionFrame → clear_buffer
- OutputDTMFFrame → send_dtmf (RFC 2833 or SIP INFO)
- OutputTransportMessageFrame → send_info (SIP INFO with JSON body)
- EndFrame/CancelFrame → hangup
- InputAudioRawFrame ← recv_audio_bytes_blocking (Rust decodes + resamples)
- InputDTMFFrame ← event polling

Bot speaking state (BotStartedSpeaking/BotStoppedSpeaking) is handled by
Pipecat's BaseOutputTransport MediaSender infrastructure.
"""

import asyncio
import json

from typing import Any, Dict, Optional

from loguru import logger

from agent_transport._event_sink import _on_event_from_rust
from agent_transport._ffi_queue import GLOBAL_DICT

try:
    from pipecat.audio.dtmf.types import KeypadEntry
    from pipecat.frames.frames import (
        CancelFrame, EndFrame, Frame, InputAudioRawFrame,
        InputDTMFFrame, InterruptionFrame, OutputAudioRawFrame,
        OutputTransportMessageFrame, OutputTransportMessageUrgentFrame,
        StartFrame, StopFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.transports.base_input import BaseInputTransport
    from pipecat.transports.base_output import BaseOutputTransport
    from pipecat.transports.base_transport import BaseTransport, TransportParams
except ImportError:
    raise ImportError("pipecat-ai is required: pip install pipecat-ai")


# ─── Input Transport ────────────────────────────────────────────────────────


class SipInputTransport(BaseInputTransport):
    """Receives audio + DTMF from SIP call.

    Audio is received from the Rust endpoint via blocking recv (GIL released),
    decoded and resampled in Rust, and pushed as InputAudioRawFrame into the
    Pipecat pipeline. DTMF and call lifecycle events are polled separately.
    """

    def __init__(self, endpoint, session_id: str, transport: "SipTransport",
                 params: Optional[TransportParams] = None,
                 event_queue: Optional[asyncio.Queue] = None, **kwargs):
        if params is None:
            params = TransportParams(
                audio_in_enabled=True,
                audio_in_passthrough=True,
            )
        # Pipecat's BaseInputTransport.start() reads params.audio_in_sample_rate
        # to set self._sample_rate. The Rust endpoint emits frames at its fixed
        # configured rate; if the user didn't specify one, propagate the
        # endpoint's rate so VAD/turn analyzers/audio-filters see matching Hz.
        # If the user specified a mismatched rate, log a warning — the frames
        # will still carry the endpoint's rate in their metadata but downstream
        # processors may miscompute based on the transport's sample_rate.
        _ep_in = endpoint.input_sample_rate
        if params.audio_in_sample_rate is None:
            params.audio_in_sample_rate = _ep_in
        elif params.audio_in_sample_rate != _ep_in:
            logger.warning(
                "SipInputTransport: params.audio_in_sample_rate={} does not match "
                "endpoint.input_sample_rate={}. Rust emits frames at the endpoint rate; "
                "reconfigure the endpoint at construction to match.",
                params.audio_in_sample_rate, _ep_in,
            )
        super().__init__(params, **kwargs)
        self._ep = endpoint
        self._cid = session_id
        self._transport = transport
        self._event_queue = event_queue
        # Started flag — distinct from _paused (the base-class pause flag).
        # _started gates start()/stop() idempotency. The recv loop runs
        # while the tasks exist; pause/resume are handled by the base class
        # via `self._paused` which `push_audio_frame` already gates on.
        self._started = False
        self._recv_task: Optional[asyncio.Task] = None
        self._event_task: Optional[asyncio.Task] = None

    async def start(self, frame: StartFrame):
        if self._started:
            return
        await super().start(frame)
        self._started = True
        await self.set_transport_ready(frame)
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._event_task = asyncio.create_task(self._event_loop())
        await self._transport._call_event_handler("on_client_connected")

    async def stop(self, frame: EndFrame):
        if not self._started:
            await super().stop(frame)
            return
        self._started = False
        await self._cancel_tasks()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        self._started = False
        await self._cancel_tasks()
        await super().cancel(frame)

    # NOTE: pause(StopFrame) is intentionally NOT overridden — we let the
    # base class set `self._paused = True`, which `push_audio_frame()`
    # already gates on (`pipecat/transports/base_input.py:319-326`).
    # The recv loop keeps reading from Rust and producing frames, but the
    # frames are dropped at `push_audio_frame` while paused. On resume the
    # base class sets `self._paused = False` and frames flow again — no
    # need to cancel + recreate tasks. This matches base-class semantics
    # without requiring a custom restart mechanism.

    async def _cancel_tasks(self):
        """Cancel and await background tasks."""
        tasks = [t for t in [self._recv_task, self._event_task] if t and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._recv_task = None
        self._event_task = None

    async def _recv_loop(self):
        """Receive audio from Rust endpoint via blocking call (GIL released)."""
        loop = asyncio.get_running_loop()
        try:
            while self._started:
                try:
                    result = await loop.run_in_executor(
                        None, lambda: self._ep.recv_audio_bytes_blocking(self._cid, 20)
                    )
                except Exception:
                    # Session ended (remote BYE removed the call from the
                    # Rust call map). Exit the recv loop cleanly — the
                    # event loop is responsible for firing on_client_disconnected.
                    break
                if result is not None:
                    audio_bytes, sample_rate, num_channels = result
                    # push_audio_frame() drops the frame if base class
                    # `_paused == True`, so no extra check needed here.
                    await self.push_audio_frame(InputAudioRawFrame(
                        audio=bytes(audio_bytes), sample_rate=sample_rate, num_channels=num_channels,
                    ))
        except asyncio.CancelledError:
            raise

    async def _event_loop(self):
        """Poll for DTMF, call state, and lifecycle events.

        If an event_queue is provided (by the server), reads from it.
        Otherwise falls back to polling endpoint directly (standalone usage).
        """
        if self._event_queue:
            await self._event_loop_from_queue()
        else:
            await self._event_loop_from_endpoint()

    async def _event_loop_from_queue(self):
        """Read events from per-session queue (dispatched by server)."""
        while self._started:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.debug("SipInputTransport event_loop error: {}", e)
                break
            try:
                await self._handle_event(event)
            except Exception:
                logger.exception("SipInputTransport handler failed for event %r", event.get("type") if isinstance(event, dict) else event)

    async def _event_loop_from_endpoint(self):
        """Subscribe to GLOBAL_DICT for events on our session id.

        Used when this transport is constructed without a server (no
        ``_event_queue`` provided) — pipecat creates the SipTransport
        directly and we own the endpoint's event flow.
        """
        # Install the shared sink (idempotent; safe if the server in
        # another instance already installed it).
        try:
            self._ep.set_event_sink(_on_event_from_rust)
        except Exception:
            logger.debug("set_event_sink failed (already installed?)", exc_info=True)

        cid = self._cid

        def _is_ours(e: dict) -> bool:
            # Endpoint-level events have no session_id; filter to events
            # for our call. Lifecycle (registered/etc) is irrelevant
            # to InputTransport.
            return e.get("session_id") == cid

        queue = GLOBAL_DICT.subscribe(filter_fn=_is_ours)
        try:
            while self._started:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.debug("SipInputTransport endpoint event_loop error: {}", e)
                    break
                try:
                    await self._handle_event(event)
                except Exception:
                    logger.exception("SipInputTransport handler failed for event %r", event.get("type") if isinstance(event, dict) else event)
        finally:
            GLOBAL_DICT.unsubscribe(queue)

    async def _handle_event(self, event):
        """Process a single event."""
        event_type = event.get("type", "")
        if event_type == "dtmf_received":
            await self.push_frame(InputDTMFFrame(button=KeypadEntry(event["digit"])))
        elif event_type == "call_terminated":
            self._started = False
            await self._transport._call_event_handler("on_client_disconnected")
            await self.push_frame(EndFrame())
        elif event_type == "beep_detected":
            await self._transport._call_event_handler(
                "on_beep_detected",
                event.get("frequency_hz"), event.get("duration_ms"),
            )
        elif event_type == "beep_timeout":
            await self._transport._call_event_handler("on_beep_timeout")
        elif event_type in (
            "audio_capture_complete",
            "audio_playout_complete",
            "audio_buffer_drained",
            "audio_capture_error",
        ):
            # LiveKit-faithful FfiQueue dispatch: every async_id event
            # goes into the shared transport broker. OutputTransport
            # subscribes per-frame inside write_audio_frame and filters
            # to matching async_id.
            self._transport._events.put(event)


# ─── Output Transport ───────────────────────────────────────────────────────


class SipOutputTransport(BaseOutputTransport):
    """Sends audio to SIP call.

    Uses Pipecat's MediaSender infrastructure (via set_transport_ready) for:
    - Audio chunking to transport chunk size
    - BotStartedSpeaking/BotStoppedSpeaking state management
    - Proper interruption handling (task cancellation + restart)

    Audio is sent to the Rust endpoint which handles:
    - RTP packetization and 20ms pacing
    - G.711 codec encoding
    - Jitter buffer, PLC, comfort noise (if enabled)
    """

    def __init__(self, endpoint, session_id: str, transport: "SipTransport",
                 params: Optional[TransportParams] = None, **kwargs):
        if params is None:
            params = TransportParams(
                audio_out_enabled=True,
            )
        # Match Rust endpoint's outbound sample rate. Pipecat's MediaSender
        # chunks/paces audio against `params.audio_out_sample_rate`; if the
        # user leaves it unset, propagate the endpoint rate so the chunking
        # math matches the rate actually sent on the wire.
        _ep_out = endpoint.output_sample_rate
        if params.audio_out_sample_rate is None:
            params.audio_out_sample_rate = _ep_out
        elif params.audio_out_sample_rate != _ep_out:
            logger.warning(
                "SipOutputTransport: params.audio_out_sample_rate={} does not match "
                "endpoint.output_sample_rate={}. Rust resamples input but expects "
                "frames carrying the native sample rate in frame.sample_rate.",
                params.audio_out_sample_rate, _ep_out,
            )
        super().__init__(params, **kwargs)
        self._ep = endpoint
        self._cid = session_id
        self._transport = transport
        self._started = False
        # FfiQueue lives on self._transport — InputTransport pumps,
        # OutputTransport subscribes per-frame. Imported here to avoid
        # circular imports during module-load time.
        from agent_transport._ffi_queue import (
            ASYNC_ID_EVENT_TYPES as _ASYNC_ID_EVENT_TYPES,
            DEFAULT_WAIT_TIMEOUT as _DEFAULT_WAIT_TIMEOUT,
        )
        self._async_id_event_types = _ASYNC_ID_EVENT_TYPES
        self._wait_timeout = _DEFAULT_WAIT_TIMEOUT

    async def start(self, frame: StartFrame):
        if self._started:
            return
        self._started = True
        await super().start(frame)
        self._loop = asyncio.get_running_loop()
        await self.set_transport_ready(frame)

    def _supports_native_dtmf(self) -> bool:
        return True

    async def _write_dtmf_native(self, frame):
        digit = str(frame.button.value)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._ep.send_dtmf(self._cid, digit))

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        """Send audio frame to SIP call with Rust backpressure.

        LiveKit-faithful subscribe-before-request pattern:
        1. Subscribe to transport.events with a filter narrowing to this
           call's async_id-bearing events.
        2. Call send_audio_async to push the frame and get the async_id.
        3. Await the matching ``audio_capture_complete`` (or
           ``audio_capture_error``) via Queue.wait_for(predicate).
        4. Unsubscribe in finally.

        The subscribe happens BEFORE the FFI call, so even if the Rust
        side emits the completion event synchronously during the push
        (immediate-emit path for below-threshold buffer), it lands in
        our Queue and wait_for finds it.
        """
        cid = self._cid

        def _is_ours(e: dict) -> bool:
            return (
                e.get("type") in self._async_id_event_types
                and e.get("session_id") == cid
            )

        queue = self._transport._events.subscribe(
            loop=asyncio.get_running_loop(),
            filter_fn=_is_ours,
        )
        try:
            try:
                async_id = self._ep.send_audio_async(
                    self._cid,
                    frame.audio,
                    frame.sample_rate,
                    frame.num_channels,
                )
            except Exception:
                return False
            try:
                ev = await queue.wait_for(
                    lambda e: e.get("async_id") == async_id,
                    timeout=self._wait_timeout,
                )
            except asyncio.TimeoutError:
                return False
        finally:
            self._transport._events.unsubscribe(queue)

        if ev.get("type") == "audio_capture_error" or ev.get("cancelled"):
            # Either a real error (audio_capture_error with non-empty
            # reason) or a clear-induced silent discard (Rust emits
            # AudioCaptureComplete with cancelled=true on clear_buffer).
            # Both mean the frame did NOT reach the wire — surface that
            # to pipecat's MediaSender via the `False` return.
            return False
        return True

    def queued_frames(self) -> int:
        """Number of 20ms audio frames buffered in the Rust outgoing queue."""
        try:
            return self._ep.queued_frames(self._cid)
        except Exception:
            return 0

    async def send_message(self, frame):
        """Send OutputTransportMessageFrame as SIP INFO with JSON body.

        Filters RTVI internal messages (matching should_ignore_frame).
        """
        if isinstance(frame.message, dict) and frame.message.get("label") == "rtvi-ai":
            return
        try:
            msg = json.dumps(frame.message) if not isinstance(frame.message, str) else frame.message
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: self._ep.send_info(self._cid, "application/json", msg)
            )
        except Exception as e:
            logger.warning("send_message via SIP INFO failed: {}", e)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Handle InterruptionFrame → clear Rust buffer BEFORE base class processing.

        The base class cancels and restarts the MediaSender audio task. Clearing
        the Rust buffer first ensures the new task doesn't race against stale
        audio queued in Rust.

        ``clear_buffer`` is idempotent on terminated sessions (0.2.0
        Terminated lifecycle short-circuits) so no defensive
        ``try/except`` is needed — a raised exception now indicates a
        real misuse (e.g. unknown session id).
        """
        if isinstance(frame, InterruptionFrame):
            self._ep.clear_buffer(self._cid)

        await super().process_frame(frame, direction)

    async def stop(self, frame: EndFrame):
        # ``hangup`` is idempotent on terminated sessions; on Active it
        # tears down the dialog + RTP. No try/except needed.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._ep.hangup(self._cid))
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._ep.hangup(self._cid))
        await super().cancel(frame)


# ─── Composite Transport ────────────────────────────────────────────────────


class SipTransport(BaseTransport):
    """Pipecat transport for SIP calls via agent-transport.

    This is the main entry point. It creates lazily-initialized input and
    output transport processors that are wired into a Pipecat pipeline.

    Architecture:
        SipEndpoint (Rust) handles SIP signaling, RTP audio, codec encoding,
        and 20ms pacing. This transport bridges Pipecat frames to the Rust endpoint.

    Event handlers (register via @transport.event_handler("name")):
        on_client_connected  — SIP call answered, media active
        on_client_disconnected — call terminated (BYE or hangup)
        on_beep_detected — voicemail beep detected
        on_beep_timeout — beep detection timed out

    Session metadata (from SIP call session):
        transport.session_id    — internal session ID
        transport.call_uuid     — SIP call UUID
        transport.remote_uri    — remote SIP URI
        transport.direction     — "Inbound" or "Outbound"
        transport.extra_headers — custom SIP headers

    Audio control:
        mute() / unmute()           — mute/unmute outgoing audio
        pause_playback() / resume_playback() — pause/resume RTP send loop
        clear_buffer()              — clear queued audio
        send_background_audio()     — mix background audio with agent voice
        start_recording() / stop_recording() — WAV stereo call recording
        flush() / wait_for_playout() — track playback completion

    SIP-specific:
        transfer() / transfer_attended() — call transfer
        hold() / unhold() — SIP hold (re-INVITE)
        reject() — reject incoming call
        detect_beep() / cancel_beep_detection() — voicemail detection
        send_dtmf() — send DTMF digits
        send_info() — send SIP INFO message
    """

    def __init__(
        self,
        endpoint,
        session_id: str,
        *,
        name: Optional[str] = None,
        params: Optional[TransportParams] = None,
        session_data: Optional[Dict[str, Any]] = None,
        _event_queue: Optional[asyncio.Queue] = None,
        **kwargs,
    ):
        super().__init__(name=name or "SipTransport", **kwargs)
        self._ep = endpoint
        self._cid = session_id
        self._params = params
        self._session_data = session_data or {}
        self._event_queue = _event_queue
        # Shared FfiQueue for this transport — InputTransport's event
        # loop puts async_id events into it; OutputTransport subscribes
        # per-frame. LiveKit-faithful subscribe-before-request pattern.
        from agent_transport._ffi_queue import FfiQueue
        self._events: FfiQueue = FfiQueue()
        self._input: Optional[SipInputTransport] = None
        self._output: Optional[SipOutputTransport] = None

        # Register Pipecat event handlers
        self._register_event_handler("on_client_connected")
        self._register_event_handler("on_client_disconnected")
        self._register_event_handler("on_beep_detected")
        self._register_event_handler("on_beep_timeout")

    # ── Pipeline processors ──────────────────────────────────────────────

    def input(self) -> SipInputTransport:
        if self._input is None:
            self._input = SipInputTransport(
                self._ep, self._cid, transport=self,
                params=self._params, event_queue=self._event_queue,
                name=f"{self._name}-input",
            )
        return self._input

    def output(self) -> SipOutputTransport:
        if self._output is None:
            self._output = SipOutputTransport(
                self._ep, self._cid, transport=self,
                params=self._params, name=f"{self._name}-output",
            )
        return self._output

    # ── Session metadata ─────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        """Internal session ID used to address this session."""
        return self._cid

    @property
    def call_uuid(self) -> str:
        """SIP call UUID."""
        return self._session_data.get("call_uuid", self._cid)

    @property
    def remote_uri(self) -> str:
        """Remote SIP URI (e.g., sip:+1234567890@provider.com)."""
        return self._session_data.get("remote_uri", "")

    @property
    def direction(self) -> str:
        """Call direction: 'Inbound' or 'Outbound'."""
        return self._session_data.get("direction", "")

    @property
    def extra_headers(self) -> Dict[str, str]:
        """Custom SIP headers from the INVITE."""
        return self._session_data.get("extra_headers", {})

    # ── Audio control ────────────────────────────────────────────────────

    def mute(self) -> None:
        """Mute outgoing audio. RTP sends silence, queue preserved."""
        self._ep.mute(self._cid)

    def unmute(self) -> None:
        """Unmute outgoing audio."""
        self._ep.unmute(self._cid)

    def pause_playback(self) -> None:
        """Pause the RTP send loop. Queue accumulates, nothing sent."""
        self._ep.pause(self._cid)

    def resume_playback(self) -> None:
        """Resume the RTP send loop."""
        self._ep.resume(self._cid)

    def clear_buffer(self) -> None:
        """Clear local audio buffer (fires pending completion callbacks)."""
        self._ep.clear_buffer(self._cid)

    # ── Background audio ─────────────────────────────────────────────────

    def send_background_audio(self, audio: bytes, sample_rate: int, num_channels: int) -> None:
        """Send background audio to be mixed with agent voice in Rust's RTP send loop."""
        self._ep.send_background_audio(self._cid, audio, sample_rate, num_channels)

    # ── Flush / playout tracking ─────────────────────────────────────────

    def flush(self) -> None:
        """Mark current playback segment complete."""
        self._ep.flush(self._cid)

    async def wait_for_playout(self, timeout_ms: int = 5000) -> bool:
        """Wait for queued audio to finish playing.

        Returns True if playout completed, False if timed out.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._ep.wait_for_playout(self._cid, timeout_ms)
        )

    # ── Recording ────────────────────────────────────────────────────────

    def start_recording(self, path: str, stereo: bool = True) -> None:
        """Start recording the call to a WAV file.

        Args:
            path: Output file path (.wav).
            stereo: If True, record stereo (L=user, R=agent). Otherwise mono.
        """
        self._ep.start_recording(self._cid, path, stereo)

    def stop_recording(self) -> None:
        """Stop recording the call."""
        self._ep.stop_recording(self._cid)

    # ── DTMF ─────────────────────────────────────────────────────────────

    def send_dtmf(self, digits: str, method: str = "rfc2833") -> None:
        """Send DTMF digits.

        Args:
            digits: One or more DTMF digits (0-9, *, #, A-D).
            method: 'rfc2833' (in-band RTP) or 'sip_info' (SIP INFO).
        """
        self._ep.send_dtmf(self._cid, digits, method)

    # ── SIP call control ─────────────────────────────────────────────────

    def transfer(self, dest_uri: str) -> None:
        """Blind transfer — transfer call to another SIP URI."""
        self._ep.transfer(self._cid, dest_uri)

    def transfer_attended(self, target_session_id: str) -> None:
        """Attended transfer — transfer to an existing call."""
        self._ep.transfer_attended(self._cid, target_session_id)

    def hold(self) -> None:
        """Put the call on hold (SIP re-INVITE with a=sendonly)."""
        self._ep.hold(self._cid)

    def unhold(self) -> None:
        """Take the call off hold (SIP re-INVITE with a=sendrecv)."""
        self._ep.unhold(self._cid)

    def reject(self, code: int = 486) -> None:
        """Reject an incoming call with a SIP status code."""
        self._ep.reject(self._cid, code)

    def send_info(self, content_type: str = "application/json", body: str = "") -> None:
        """Send a SIP INFO message."""
        self._ep.send_info(self._cid, content_type, body)

    # ── Beep detection ───────────────────────────────────────────────────

    def detect_beep(self, timeout_ms: int = 30000, min_duration_ms: int = 80,
                    max_duration_ms: int = 5000) -> None:
        """Start voicemail beep detection.

        Fires on_beep_detected or on_beep_timeout event when done.
        """
        self._ep.detect_beep(self._cid, timeout_ms, min_duration_ms, max_duration_ms)

    def cancel_beep_detection(self) -> None:
        """Cancel ongoing beep detection."""
        self._ep.cancel_beep_detection(self._cid)

    # ── Raw message ──────────────────────────────────────────────────────

    def send_raw_message(self, content_type: str, body: str) -> None:
        """Send a raw SIP INFO message."""
        self._ep.send_info(self._cid, content_type, body)
