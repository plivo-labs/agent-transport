#!/usr/bin/env python3
"""
SIP e2e smoke for CI.

This script follows the same SIP/audio flow as the CLI phone examples, but keeps
CI policy here: E2E_* environment names, bounded waits, strict exit codes, and a
received WAV artifact.
"""

import argparse
import importlib
import math
import os
import struct
import sys
import threading
import time
import wave
from pathlib import Path

SAMPLE_RATE = 8000
CHANNELS = 1
FRAME_SAMPLES = SAMPLE_RATE * 20 // 1000
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLIP = REPO_ROOT / "examples" / "audio" / "caller_greeting_8k.wav"
DEFAULT_OUTPUT = Path("/tmp/received_audio.wav")
DEFAULT_CALLER_OUTPUT = Path("/tmp/received_audio_caller.wav")


class E2EFailure(RuntimeError):
    pass


def load_agent_transport():
    module = importlib.import_module("agent_transport")
    return module.SipEndpoint, module.AudioFrame, module.init_logging


def load_wav(path):
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())

    n_samples = len(raw) // 2
    samples = list(struct.unpack(f"<{n_samples}h", raw))
    if channels > 1:
        samples = samples[::channels]

    if rate != SAMPLE_RATE:
        n_out = int(len(samples) / rate * SAMPLE_RATE)
        samples = [
            int(
                samples[int(i * (len(samples) - 1) / max(1, n_out - 1))]
            )
            for i in range(n_out)
        ]

    return samples


def save_wav(path, samples):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def audio_stats(samples):
    if not samples:
        return 0, 0
    peak = max(abs(s) for s in samples)
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return peak, rms


def session_id_from_event(event):
    session_id = event.get("session_id")
    if session_id:
        return session_id
    session = event.get("session")
    if isinstance(session, dict):
        return session.get("session_id")
    return getattr(session, "session_id", None)


def wait_for_event_type(ep, event_type, timeout_s, label="event", expected_session_id=None):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        event = ep.wait_for_event(timeout_ms=500)
        if event is None:
            continue
        current_type = event.get("type")
        event_session_id = session_id_from_event(event)
        print(f"[{label}] event={current_type}")
        if current_type == event_type:
            if expected_session_id and event_session_id != expected_session_id:
                print(f"[{label}] ignoring {event_type} for session_id={event_session_id}")
                continue
            return event
        if current_type == "registration_failed":
            raise E2EFailure(f"Registration failed: {event.get('error') or event}")
        if current_type == "call_terminated":
            if expected_session_id and event_session_id != expected_session_id:
                print(f"[{label}] ignoring call_terminated for session_id={event_session_id}")
                continue
            raise E2EFailure(f"Call terminated before {event_type}: {event.get('reason', '')}")
    raise E2EFailure(f"Timed out after {timeout_s:.0f}s waiting for {event_type}")


def send_frames_realtime(ep, AudioFrame, session_id, samples, label, call_ended):
    peak, rms = audio_stats(samples)
    duration = len(samples) / SAMPLE_RATE
    print(f"[{label}] sending {duration:.1f}s audio (peak={peak}, rms={rms:.0f})")

    frames_sent = 0
    for i in range(0, len(samples), FRAME_SAMPLES):
        if call_ended.is_set():
            raise E2EFailure(f"Call ended while sending {label}")
        chunk = samples[i:i + FRAME_SAMPLES]
        if len(chunk) < FRAME_SAMPLES:
            chunk = chunk + [0] * (FRAME_SAMPLES - len(chunk))
        try:
            ep.send_audio(session_id, AudioFrame(chunk, SAMPLE_RATE, CHANNELS))
            frames_sent += 1
        except Exception as exc:
            raise E2EFailure(f"{label} send failed at frame {frames_sent}: {exc}") from exc
        time.sleep(0.02)

    if frames_sent == 0:
        raise E2EFailure(f"{label} sent no frames")
    print(f"[{label}] sent {frames_sent} frames")


def send_silence(ep, AudioFrame, session_id, seconds, call_ended):
    silence = [0] * FRAME_SAMPLES
    for frame_idx in range(int(seconds / 0.02)):
        if call_ended.is_set():
            break
        try:
            ep.send_audio(session_id, AudioFrame(silence, SAMPLE_RATE, CHANNELS))
        except Exception as exc:
            raise E2EFailure(f"silence send failed at frame {frame_idx}: {exc}") from exc
        time.sleep(0.02)


