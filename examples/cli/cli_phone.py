#!/usr/bin/env python3
"""
CLI Phone — make a real SIP call and talk from your terminal.

Uses agent_transport for SIP + sounddevice for mic/speaker.

Prerequisites:
    cd crates/agent-transport-python && maturin develop
    pip install sounddevice numpy

Usage:
    # Outbound call:
    SIP_USERNAME=xxx SIP_PASSWORD=yyy \
        python examples/cli_phone.py sip:+15551234567@phone.plivo.com

    # Inbound (wait for a call):
    SIP_USERNAME=xxx SIP_PASSWORD=yyy python examples/cli_phone.py

Keyboard controls during call:
    0-9, *, #   Send DTMF digit
    m           Mute mic
    u           Unmute mic
    h           Hold (SIP Re-INVITE sendonly)
    H           Unhold (SIP Re-INVITE sendrecv)
    q / Enter   Hang up
"""

import os
import sys
import tty
import termios
import select
import threading
import numpy as np
import sounddevice as sd
from agent_transport import SipEndpoint, AudioFrame, init_logging

SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_SAMPLES = SAMPLE_RATE * 20 // 1000  # 320 samples per 20ms frame
DTMF_KEYS = set("0123456789*#")


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

    print("Initializing SIP endpoint...")
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
        call_id = ep.call(dest_uri)
        print(f"Call initiated (call_id={call_id}). Waiting for answer...")
    else:
        print("Waiting for incoming call... (Ctrl+C to quit)")
        while True:
            event = ep.wait_for_event(timeout_ms=1000)
            if event and event["type"] == "incoming_call":
                call_id = event["session"]["call_id"]
                print(f"Incoming call from {event['session']['remote_uri']}")
                ep.answer(call_id)
                break

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

    print()
    print("=== CONNECTED ===")
    print("  0-9,*,#  Send DTMF    m Mute    u Unmute    h Hold    H Unhold    q Hang up")
    print()

    running = threading.Event()
    running.set()
    is_held = False

    def mic_to_sip():
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                blocksize=FRAME_SAMPLES, dtype="int16") as stream:
                while running.is_set():
                    data, _ = stream.read(FRAME_SAMPLES)
                    try:
                        ep.send_audio(call_id, AudioFrame(data[:, 0].tolist(), SAMPLE_RATE, CHANNELS))
                    except Exception:
                        break
        except Exception:
            pass

    def sip_to_speaker():
        try:
            with sd.OutputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                 blocksize=FRAME_SAMPLES, dtype="int16") as stream:
                while running.is_set():
                    try:
                        frame = ep.recv_audio_blocking(call_id, 20)
                    except Exception:
                        break
                    if frame is not None:
                        stream.write(np.array(frame.data, dtype=np.int16).reshape(-1, 1))
                    else:
                        stream.write(np.zeros((FRAME_SAMPLES, 1), dtype=np.int16))
        except Exception:
            pass

    call_ended = threading.Event()

    def event_watcher():
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

    for t in [threading.Thread(target=f, daemon=True) for f in [mic_to_sip, sip_to_speaker, event_watcher]]:
        t.start()

    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setraw(sys.stdin.fileno())
        while running.is_set() and not call_ended.is_set():
            ch = getch_nonblocking()
            if ch is None:
                if call_ended.wait(timeout=0.1):
                    break
                continue

            if ch in DTMF_KEYS:
                ep.send_dtmf(call_id, ch)
                sys.stdout.write(f"\r  DTMF sent: {ch}\n")
                sys.stdout.flush()
            elif ch == 'm':
                ep.mute(call_id)
                sys.stdout.write("\r  MUTED\n")
                sys.stdout.flush()
            elif ch == 'u':
                ep.unmute(call_id)
                sys.stdout.write("\r  UNMUTED\n")
                sys.stdout.flush()
            elif ch == 'h':
                if not is_held:
                    ep.hold(call_id)
                    is_held = True
                    sys.stdout.write("\r  HOLD — Re-INVITE sendonly\n")
                    sys.stdout.flush()
            elif ch == 'H':
                if is_held:
                    ep.unhold(call_id)
                    is_held = False
                    sys.stdout.write("\r  UNHOLD — Re-INVITE sendrecv\n")
                    sys.stdout.flush()
            elif ch in ('q', '\r', '\n', '\x03'):
                sys.stdout.write("\r  Hanging up...\n")
                sys.stdout.flush()
                ep.hangup(call_id)
                break
    except Exception:
        pass
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

    running.clear()
    ep.shutdown()
    print("Done.")


if __name__ == "__main__":
    main()
