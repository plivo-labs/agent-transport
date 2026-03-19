#!/usr/bin/env python3
"""
CLI Example Phone — make a real SIP call and talk from your terminal.

Uses the plivo_endpoint Python binding for SIP + sounddevice for mic/speaker.
Demonstrates the full programmatic audio API: recv_audio → speaker, mic → send_audio.

Prerequisites:
    cd crates/agent-endpoint-python && maturin develop
    pip install sounddevice numpy

Usage:
    # Outbound call (call someone):
    PLIVO_USER=xxx PLIVO_PASS=yyy python examples/cli_phone.py sip:+15551234567@phone.plivo.com

    # Inbound (wait for a call):
    PLIVO_USER=xxx PLIVO_PASS=yyy python examples/cli_phone.py
"""

import os
import sys
import time
import threading
import numpy as np
import sounddevice as sd
from plivo_endpoint import PlivoEndpoint, AudioFrame

# Audio config — must match the endpoint's conference bridge rate
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_DURATION_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 320 samples per frame


def main():
    username = os.environ.get("PLIVO_USER") or os.environ.get("PLIVO_USERNAME")
    password = os.environ.get("PLIVO_PASS") or os.environ.get("PLIVO_PASSWORD")
    sip_server = os.environ.get("PLIVO_SIP_DOMAIN", "phone.plivo.com")
    dest_uri = sys.argv[1] if len(sys.argv) > 1 else None

    if not username or not password:
        print("Set PLIVO_USER and PLIVO_PASS environment variables.")
        sys.exit(1)

    # --- Initialize endpoint ---
    print("Initializing SIP endpoint...")
    ep = PlivoEndpoint(sip_server=sip_server, log_level=3)

    print(f"Registering as {username}@{sip_server}...")
    ep.register(username, password)

    # Wait for registration
    event = ep.wait_for_event(timeout_ms=10000)
    if event is None or event["type"] != "registered":
        print(f"Registration failed: {event}")
        sys.exit(1)
    print("Registered.")

    # --- Place or receive call ---
    if dest_uri:
        print(f"Calling {dest_uri}...")
        call_id = ep.call(dest_uri)
        print(f"Call initiated (call_id={call_id}). Waiting for answer...")
    else:
        print("Waiting for incoming call... (Ctrl+C to quit)")
        while True:
            event = ep.wait_for_event(timeout_ms=1000)
            if event and event["type"] == "incoming_call":
                call_id = event["session"].call_id
                print(f"Incoming call from {event['session'].remote_uri}")
                print("Answering...")
                ep.answer(call_id)
                break

    # Wait for media to become active
    while True:
        event = ep.wait_for_event(timeout_ms=500)
        if event is None:
            continue
        if event["type"] == "call_media_active":
            break
        if event["type"] == "call_terminated":
            print(f"Call ended before connecting: {event.get('reason', '')}")
            ep.shutdown()
            return
        if event["type"] == "call_state":
            print(f"  Call state: {event['session'].state}")

    print()
    print("=== CONNECTED — speak into your microphone ===")
    print("Press Enter to hang up.")
    print()

    # --- Audio bridge: mic → send_audio, recv_audio → speaker ---
    running = threading.Event()
    running.set()

    def mic_to_sip():
        """Capture from microphone, push to SIP call via send_audio."""
        def mic_callback(indata, frames, time_info, status):
            if not running.is_set():
                raise sd.CallbackAbort
            # indata is float32 [-1, 1], convert to int16
            samples = (indata[:, 0] * 32767).astype(np.int16)
            frame = AudioFrame(samples.tolist(), SAMPLE_RATE, CHANNELS)
            try:
                ep.send_audio(call_id, frame)
            except Exception:
                pass

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            blocksize=FRAME_SAMPLES,
            dtype="float32",
            callback=mic_callback,
        ):
            running.wait()  # blocks until running is cleared

    def sip_to_speaker():
        """Pull from SIP call via recv_audio, play through speaker."""
        def speaker_callback(outdata, frames, time_info, status):
            if not running.is_set():
                raise sd.CallbackAbort
            frame = ep.recv_audio(call_id)
            if frame is not None:
                samples = np.array(frame.data, dtype=np.int16).astype(np.float32) / 32767.0
                # Pad or trim to match requested frame size
                if len(samples) >= frames:
                    outdata[:, 0] = samples[:frames]
                else:
                    outdata[:len(samples), 0] = samples
                    outdata[len(samples):, 0] = 0.0
            else:
                outdata[:] = 0.0

        with sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            blocksize=FRAME_SAMPLES,
            dtype="float32",
            callback=speaker_callback,
        ):
            running.wait()

    # Start audio threads
    mic_thread = threading.Thread(target=mic_to_sip, daemon=True)
    speaker_thread = threading.Thread(target=sip_to_speaker, daemon=True)
    mic_thread.start()
    speaker_thread.start()

    # --- Event loop: watch for hangup or call end ---
    # Separate thread to watch for Enter key
    hangup = threading.Event()

    def wait_for_enter():
        try:
            input()
        except EOFError:
            pass
        hangup.set()

    enter_thread = threading.Thread(target=wait_for_enter, daemon=True)
    enter_thread.start()

    while running.is_set():
        if hangup.is_set():
            print("Hanging up...")
            ep.hangup(call_id)

        event = ep.poll_event()
        if event and event["type"] == "call_terminated":
            print(f"Call ended: {event.get('reason', '')}")
            break
        if event and event["type"] == "dtmf_received":
            print(f"DTMF: {event['digit']}")

        time.sleep(0.05)

    # --- Cleanup ---
    running.clear()
    mic_thread.join(timeout=1)
    speaker_thread.join(timeout=1)
    ep.shutdown()
    print("Done.")


if __name__ == "__main__":
    main()