def start_receivers(ep, session_id, label="recv"):
    running = threading.Event()
    running.set()
    call_ended = threading.Event()
    recv_lock = threading.Lock()
    received_samples = []
    received_dtmf = []
    errors = []

    def recv_loop():
        frame_count = 0
        while running.is_set() and not call_ended.is_set():
            try:
                result = ep.recv_audio_bytes_blocking(session_id, 20)
            except Exception as exc:
                if "call not active" in str(exc).lower():
                    call_ended.set()
                elif running.is_set() and not call_ended.is_set():
                    errors.append(f"recv_audio_bytes_blocking failed: {exc}")
                break
            if result is None:
                continue
            audio_bytes, sample_rate, _ = result
            n_samples = len(audio_bytes) // 2
            if n_samples == 0:
                continue
            samples = list(struct.unpack(f"<{n_samples}h", bytes(audio_bytes)))
            with recv_lock:
                received_samples.extend(samples)
            frame_count += 1
            if frame_count == 1:
                print(f"[{label}/recv] first frame: {n_samples} samples at {sample_rate}Hz")
            elif frame_count % 500 == 0:
                with recv_lock:
                    duration = len(received_samples) / SAMPLE_RATE
                _, rms = audio_stats(samples)
                print(f"[{label}/recv] {frame_count} frames ({duration:.1f}s total, rms={rms:.0f})")

    def event_loop():
        while running.is_set() and not call_ended.is_set():
            try:
                event = ep.wait_for_event(timeout_ms=200)
            except Exception as exc:
                errors.append(f"wait_for_event failed: {exc}")
                break
            if event is None:
                continue
            if event.get("type") == "call_terminated":
                event_session_id = session_id_from_event(event)
                if event_session_id and event_session_id != session_id:
                    print(f"[{label}/event] ignoring call end for session_id={event_session_id}")
                    continue
                print(f"[{label}/event] call ended: {event.get('reason', '')}")
                call_ended.set()
                break
            if event.get("type") == "dtmf_received":
                event_session_id = session_id_from_event(event)
                if not event_session_id or event_session_id == session_id:
                    digit = event.get("digit")
                    print(f"[{label}/event] DTMF received: {digit}")
                    with recv_lock:
                        received_dtmf.append(digit)

    threading.Thread(target=recv_loop, daemon=True).start()
    threading.Thread(target=event_loop, daemon=True).start()
    return running, call_ended, recv_lock, received_samples, errors, received_dtmf


def wait_for_received_audio(samples, recv_lock, min_seconds, timeout_s, call_ended, label="recv"):
    if timeout_s <= 0:
        raise E2EFailure(f"No remaining call budget to wait for {min_seconds:.1f}s of audio on {label}")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if call_ended.is_set():
            raise E2EFailure(f"Call ended before receiving {min_seconds:.1f}s of audio on {label}")
        with recv_lock:
            duration = len(samples) / SAMPLE_RATE
        if duration >= min_seconds:
            return
        time.sleep(0.1)
    raise E2EFailure(f"Timed out waiting for {min_seconds:.1f}s of received audio on {label}")


def wait_for_dtmf_received(received_dtmf, recv_lock, expected_digit, timeout_s, call_ended, label="recv"):
    if timeout_s <= 0:
        raise E2EFailure(f"No remaining call budget to wait for DTMF {expected_digit} on {label}")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if call_ended.is_set():
            raise E2EFailure(f"Call ended before DTMF {expected_digit} arrived on {label}")
        with recv_lock:
            received = list(received_dtmf)
        if any(str(d) == str(expected_digit) for d in received):
            return
        time.sleep(0.05)
    with recv_lock:
        seen = list(received_dtmf)
    raise E2EFailure(
        f"Timed out waiting for DTMF {expected_digit} on {label}; received={seen}"
    )


def analyze(samples, output_path, speech_threshold, label="received"):
    save_wav(output_path, samples)
    peak, rms = audio_stats(samples)
    duration = len(samples) / SAMPLE_RATE
    speech_samples = sum(1 for sample in samples if abs(sample) > speech_threshold)
    speech_percent = 100 * speech_samples / len(samples) if samples else 0.0
    print(f"[{label}] received={duration:.2f}s peak={peak} rms={rms:.0f} speech={speech_percent:.2f}%")
    print(f"[{label}] artifact={output_path}")
    return duration, speech_percent


