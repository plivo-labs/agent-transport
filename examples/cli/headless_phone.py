#!/usr/bin/env python3
"""
Headless CLI Phone — multi-turn SIP test without a sound card.

Sends audio from WAV files, captures received audio to file.
Runs a realistic multi-turn conversation with interruptions.
Designed for VPS / CI testing.

Usage:
    SIP_USERNAME=xxx SIP_PASSWORD=yyy \
        python examples/cli/headless_phone.py sip:DEST@phone.plivo.com

    # Wait for incoming call:
    SIP_USERNAME=xxx SIP_PASSWORD=yyy \
        python examples/cli/headless_phone.py
"""

import os
import sys
import threading
import time
import wave
import struct
import math
from agent_transport import SipEndpoint, AudioFrame, init_logging

SAMPLE_RATE = 8000
CHANNELS = 1
FRAME_SAMPLES = SAMPLE_RATE * 20 // 1000  # 160 samples per 20ms

AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "audio")


def load_wav(path):
    """Load WAV file as int16 samples, resample to 8kHz if needed."""
    with wave.open(path) as w:
        rate = w.getframerate()
        nch = w.getnchannels()
        raw = w.readframes(w.getnframes())

    n_samples = len(raw) // 2
    samples = list(struct.unpack(f"<{n_samples}h", raw))

    if nch > 1:
        samples = samples[::nch]

    if rate != SAMPLE_RATE:
        n_out = int(len(samples) / rate * SAMPLE_RATE)
        resampled = []
        for i in range(n_out):
            src = i * (len(samples) - 1) / (n_out - 1)
            idx = int(src)
            frac = src - idx
            if idx + 1 < len(samples):
                val = samples[idx] * (1 - frac) + samples[idx + 1] * frac
            else:
                val = samples[idx]
            resampled.append(int(val))
        samples = resampled

    return samples


def save_wav(path, samples, sample_rate=SAMPLE_RATE):
    """Save int16 samples to WAV."""
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def audio_stats(samples):
    """Return peak and RMS of samples."""
    if not samples:
        return 0, 0
    peak = max(abs(s) for s in samples)
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return peak, rms


def send_audio_realtime(ep, session_id, samples, label, call_ended):
    """Send samples at real-time pace (20ms per frame). Returns frames sent."""
    peak, rms = audio_stats(samples)
    dur = len(samples) / SAMPLE_RATE
    print(f"  [{label}] Sending {dur:.1f}s audio (peak={peak}, rms={rms:.0f})")

    frames_sent = 0
    for i in range(0, len(samples), FRAME_SAMPLES):
        if call_ended.is_set():
            break
        chunk = samples[i:i + FRAME_SAMPLES]
        if len(chunk) < FRAME_SAMPLES:
            chunk = chunk + [0] * (FRAME_SAMPLES - len(chunk))
        try:
            ep.send_audio(session_id, AudioFrame(chunk, SAMPLE_RATE, CHANNELS))
            frames_sent += 1
        except Exception as e:
            print(f"  [{label}] Send error at frame {frames_sent}: {e}")
            break
        time.sleep(0.02)

    print(f"  [{label}] Sent {frames_sent} frames ({frames_sent * 0.02:.1f}s)")
    return frames_sent


def send_silence(ep, session_id, duration_s, call_ended):
    """Send silence at real-time pace."""
    n_frames = int(duration_s / 0.02)
    silence = [0] * FRAME_SAMPLES
    for _ in range(n_frames):
        if call_ended.is_set():
            break
        try:
            ep.send_audio(session_id, AudioFrame(silence, SAMPLE_RATE, CHANNELS))
        except Exception:
            break
        time.sleep(0.02)


def wait_for_speech(recv_samples, recv_lock, timeout_s, threshold=300):
    """Wait until we detect speech in received audio (RMS above threshold)."""
    start = time.time()
    last_len = 0
    while time.time() - start < timeout_s:
        with recv_lock:
            current_len = len(recv_samples)
        if current_len > last_len + SAMPLE_RATE:  # check every ~1s of new audio
            with recv_lock:
                recent = recv_samples[last_len:]
            _, rms = audio_stats(recent)
            if rms > threshold:
                return True
            last_len = current_len
        time.sleep(0.1)
    return False


def wait_for_silence(recv_samples, recv_lock, timeout_s, silence_duration=1.0, threshold=200):
    """Wait until received audio goes silent (agent stopped speaking)."""
    start = time.time()
    silence_start = None
    last_check = 0
    while time.time() - start < timeout_s:
        with recv_lock:
            current_len = len(recv_samples)
        # Check last 200ms of audio
        check_samples = int(SAMPLE_RATE * 0.2)
        if current_len > last_check + check_samples:
            with recv_lock:
                recent = recv_samples[-check_samples:]
            _, rms = audio_stats(recent)
            last_check = current_len
            if rms < threshold:
                if silence_start is None:
                    silence_start = time.time()
                elif time.time() - silence_start >= silence_duration:
                    return True
            else:
                silence_start = None
        time.sleep(0.1)
    return False


