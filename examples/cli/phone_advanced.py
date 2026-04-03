#!/usr/bin/env python3
"""
CLI Phone Advanced — test SIP transport methods (flush, clear_buffer, wait_for_playout, pause, resume).

Simulates how LiveKit/Pipecat frameworks use the transport layer by playing
audio files through the SIP call and exercising segment lifecycle methods.

Prerequisites:
    cd crates/agent-transport-python && maturin develop
    pip install sounddevice numpy

Usage:
    SIP_USERNAME=xxx SIP_PASSWORD=yyy \
        python examples/cli_phone_advanced.py sip:+15551234567@phone.plivo.com

Keyboard controls during call:
    0-9, *, #   Send DTMF digit
    m / u       Mute / Unmute mic
    h / H       Hold / Unhold (SIP Re-INVITE sendonly/sendrecv)
    p / r       Pause / Resume playback (watch queue grow/drain in logs)

    --- Transport method tests (mic auto-paused during these) ---
    a           Play audio → flush → wait_for_playout (full segment lifecycle)
    i           Play audio → interrupt after 2s with clear_buffer
    s           Segment chain: clip1 → flush → wait → clip2 → flush → wait

    --- Manual controls ---
    f           Flush
    c           Clear buffer
    w           Wait for playout
    q / Enter   Hang up
"""

import os
import sys
import tty
import termios
import select
import threading
import time
import wave
import numpy as np
import sounddevice as sd
from agent_transport import SipEndpoint, AudioFrame, init_logging

SAMPLE_RATE = 8000
CHANNELS = 1
FRAME_SAMPLES = SAMPLE_RATE * 20 // 1000
DTMF_KEYS = set("0123456789*#")
AUDIO_DIR = os.path.join(os.path.dirname(__file__), "audio")


def load_wav_as_16k(path):
    """Load WAV file, resample to 16kHz mono int16."""
    with wave.open(path) as w:
        rate, nch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16)
    if nch > 1:
        samples = samples[::nch]
    if rate != SAMPLE_RATE:
        n_out = int(len(samples) / rate * SAMPLE_RATE)
        samples = np.interp(
            np.linspace(0, len(samples) - 1, n_out),
            np.arange(len(samples)),
            samples.astype(np.float64),
        ).astype(np.int16)
    return samples


def send_audio_frames(ep, session_id, samples, label=""):
    """Send PCM samples as 20ms frames. Returns (frames_sent, duration_s)."""
    sent = 0
    for i in range(0, len(samples), FRAME_SAMPLES):
        chunk = samples[i:i + FRAME_SAMPLES]
        if len(chunk) < FRAME_SAMPLES:
            chunk = np.pad(chunk, (0, FRAME_SAMPLES - len(chunk)))
        try:
            ep.send_audio(session_id, AudioFrame(chunk.tolist(), SAMPLE_RATE, CHANNELS))
            sent += 1
        except Exception:
            break
    dur = sent * 0.02
    if label:
        sys.stdout.write(f"\r  [{label}] Sent {sent} frames ({dur:.1f}s)\n")
        sys.stdout.flush()
    return sent, dur


def getch_nonblocking():
    if select.select([sys.stdin], [], [], 0.0)[0]:
        return sys.stdin.read(1)
    return None