def e2e_env(args):
    env = {
        "a": {
            "username": os.environ.get("E2E_SIP_USERNAME_A"),
            "password": os.environ.get("E2E_SIP_PASSWORD_A"),
            "dest_uri": os.environ.get("E2E_SIP_DEST_URI_A"),
        },
        "b": {
            "username": os.environ.get("E2E_SIP_USERNAME_B"),
            "password": os.environ.get("E2E_SIP_PASSWORD_B"),
            "dest_uri": os.environ.get("E2E_SIP_DEST_URI_B"),
        },
    }
    sip_domain = os.environ.get("E2E_SIP_DOMAIN") or "phone.plivo.com"
    rust_log = os.environ.get("E2E_RUST_LOG", "info")
    required = [
        ("E2E_SIP_USERNAME_A", env["a"]["username"]),
        ("E2E_SIP_PASSWORD_A", env["a"]["password"]),
    ]
    if args.direction == "outbound":
        required.append(("E2E_SIP_DEST_URI_A", env["a"]["dest_uri"]))
    else:
        required.extend([
            ("E2E_SIP_USERNAME_B", env["b"]["username"]),
            ("E2E_SIP_PASSWORD_B", env["b"]["password"]),
            ("E2E_SIP_DEST_URI_B", env["b"]["dest_uri"]),
        ])
    missing = [name for name, value in required if not value]
    if missing and not args.dry_run:
        raise E2EFailure(f"Missing required env vars: {', '.join(missing)}")
    return env, sip_domain, rust_log