def main():
    username = os.environ.get("SIP_USERNAME")
    password = os.environ.get("SIP_PASSWORD")
    sip_domain = os.environ.get("SIP_DOMAIN", "phone.plivo.com")
    dest_uri = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else None

    if not username or not password:
        print("Set SIP_USERNAME and SIP_PASSWORD environment variables.")
        sys.exit(1)

    init_logging(os.environ.get("RUST_LOG", "info"))

    # Load all available audio clips
    clips = {}
    clip_files = {
        "greeting": "caller_greeting_8k.wav",      # ~3.7s "Hello, is anyone there?"
        "detailed": "caller_detailed_8k.wav",       # ~13s detailed speech
        "followup": "caller_followup_8k.wav",       # followup question
        "confirm": "caller_confirm_8k.wav",         # confirmation
        "goodbye": "caller_goodbye_8k.wav",         # goodbye
        "interrupt": "caller_interrupt_8k.wav",      # interruption phrase
        "female": "speech_female_8k.wav",           # ~14.8s female speech
    }
    for key, filename in clip_files.items():
        p = os.path.join(AUDIO_DIR, filename)
        if os.path.exists(p):
            clips[key] = load_wav(p)
            peak, rms = audio_stats(clips[key])
            print(f"  Loaded {key}: {filename} ({len(clips[key])/SAMPLE_RATE:.1f}s, peak={peak}, rms={rms:.0f})")

    if not clips:
        print(f"No audio files found in {AUDIO_DIR}")
        sys.exit(1)

    # Init SIP
    ep = SipEndpoint(sip_server=sip_domain, log_level=3)
    print(f"\nRegistering as {username}@{sip_domain}...")
    ep.register(username, password)

    event = ep.wait_for_event(timeout_ms=10000)
    if event is None or event["type"] != "registered":
        print(f"Registration failed: {event}")
        sys.exit(1)
    print("Registered.")

    # Place or receive call
    if dest_uri:
        print(f"Calling {dest_uri}...")
        session_id = ep.call(dest_uri)
        print(f"Call initiated (session_id={session_id})")
    else:
        print("Waiting for incoming call...")
        while True:
            event = ep.wait_for_event(timeout_ms=1000)
            if event and event["type"] == "incoming_call":
                session_id = event["session"]["session_id"]
                print(f"Incoming from {event['session']['remote_uri']}")
                ep.answer(session_id)
                break

    # Wait for media
    while True:
        event = ep.wait_for_event(timeout_ms=500)
        if event is None:
            continue
        if event["type"] == "call_media_active":
            break
        if event["type"] == "call_terminated":
            print(f"Call ended before media: {event.get('reason', '')}")
            ep.shutdown()
            return

    print("\n" + "=" * 60)
    print("  CONNECTED — Starting multi-turn test")
    print("=" * 60 + "\n")

    running = threading.Event()
    running.set()
    call_ended = threading.Event()
    received_samples = []
    recv_lock = threading.Lock()

    # --- Receive thread ---
    def recv_thread():
        count = 0
        while running.is_set() and not call_ended.is_set():
            try:
                result = ep.recv_audio_bytes_blocking(session_id, 20)
            except Exception:
                break
            if result is not None:
                audio_bytes, sr, nc = result
                n = len(audio_bytes) // 2
                if n > 0:
                    samples = list(struct.unpack(f"<{n}h", bytes(audio_bytes)))
                    with recv_lock:
                        received_samples.extend(samples)
                    count += 1
                    if count == 1:
                        print(f"  [RECV] First frame: {n} samples, sr={sr}")
                    elif count % 500 == 0:
                        with recv_lock:
                            total = len(received_samples)
                        _, rms = audio_stats(samples)
                        print(f"  [RECV] {count} frames ({total/SAMPLE_RATE:.1f}s total, current_rms={rms:.0f})")

    # --- Event thread ---
    def event_thread():
        while running.is_set() and not call_ended.is_set():
            event = ep.wait_for_event(timeout_ms=200)
            if event is None:
                continue
            if event["type"] == "call_terminated":
                print(f"\n  [EVENT] Call ended: {event.get('reason', '')}")
                call_ended.set()
                break
            elif event["type"] == "dtmf_received":
                print(f"  [EVENT] DTMF: {event['digit']}")

    threading.Thread(target=recv_thread, daemon=True).start()
    threading.Thread(target=event_thread, daemon=True).start()

    # ──────────────────────────────────────────────────────────
    # TURN 1: Wait for agent greeting, then respond
    # ──────────────────────────────────────────────────────────
    print("\n--- TURN 1: Agent greets, we respond ---")
    print("  Waiting for agent greeting...")
    # Send silence while waiting (keeps RTP flowing)
    send_silence(ep, session_id, 4.0, call_ended)

    # Wait for agent to finish greeting
    wait_for_silence(received_samples, recv_lock, timeout_s=8.0, silence_duration=0.8)

    if call_ended.is_set():
        print("  Call ended during turn 1")
    else:
        # Send our greeting
        clip = clips.get("greeting") or clips.get("female") or list(clips.values())[0]
        send_audio_realtime(ep, session_id, clip, "TURN1-SEND", call_ended)
        # Brief pause after speaking
        send_silence(ep, session_id, 0.5, call_ended)

    # ──────────────────────────────────────────────────────────
    # TURN 2: Wait for agent response, then ask follow-up
    # ──────────────────────────────────────────────────────────
    if not call_ended.is_set():
        print("\n--- TURN 2: Agent responds, we ask follow-up ---")
        print("  Waiting for agent to finish speaking...")
        # Wait for agent speech then silence
        wait_for_speech(received_samples, recv_lock, timeout_s=10.0)
        wait_for_silence(received_samples, recv_lock, timeout_s=15.0, silence_duration=1.0)

        if not call_ended.is_set():
            clip = clips.get("followup") or clips.get("detailed") or clips.get("female") or list(clips.values())[0]
            send_audio_realtime(ep, session_id, clip, "TURN2-SEND", call_ended)
            send_silence(ep, session_id, 0.5, call_ended)

    # ──────────────────────────────────────────────────────────
    # TURN 3: INTERRUPT — speak while agent is still talking
    # ──────────────────────────────────────────────────────────
    if not call_ended.is_set():
        print("\n--- TURN 3: INTERRUPT — speak while agent is talking ---")
        print("  Waiting for agent to START speaking...")
        # Wait for agent speech to begin
        wait_for_speech(received_samples, recv_lock, timeout_s=10.0)

        if not call_ended.is_set():
            # Agent is speaking — wait 1s then interrupt!
            print("  Agent is speaking, waiting 1s then interrupting...")
            send_silence(ep, session_id, 1.0, call_ended)

            clip = clips.get("interrupt") or clips.get("greeting") or list(clips.values())[0]
            print("  >>> INTERRUPTING NOW <<<")
            send_audio_realtime(ep, session_id, clip, "TURN3-INTERRUPT", call_ended)
            send_silence(ep, session_id, 0.5, call_ended)

    # ──────────────────────────────────────────────────────────
    # TURN 4: Wait for agent recovery, then say goodbye
    # ──────────────────────────────────────────────────────────
    if not call_ended.is_set():
        print("\n--- TURN 4: Agent recovers from interrupt, we say goodbye ---")
        print("  Waiting for agent to finish speaking...")
        wait_for_speech(received_samples, recv_lock, timeout_s=10.0)
        wait_for_silence(received_samples, recv_lock, timeout_s=15.0, silence_duration=1.0)

        if not call_ended.is_set():
            clip = clips.get("goodbye") or clips.get("greeting") or list(clips.values())[0]
            send_audio_realtime(ep, session_id, clip, "TURN4-GOODBYE", call_ended)
            send_silence(ep, session_id, 1.0, call_ended)

    # ──────────────────────────────────────────────────────────
    # Wait for final agent response then hang up
    # ──────────────────────────────────────────────────────────
    if not call_ended.is_set():
        print("\n--- Waiting for final agent response ---")
        wait_for_speech(received_samples, recv_lock, timeout_s=8.0)
        wait_for_silence(received_samples, recv_lock, timeout_s=10.0, silence_duration=1.5)

        print("  Hanging up...")
        ep.hangup(session_id)
        time.sleep(1.0)

    running.clear()
    time.sleep(0.5)

    # ──────────────────────────────────────────────────────────
    # Save and analyze received audio
    # ──────────────────────────────────────────────────────────
    with recv_lock:
        total_received = list(received_samples)

    print("\n" + "=" * 60)
    print("  RESULTS")
    print("=" * 60)

    if total_received:
        out_path = "/tmp/received_audio.wav"
        save_wav(out_path, total_received)
        peak, rms = audio_stats(total_received)
        dur = len(total_received) / SAMPLE_RATE
        speech_samples = sum(1 for s in total_received if abs(s) > 500)
        print(f"  Total received: {dur:.1f}s (peak={peak}, rms={rms:.0f})")
        print(f"  Speech content: {speech_samples}/{len(total_received)} samples ({100*speech_samples/len(total_received):.1f}%)")
        print(f"  Saved to: {out_path}")

        # Analyze by 2s chunks to show speech regions
        chunk_dur = 2.0
        chunk_size = int(SAMPLE_RATE * chunk_dur)
        print(f"\n  Audio timeline ({chunk_dur:.0f}s chunks):")
        for i in range(0, len(total_received), chunk_size):
            chunk = total_received[i:i + chunk_size]
            _, chunk_rms = audio_stats(chunk)
            bar = "#" * min(int(chunk_rms / 100), 40)
            label = "SPEECH" if chunk_rms > 300 else "silence"
            t = i / SAMPLE_RATE
            print(f"    {t:5.1f}s: rms={chunk_rms:5.0f} [{label:7s}] {bar}")
    else:
        print("  No audio received from agent!")

    ep.shutdown()
    print("\nDone.")


if __name__ == "__main__":
    main()