def main():
    username = os.environ.get("SIP_USERNAME")
    password = os.environ.get("SIP_PASSWORD")
    sip_domain = os.environ.get("SIP_DOMAIN", "phone.plivo.com")
    dest_uri = sys.argv[1] if len(sys.argv) > 1 else None

    if not username or not password:
        print("Set SIP_USERNAME and SIP_PASSWORD environment variables.")
        sys.exit(1)

    init_logging(os.environ.get("RUST_LOG", "info"))

    ep = SipEndpoint(sip_server=sip_domain, log_level=3)
    print(f"Registering as {username}@{sip_domain}...")
    ep.register(username, password)

    event = ep.wait_for_event(timeout_ms=10000)
    if event is None or event["type"] != "registered":
        print(f"Registration failed: {event}")
        sys.exit(1)
    print("Registered.")

    if dest_uri:
        print(f"Calling {dest_uri}...")
        session_id = ep.call(dest_uri)
    else:
        print("Waiting for incoming call...")
        while True:
            event = ep.wait_for_event(timeout_ms=1000)
            if event and event["type"] == "incoming_call":
                session_id = event["session"]["session_id"]
                print(f"Incoming from {event['session']['remote_uri']}")
                ep.answer(session_id)
                break

    while True:
        event = ep.wait_for_event(timeout_ms=500)
        if event is None:
            continue
        if event["type"] == "call_media_active":
            break
        if event["type"] == "call_terminated":
            print(f"Call ended: {event.get('reason', '')}")
            ep.shutdown()
            return

    # Load test audio files
    long_audio = short_audio = greeting_audio = None
    for name, var in [("speech_female_8k.wav", "long"), ("caller_greeting_8k.wav", "greeting"), ("caller_detailed_8k.wav", "short")]:
        p = os.path.join(AUDIO_DIR, name)
        if os.path.exists(p):
            data = load_wav_as_16k(p)
            if var == "long": long_audio = data
            elif var == "greeting": greeting_audio = data
            elif var == "short": short_audio = data
    if long_audio is None and greeting_audio is None:
        print(f"  Warning: no audio files in {AUDIO_DIR}")

    print()
    print("=== CONNECTED ===")
    print("  0-9,*,#  DTMF    m/u Mute/Unmute    h/H Hold/Unhold    p/r Pause/Resume")
    print("  a Play+Flush+Wait    i Play+Interrupt    s Segment chain")
    print("  f Flush    c Clear    w Wait playout    q Hang up")
    print()

    running = threading.Event()
    running.set()
    mic_paused = threading.Event()
    mic_stopped = threading.Event()

    def stop_mic_and_drain():
        """Stop mic, wait for confirmation, drain stale frames."""
        mic_stopped.clear()
        mic_paused.set()
        mic_stopped.wait(timeout=0.1)
        ep.clear_buffer(session_id)

    def resume_mic():
        mic_stopped.clear()
        mic_paused.clear()

    # --- Mic ---
    def mic_thread_fn():
        sent = 0
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                blocksize=FRAME_SAMPLES, dtype="int16") as stream:
                while running.is_set():
                    data, _ = stream.read(FRAME_SAMPLES)
                    if mic_paused.is_set():
                        mic_stopped.set()
                        continue
                    try:
                        ep.send_audio(session_id, AudioFrame(data[:, 0].tolist(), SAMPLE_RATE, CHANNELS))
                        sent += 1
                        if sent % 250 == 0:
                            peak = int(np.max(np.abs(data[:, 0])))
                            q = ep.queued_frames(session_id) if running.is_set() else -1
                            sys.stdout.write(f"\r  [MIC] sent={sent} peak={peak} queued={q}\n")
                            sys.stdout.flush()
                    except Exception:
                        break
        except Exception:
            pass

    # --- Speaker ---
    def speaker_thread_fn():
        played = 0
        try:
            with sd.OutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                 blocksize=FRAME_SAMPLES, dtype="int16") as stream:
                while running.is_set():
                    try:
                        frame = ep.recv_audio_blocking(session_id, 20)
                    except Exception:
                        break
                    if frame is not None:
                        stream.write(np.array(frame.data, dtype=np.int16).reshape(-1, 1))
                        played += 1
                        if played % 250 == 0:
                            sys.stdout.write(f"\r  [SPK] played={played}\n")
                            sys.stdout.flush()
                    else:
                        stream.write(np.zeros((FRAME_SAMPLES, 1), dtype=np.int16))
        except Exception:
            pass

    # --- Events ---
    call_ended = threading.Event()

    def event_thread_fn():
        while running.is_set():
            event = ep.wait_for_event(timeout_ms=200)
            if event is None:
                continue
            if event["type"] == "call_terminated":
                sys.stdout.write(f"\r  Call ended: {event.get('reason', '')}\n")
                sys.stdout.flush()
                call_ended.set()
                break
            elif event["type"] == "dtmf_received":
                sys.stdout.write(f"\r  DTMF recv: {event['digit']}\n")
                sys.stdout.flush()

    for fn in [mic_thread_fn, speaker_thread_fn, event_thread_fn]:
        threading.Thread(target=fn, daemon=True).start()

    # --- Keyboard ---
    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setraw(sys.stdin.fileno())
        while running.is_set() and not call_ended.is_set():
            ch = getch_nonblocking()
            if ch is None:
                if call_ended.wait(timeout=0.1):
                    break
                continue

            out = sys.stdout.write
            flush = sys.stdout.flush

            try:
                if ch in DTMF_KEYS:
                    ep.send_dtmf(session_id, ch)
                    out(f"\r  DTMF sent: {ch}\n"); flush()

                elif ch == 'm':
                    ep.mute(session_id); out("\r  MUTED\n"); flush()
                elif ch == 'u':
                    ep.unmute(session_id); out("\r  UNMUTED\n"); flush()
                elif ch == 'h':
                    ep.hold(session_id); out("\r  HOLD — Re-INVITE sendonly\n"); flush()
                elif ch == 'H':
                    ep.unhold(session_id); out("\r  UNHOLD — Re-INVITE sendrecv\n"); flush()

                elif ch == 'p':
                    ep.pause(session_id)
                    q = ep.queued_frames(session_id)
                    out(f"\r  PAUSED — queued={q} ({q*0.02:.1f}s)\n"); flush()
                elif ch == 'r':
                    q = ep.queued_frames(session_id)
                    ep.resume(session_id)
                    out(f"\r  RESUMED — draining {q} frames ({q*0.02:.1f}s)\n"); flush()

                elif ch == 'f':
                    ep.flush(session_id)
                    out(f"\r  FLUSH — queued={ep.queued_frames(session_id)}\n"); flush()
                elif ch == 'c':
                    q = ep.queued_frames(session_id)
                    ep.clear_buffer(session_id)
                    out(f"\r  CLEAR — discarded {q} frames\n"); flush()
                elif ch == 'w':
                    q = ep.queued_frames(session_id)
                    out(f"\r  WAIT (queued={q})...\n"); flush()
                    t0 = time.time()
                    ok = ep.wait_for_playout(session_id, 30000)
                    out(f"\r  PLAYOUT {'done' if ok else 'timeout'} in {time.time()-t0:.2f}s\n"); flush()

                # --- Audio playback tests ---

                elif ch == 'a':
                    audio = long_audio if long_audio is not None else greeting_audio
                    if audio is None:
                        out("\r  No audio files\n"); flush(); continue
                    out("\r  --- Play + Flush + Wait ---\n"); flush()
                    stop_mic_and_drain()
                    frames, dur = send_audio_frames(ep, session_id, audio, "PLAY")
                    ep.flush(session_id)
                    q = ep.queued_frames(session_id)
                    out(f"\r  [FLUSH] queued={q} ({q*0.02:.1f}s)\n"); flush()
                    t0 = time.time()
                    ok = ep.wait_for_playout(session_id, 30000)
                    out(f"\r  [PLAYOUT] {'done' if ok else 'timeout'} — {time.time()-t0:.2f}s for {dur:.1f}s audio\n"); flush()
                    resume_mic()

                elif ch == 'i':
                    audio = long_audio if long_audio is not None else greeting_audio
                    if audio is None:
                        out("\r  No audio files\n"); flush(); continue
                    out("\r  --- Play + Interrupt after 2s ---\n"); flush()
                    stop_mic_and_drain()
                    frames, dur = send_audio_frames(ep, session_id, audio, "PLAY")
                    q0 = ep.queued_frames(session_id)
                    out(f"\r  [QUEUED] {q0} — interrupting in 2s...\n"); flush()
                    time.sleep(2.0)
                    q1 = ep.queued_frames(session_id)
                    ep.clear_buffer(session_id)
                    time.sleep(0.05)
                    q2 = ep.queued_frames(session_id)
                    out(f"\r  [INTERRUPT] played ~{(q0-q1)*0.02:.1f}s, discarded ~{q1*0.02:.1f}s (queue: {q0}→{q1}→{q2})\n"); flush()
                    resume_mic()

                elif ch == 's':
                    clip1 = greeting_audio
                    clip2 = short_audio if short_audio is not None else greeting_audio
                    if clip1 is None:
                        out("\r  No audio files\n"); flush(); continue
                    out("\r  --- Segment chain ---\n"); flush()
                    stop_mic_and_drain()
                    try:
                        for idx, clip in enumerate([clip1, clip2], 1):
                            label = f"SEG{idx}"
                            f, d = send_audio_frames(ep, session_id, clip, label)
                            ep.flush(session_id)
                            q = ep.queued_frames(session_id)
                            out(f"\r  [{label}] flushed, queued={q}, waiting...\n"); flush()
                            t0 = time.time()
                            ok = ep.wait_for_playout(session_id, 30000)
                            out(f"\r  [{label}] {'done' if ok else 'timeout'} in {time.time()-t0:.2f}s ({d:.1f}s)\n"); flush()
                        out("\r  [CHAIN] Complete\n"); flush()
                    except Exception as e:
                        out(f"\r  [CHAIN ERROR] {e}\n"); flush()
                    resume_mic()

                elif ch in ('q', '\r', '\n', '\x03'):
                    out("\r  Hanging up...\n"); flush()
                    ep.hangup(session_id)
                    break

            except Exception as e:
                out(f"\r  [ERROR] {e}\n"); flush()

    except Exception as e:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        import traceback
        print(f"\n  [CRASH] {e}")
        traceback.print_exc()
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

    running.clear()
    ep.shutdown()
    print("Done.")


if __name__ == "__main__":
    main()