def parse_args(argv):
    parser = argparse.ArgumentParser(description="SIP e2e smoke")
    parser.add_argument("--ci", action="store_true", help="Accepted for workflow readability")
    parser.add_argument(
        "--direction",
        choices=["outbound", "inbound"],
        default="outbound",
        help="outbound uses account A; inbound registers A as receiver and B as caller",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate CLI/env shape without dialing")
    parser.add_argument("--clip", type=Path, default=DEFAULT_CLIP)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--caller-output", type=Path, default=DEFAULT_CALLER_OUTPUT)
    parser.add_argument("--max-register-seconds", type=float, default=10.0)
    parser.add_argument("--max-answer-seconds", type=float, default=30.0)
    parser.add_argument("--max-call-seconds", type=float, default=90.0)
    parser.add_argument("--max-hangup-seconds", type=float, default=10.0)
    parser.add_argument("--call-attempts", type=int, default=2)
    parser.add_argument("--call-retry-seconds", type=float, default=3.0)
    parser.add_argument("--min-received-seconds", type=float, default=1.0)
    parser.add_argument("--min-speech-percent", type=float, default=1.0)
    parser.add_argument("--speech-threshold", type=int, default=500)
    parser.add_argument("--dtmf-digit", default="1")
    return parser.parse_args(argv)


def assert_analyzed_audio(samples, output_path, args, label):
    duration, speech_percent = analyze(samples, output_path, args.speech_threshold, label)
    if duration < args.min_received_seconds:
        raise E2EFailure(
            f"{label} audio duration {duration:.2f}s is below {args.min_received_seconds:.2f}s"
        )
    if speech_percent < args.min_speech_percent:
        raise E2EFailure(
            f"{label} speech percent {speech_percent:.2f}% is below {args.min_speech_percent:.2f}%"
        )


def call_with_retries(ep, dest_uri, args, label):
    attempts = max(1, args.call_attempts)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return ep.call(dest_uri)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            print(f"[{label}] call attempt {attempt} failed: {exc}; retrying")
            time.sleep(args.call_retry_seconds)
    raise E2EFailure(f"{label} call initiation failed: {last_error}")


def stop_receiver(receiver):
    if receiver is not None:
        running, _, _, _, _, _ = receiver
        running.clear()


def receiver_errors(receiver):
    if receiver is None:
        return []
    return receiver[4]


def receiver_samples(receiver):
    if receiver is None:
        return []
    _, _, recv_lock, received_samples, _, _ = receiver
    with recv_lock:
        return list(received_samples)


def run_outbound(args, SipEndpoint, AudioFrame, env, sip_domain, clip):
    account = env["a"]
    ep = None
    receiver = None

    try:
        ep = SipEndpoint(sip_server=sip_domain, log_level=3)
        print("registering SIP account A")
        ep.register(account["username"], account["password"])
        wait_for_event_type(ep, "registered", args.max_register_seconds, "account-a")
        print("registered account A")

        print("account A calling E2E_SIP_DEST_URI_A")
        session_id = call_with_retries(ep, account["dest_uri"], args, "account-a")
        wait_for_event_type(
            ep,
            "call_answered",
            args.max_answer_seconds,
            "account-a",
            expected_session_id=session_id,
        )
        print(f"connected session_id={session_id}")

        receiver = start_receivers(ep, session_id, "account-a")
        _, call_ended, recv_lock, received_samples, _, _ = receiver
        deadline = time.monotonic() + args.max_call_seconds

        send_silence(ep, AudioFrame, session_id, 0.5, call_ended)
        send_frames_realtime(ep, AudioFrame, session_id, clip, "a-outbound-clip", call_ended)

        print(f"sending DTMF {args.dtmf_digit}")
        try:
            ep.send_dtmf(session_id, args.dtmf_digit)
        except Exception as exc:
            raise E2EFailure(f"DTMF send failed: {exc}") from exc

        wait_for_received_audio(
            received_samples,
            recv_lock,
            args.min_received_seconds,
            min(15.0, max(0.0, deadline - time.monotonic())),
            call_ended,
            "account-a",
        )
        send_silence(ep, AudioFrame, session_id, 1.0, call_ended)

        if call_ended.is_set():
            raise E2EFailure("Call ended before scripted hangup")
        print("hanging up")
        try:
            ep.hangup(session_id)
        except Exception as exc:
            raise E2EFailure(f"Hangup failed: {exc}") from exc
        if not call_ended.wait(timeout=args.max_hangup_seconds):
            raise E2EFailure("Timed out waiting for call termination after hangup")
    finally:
        stop_receiver(receiver)
        if receiver is not None:
            time.sleep(0.5)
        if ep is not None:
            try:
                ep.shutdown()
            except Exception as exc:
                print(f"warning: shutdown failed: {exc}")

    errors = receiver_errors(receiver)
    if errors:
        raise E2EFailure("; ".join(errors))
    assert_analyzed_audio(receiver_samples(receiver), args.output, args, "account-a")


def run_inbound(args, SipEndpoint, AudioFrame, env, sip_domain, clip):
    receiver_ep = None
    caller_ep = None
    receiver = None
    caller_receiver = None

    try:
        receiver_ep = SipEndpoint(sip_server=sip_domain, log_level=3)
        caller_ep = SipEndpoint(sip_server=sip_domain, log_level=3)

        print("registering receiver account A")
        receiver_ep.register(env["a"]["username"], env["a"]["password"])
        wait_for_event_type(receiver_ep, "registered", args.max_register_seconds, "receiver-a")
        print("registered receiver account A")

        print("registering caller account B")
        caller_ep.register(env["b"]["username"], env["b"]["password"])
        wait_for_event_type(caller_ep, "registered", args.max_register_seconds, "caller-b")
        print("registered caller account B")

        print("caller B calling E2E_SIP_DEST_URI_B")
        caller_session_id = call_with_retries(caller_ep, env["b"]["dest_uri"], args, "caller-b")

        ringing = wait_for_event_type(receiver_ep, "call_ringing", args.max_answer_seconds, "receiver-a")
        receiver_session_id = session_id_from_event(ringing)
        answered = wait_for_event_type(
            receiver_ep,
            "call_answered",
            args.max_answer_seconds,
            "receiver-a",
            expected_session_id=receiver_session_id,
        )
        receiver_session_id = session_id_from_event(answered) or receiver_session_id
        caller_answered = wait_for_event_type(
            caller_ep,
            "call_answered",
            args.max_answer_seconds,
            "caller-b",
            expected_session_id=caller_session_id,
        )
        caller_session_id = session_id_from_event(caller_answered) or caller_session_id

        if not receiver_session_id:
            raise E2EFailure("Inbound receiver did not expose a session id")
        if not caller_session_id:
            raise E2EFailure("Inbound caller did not expose a session id")
        print(f"connected receiver_session_id={receiver_session_id} caller_session_id={caller_session_id}")

        receiver = start_receivers(receiver_ep, receiver_session_id, "receiver-a")
        caller_receiver = start_receivers(caller_ep, caller_session_id, "caller-b")
        _, receiver_call_ended, receiver_lock, receiver_samples_list, _, receiver_dtmf = receiver
        _, caller_call_ended, caller_lock, caller_samples_list, _, _ = caller_receiver
        deadline = time.monotonic() + args.max_call_seconds

        send_silence(caller_ep, AudioFrame, caller_session_id, 0.5, caller_call_ended)
        send_frames_realtime(caller_ep, AudioFrame, caller_session_id, clip, "b-to-a-clip", caller_call_ended)
        wait_for_received_audio(
            receiver_samples_list,
            receiver_lock,
            args.min_received_seconds,
            min(15.0, max(0.0, deadline - time.monotonic())),
            receiver_call_ended,
            "receiver-a",
        )

        send_silence(receiver_ep, AudioFrame, receiver_session_id, 0.5, receiver_call_ended)
        send_frames_realtime(receiver_ep, AudioFrame, receiver_session_id, clip, "a-to-b-clip", receiver_call_ended)
        wait_for_received_audio(
            caller_samples_list,
            caller_lock,
            args.min_received_seconds,
            min(15.0, max(0.0, deadline - time.monotonic())),
            caller_call_ended,
            "caller-b",
        )

        print(f"caller B sending DTMF {args.dtmf_digit}")
        try:
            caller_ep.send_dtmf(caller_session_id, args.dtmf_digit)
        except Exception as exc:
            raise E2EFailure(f"Inbound DTMF send failed: {exc}") from exc

        send_silence(caller_ep, AudioFrame, caller_session_id, 0.5, caller_call_ended)
        send_silence(receiver_ep, AudioFrame, receiver_session_id, 0.5, receiver_call_ended)

        wait_for_dtmf_received(
            receiver_dtmf,
            receiver_lock,
            args.dtmf_digit,
            min(5.0, max(0.0, deadline - time.monotonic())),
            receiver_call_ended,
            "receiver-a",
        )

        if caller_call_ended.is_set() or receiver_call_ended.is_set():
            raise E2EFailure("Inbound call ended before scripted hangup")
        print("caller B hanging up")
        try:
            caller_ep.hangup(caller_session_id)
        except Exception as exc:
            raise E2EFailure(f"Inbound hangup failed: {exc}") from exc
        hangup_deadline = time.monotonic() + args.max_hangup_seconds
        for waiter_label, waiter_event in (
            ("receiver-a", receiver_call_ended),
            ("caller-b", caller_call_ended),
        ):
            remaining = max(0.0, hangup_deadline - time.monotonic())
            if not waiter_event.wait(timeout=remaining):
                raise E2EFailure(
                    f"Timed out waiting for {waiter_label} call termination after hangup"
                )
    finally:
        stop_receiver(receiver)
        stop_receiver(caller_receiver)
        if receiver is not None or caller_receiver is not None:
            time.sleep(0.5)
        for label, ep in [("caller", caller_ep), ("receiver", receiver_ep)]:
            if ep is None:
                continue
            try:
                ep.shutdown()
            except Exception as exc:
                print(f"warning: {label} shutdown failed: {exc}")

    errors = receiver_errors(receiver) + receiver_errors(caller_receiver)
    if errors:
        raise E2EFailure("; ".join(errors))
    assert_analyzed_audio(receiver_samples(receiver), args.output, args, "receiver-a")
    assert_analyzed_audio(receiver_samples(caller_receiver), args.caller_output, args, "caller-b")


def run(args):
    env, sip_domain, rust_log = e2e_env(args)
    print(f"dry_run={args.dry_run}")
    print(f"direction={args.direction}")
    print("sip env loaded")
    print(f"clip={args.clip}")
    print(f"output={args.output}")
    if args.direction == "inbound":
        print(f"caller_output={args.caller_output}")

    if args.dry_run:
        print("dry run passed")
        return 0

    SipEndpoint, AudioFrame, init_logging = load_agent_transport()
    init_logging(rust_log)

    if not args.clip.exists():
        raise E2EFailure(f"Audio clip not found: {args.clip}")
    clip = load_wav(args.clip)

    if args.direction == "outbound":
        run_outbound(args, SipEndpoint, AudioFrame, env, sip_domain, clip)
    else:
        run_inbound(args, SipEndpoint, AudioFrame, env, sip_domain, clip)

    print("e2e smoke passed")
    return 0


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return run(args)
    except E2EFailure as exc:
        print(f"E2E FAILED: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
