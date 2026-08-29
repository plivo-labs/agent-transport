#!/usr/bin/env python3
"""Local audio-stream e2e matrix for CI.

This exercises the Rust-backed AudioStreamEndpoint through the Python binding
using local Plivo-protocol WebSocket clients. It needs no SIP credentials and
does not dial an external provider.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import socket
import sys
import time
from dataclasses import dataclass
from typing import Callable


class E2EFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class Scenario:
    name: str
    wire_encoding: str
    wire_sample_rate: int
    input_sample_rate: int
    output_sample_rate: int
    controls: bool = False
    disconnect: bool = False

    @property
    def content_type(self) -> str:
        if self.wire_encoding == "mulaw":
            return "audio/x-mulaw"
        return "audio/x-l16"


@dataclass(frozen=True)
class AudioStats:
    samples: int
    speech_samples: int

    @property
    def speech_percent(self) -> float:
        if self.samples == 0:
            return 0.0
        return 100.0 * self.speech_samples / self.samples


SCENARIOS: dict[str, Scenario] = {
    "l16-8k": Scenario("l16-8k", "l16", 8000, 8000, 8000),
    "l16-16k": Scenario("l16-16k", "l16", 16000, 8000, 8000),
    "mulaw-8k": Scenario("mulaw-8k", "mulaw", 8000, 8000, 8000),
    "controls": Scenario("controls", "l16", 8000, 8000, 8000, controls=True),
    "disconnect": Scenario("disconnect", "l16", 8000, 8000, 8000, disconnect=True),
}


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Audio-stream WebSocket e2e matrix")
    parser.add_argument("--ci", action="store_true", help="Accepted for workflow readability")
    parser.add_argument("--dry-run", action="store_true", help="Validate CLI shape without opening a socket")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=0)
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted([*SCENARIOS.keys(), "matrix"]),
        help="Scenario to run. May be repeated. Default: matrix.",
    )
    parser.add_argument(
        "--direction",
        choices=["both", "inbound", "outbound"],
        default="both",
        help=(
            "Media direction to exercise. 'both' runs the full roundtrip "
            "(default, matches the existing matrix). 'inbound' / 'outbound' "
            "isolate one direction and skip non-media scenarios."
        ),
    )
    parser.add_argument("--min-inbound-seconds", type=float, default=0.25)
    parser.add_argument("--min-outbound-seconds", type=float, default=0.25)
    parser.add_argument("--speech-threshold", type=int, default=500)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    return parser.parse_args(argv)


MEDIA_SCENARIO_NAMES = ("l16-8k", "l16-16k", "mulaw-8k")


def selected_scenarios(names: list[str] | None, direction: str) -> list[Scenario]:
    if not names or "matrix" in names:
        chosen = list(MEDIA_SCENARIO_NAMES) + ["controls", "disconnect"]
    else:
        chosen = list(names)
    if direction != "both":
        chosen = [name for name in chosen if name in MEDIA_SCENARIO_NAMES]
    return [SCENARIOS[name] for name in chosen]


def load_deps():
    try:
        import websockets
    except ImportError as exc:
        raise E2EFailure("Missing dependency: websockets") from exc

    try:
        from agent_transport import AudioFrame, AudioStreamEndpoint, init_logging
    except ImportError as exc:
        raise E2EFailure("Missing dependency: agent_transport") from exc

    return websockets, AudioFrame, AudioStreamEndpoint, init_logging


def choose_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def sine_samples(sample_rate: int, seconds: float, freq: float = 440.0, amplitude: int = 8000) -> list[int]:
    count = int(sample_rate * seconds)
    return [
        int(amplitude * math.sin(2.0 * math.pi * freq * idx / sample_rate))
        for idx in range(count)
    ]


def pcm16le(samples: list[int]) -> bytes:
    return b"".join(int(sample).to_bytes(2, "little", signed=True) for sample in samples)


def linear_to_ulaw(sample: int) -> int:
    bias = 0x84
    clip = 32635
    sample = max(-clip, min(clip, sample))
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    sample += bias
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def ulaw_to_linear(byte: int) -> int:
    byte = (~byte) & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = ((mantissa << 3) + 0x84) << exponent
    sample -= 0x84
    return -sample if sign else sample


def encode_wire(samples: list[int], scenario: Scenario) -> bytes:
    if scenario.wire_encoding == "mulaw":
        return bytes(linear_to_ulaw(sample) for sample in samples)
    return pcm16le(samples)


def decode_wire(raw: bytes, scenario: Scenario) -> list[int]:
    if scenario.wire_encoding == "mulaw":
        return [ulaw_to_linear(byte) for byte in raw]
    return [
        int.from_bytes(raw[idx : idx + 2], "little", signed=True)
        for idx in range(0, len(raw) - 1, 2)
    ]


def b64_wire(samples: list[int], scenario: Scenario) -> str:
    return base64.b64encode(encode_wire(samples, scenario)).decode("ascii")


def audio_stats(samples: list[int], threshold: int) -> AudioStats:
    speech = sum(1 for sample in samples if abs(sample) >= threshold)
    return AudioStats(samples=len(samples), speech_samples=speech)


async def wait_event(
    ep,
    event_type: str,
    timeout_seconds: float,
    predicate: Callable[[dict], bool] | None = None,
):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        event = await asyncio.to_thread(ep.wait_for_event, min(remaining_ms, 250))
        if event is None:
            continue
        print(f"event={event.get('type')}")
        if event.get("type") == event_type and (predicate is None or predicate(event)):
            return event
        if event.get("type") in {"call_terminated", "shutdown"} and event_type != event.get("type"):
            if predicate is None or predicate(event):
                raise E2EFailure(f"Unexpected terminal event while waiting for {event_type}: {event}")
    raise E2EFailure(f"Timed out waiting for event: {event_type}")


async def recv_endpoint_audio(ep, session_id: str, sample_rate: int, min_seconds: float, threshold: int, timeout_seconds: float):
    deadline = time.monotonic() + timeout_seconds
    samples: list[int] = []
    required = int(sample_rate * min_seconds)
    while len(samples) < required and time.monotonic() < deadline:
        frame = await asyncio.to_thread(ep.recv_audio_blocking, session_id, 250)
        if frame is None:
            continue
        samples.extend(int(sample) for sample in frame.data)
    stats = audio_stats(samples, threshold)
    if len(samples) < required:
        raise E2EFailure(f"Insufficient inbound audio: got {len(samples)} samples, need {required}")
    if stats.speech_percent < 1.0:
        raise E2EFailure(f"Insufficient inbound speech: {stats.speech_percent:.2f}%")
    return stats


def start_message(call_id: str, stream_id: str, scenario: Scenario) -> str:
    return json.dumps(
        {
            "event": "start",
            "start": {
                "callId": call_id,
                "streamId": stream_id,
                "mediaFormat": {
                    "encoding": scenario.content_type,
                    "sampleRate": scenario.wire_sample_rate,
                },
            },
            "extra_headers": '{"X-PH-e2e": "audio-stream"}',
        }
    )


def media_message(samples: list[int], scenario: Scenario) -> str:
    return json.dumps({"event": "media", "media": {"payload": b64_wire(samples, scenario)}})


async def read_json(ws, timeout_seconds: float):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout_seconds)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


async def read_until(ws, predicate, timeout_seconds: float):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        msg = await read_json(ws, max(0.1, deadline - time.monotonic()))
        print(f"ws_event={msg.get('event')}")
        if predicate(msg):
            return msg
    raise E2EFailure("Timed out waiting for expected websocket message")


async def connect_with_retry(websockets, uri: str, timeout_seconds: float):
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            return await websockets.connect(uri, open_timeout=min(2.0, timeout_seconds))
        except OSError as exc:
            last_error = exc
            await asyncio.sleep(0.05)
    raise E2EFailure(f"Timed out connecting to {uri}: {last_error}")


async def start_session(websockets, ep, uri: str, scenario: Scenario, timeout_seconds: float):
    ws = await connect_with_retry(websockets, uri, timeout_seconds)
    call_id = f"audio-stream-e2e-call-{scenario.name}"
    stream_id = f"audio-stream-e2e-stream-{scenario.name}"
    await ws.send(start_message(call_id, stream_id, scenario))
    answered = await wait_event(
        ep,
        "call_answered",
        timeout_seconds,
        lambda event: getattr(event.get("session"), "call_uuid", None) == call_id,
    )
    session_id = answered["session"].session_id
    print(f"session_id={session_id}")
    return ws, session_id


async def send_inbound_audio(ws, scenario: Scenario, seconds: float):
    inbound = sine_samples(scenario.wire_sample_rate, seconds, freq=440.0)
    frame_size = int(scenario.wire_sample_rate * 0.02)
    for idx in range(0, len(inbound), frame_size):
        await ws.send(media_message(inbound[idx : idx + frame_size], scenario))
        await asyncio.sleep(0.005)


async def send_outbound_audio(ep, AudioFrame, session_id: str, scenario: Scenario, seconds: float):
    outbound = sine_samples(scenario.output_sample_rate, seconds, freq=660.0)
    outbound_frame_size = int(scenario.output_sample_rate * 0.02)
    for idx in range(0, len(outbound), outbound_frame_size):
        ep.send_audio(
            session_id,
            AudioFrame(outbound[idx : idx + outbound_frame_size], scenario.output_sample_rate, 1),
        )
        await asyncio.sleep(0.02)


async def verify_outbound_audio(ws, ep, session_id: str, scenario: Scenario, min_seconds: float, threshold: int, timeout_seconds: float):
    ep.flush(session_id)
    expected_wire_bytes_per_second = scenario.wire_sample_rate
    if scenario.wire_encoding == "l16":
        expected_wire_bytes_per_second *= 2
    required_outbound_bytes = int(expected_wire_bytes_per_second * min_seconds)
    outbound_bytes = bytearray()
    checkpoint_name = None
    deadline = time.monotonic() + timeout_seconds
    while (len(outbound_bytes) < required_outbound_bytes or checkpoint_name is None) and time.monotonic() < deadline:
        msg = await read_json(ws, max(0.1, deadline - time.monotonic()))
        event = msg.get("event")
        print(f"ws_event={event}")
        if event == "playAudio":
            media = msg.get("media") or {}
            if media.get("contentType") != scenario.content_type:
                raise E2EFailure(f"Unexpected outbound contentType: {media.get('contentType')}")
            if media.get("sampleRate") != scenario.wire_sample_rate:
                raise E2EFailure(f"Unexpected outbound sampleRate: {media.get('sampleRate')}")
            outbound_bytes.extend(base64.b64decode(media.get("payload", "")))
        elif event == "checkpoint":
            checkpoint_name = msg.get("name")
            await ws.send(json.dumps({"event": "playedStream", "name": checkpoint_name}))

    if len(outbound_bytes) < required_outbound_bytes:
        raise E2EFailure(
            f"Insufficient outbound audio: got {len(outbound_bytes)} bytes, need {required_outbound_bytes}"
        )
    if checkpoint_name is None:
        raise E2EFailure("Did not receive outbound checkpoint")
    if not await asyncio.to_thread(ep.wait_for_playout, session_id, int(timeout_seconds * 1000)):
        raise E2EFailure("Timed out waiting for playout acknowledgement")

    outbound_samples = decode_wire(bytes(outbound_bytes), scenario)
    outbound_stats = audio_stats(outbound_samples, threshold)
    if outbound_stats.speech_percent < 1.0:
        raise E2EFailure(f"Insufficient outbound speech: {outbound_stats.speech_percent:.2f}%")
    print(
        "outbound_audio="
        f"{len(outbound_samples) / scenario.wire_sample_rate:.3f}s "
        f"speech={outbound_stats.speech_percent:.2f}%"
    )


async def run_media_roundtrip(websockets, AudioFrame, ep, uri: str, scenario: Scenario, args):
    ws, session_id = await start_session(websockets, ep, uri, scenario, args.timeout_seconds)
    try:
        if args.direction in ("both", "inbound"):
            await send_inbound_audio(ws, scenario, args.min_inbound_seconds + 0.1)
            inbound_stats = await recv_endpoint_audio(
                ep,
                session_id,
                scenario.input_sample_rate,
                args.min_inbound_seconds,
                args.speech_threshold,
                args.timeout_seconds,
            )
            print(
                "inbound_audio="
                f"{inbound_stats.samples / scenario.input_sample_rate:.3f}s "
                f"speech={inbound_stats.speech_percent:.2f}%"
            )

            await ws.send(json.dumps({"event": "dtmf", "dtmf": {"digit": "7"}}))
            dtmf = await wait_event(
                ep,
                "dtmf_received",
                args.timeout_seconds,
                lambda event: event.get("session_id") == session_id,
            )
            if dtmf.get("digit") != "7" or dtmf.get("method") != "audio_stream":
                raise E2EFailure(f"Unexpected DTMF event: {dtmf}")
            print("dtmf_received=7")

        if args.direction in ("both", "outbound"):
            await send_outbound_audio(ep, AudioFrame, session_id, scenario, args.min_outbound_seconds + 0.25)
            await verify_outbound_audio(
                ws,
                ep,
                session_id,
                scenario,
                args.min_outbound_seconds,
                args.speech_threshold,
                args.timeout_seconds,
            )

            ep.send_dtmf(session_id, "42")
            outbound_dtmf = await read_until(
                ws,
                lambda m: m.get("event") == "sendDTMF",
                args.timeout_seconds,
            )
            if outbound_dtmf.get("dtmf") != "42":
                raise E2EFailure(f"Unexpected outbound DTMF payload: {outbound_dtmf}")
            print("outbound_dtmf=42")

        await ws.send(json.dumps({"event": "stop"}))
        await wait_event(
            ep,
            "call_terminated",
            args.timeout_seconds,
            lambda event: getattr(event.get("session"), "session_id", None) == session_id,
        )
    finally:
        await ws.close()


async def run_controls(websockets, AudioFrame, ep, uri: str, scenario: Scenario, args):
    ws, session_id = await start_session(websockets, ep, uri, scenario, args.timeout_seconds)
    try:
        ep.pause(session_id)
        msg = await read_until(ws, lambda m: m.get("event") == "clearAudio", args.timeout_seconds)
        if msg.get("streamId") != f"audio-stream-e2e-stream-{scenario.name}":
            raise E2EFailure(f"Unexpected clearAudio streamId: {msg}")
        await ws.send(json.dumps({"event": "clearedAudio"}))

        ep.resume(session_id)
        await send_outbound_audio(ep, AudioFrame, session_id, scenario, 0.08)
        ep.clear_buffer(session_id)
        await read_until(ws, lambda m: m.get("event") == "clearAudio", args.timeout_seconds)

        ep.send_dtmf(session_id, "42")
        dtmf_msg = await read_until(ws, lambda m: m.get("event") == "sendDTMF", args.timeout_seconds)
        if dtmf_msg.get("dtmf") != "42":
            raise E2EFailure(f"Unexpected outbound DTMF payload: {dtmf_msg}")

        cp_name = ep.checkpoint(session_id, "manual-cp")
        cp_msg = await read_until(ws, lambda m: m.get("event") == "checkpoint", args.timeout_seconds)
        if cp_msg.get("name") != cp_name:
            raise E2EFailure(f"Unexpected checkpoint payload: {cp_msg}")
        await ws.send(json.dumps({"event": "playedStream", "name": cp_name}))

        await ws.send(json.dumps({"event": "stop"}))
        await wait_event(
            ep,
            "call_terminated",
            args.timeout_seconds,
            lambda event: getattr(event.get("session"), "session_id", None) == session_id,
        )
    finally:
        await ws.close()


async def run_disconnect(websockets, ep, uri: str, scenario: Scenario, args):
    ws, session_id = await start_session(websockets, ep, uri, scenario, args.timeout_seconds)
    await ws.close()
    await wait_event(
        ep,
        "call_terminated",
        args.timeout_seconds,
        lambda event: getattr(event.get("session"), "session_id", None) == session_id,
    )
    try:
        ep.send_dtmf(session_id, "1")
    except Exception:
        print("post_disconnect_send_failed=true")
    else:
        raise E2EFailure("send_dtmf unexpectedly succeeded after websocket disconnect")


async def run_scenario(websockets, AudioFrame, AudioStreamEndpoint, scenario: Scenario, args):
    port = args.listen_port or choose_port(args.listen_host)
    listen_addr = f"{args.listen_host}:{port}"
    uri = f"ws://{listen_addr}"
    print(f"scenario={scenario.name}")
    print(f"listen_addr={listen_addr}")
    print(f"wire={scenario.content_type}/{scenario.wire_sample_rate}")
    print(f"pipeline_in={scenario.input_sample_rate} pipeline_out={scenario.output_sample_rate}")

    ep = AudioStreamEndpoint(
        listen_addr,
        "",
        "",
        scenario.input_sample_rate,
        scenario.output_sample_rate,
        False,
    )

    try:
        if scenario.controls:
            await run_controls(websockets, AudioFrame, ep, uri, scenario, args)
        elif scenario.disconnect:
            await run_disconnect(websockets, ep, uri, scenario, args)
        else:
            await run_media_roundtrip(websockets, AudioFrame, ep, uri, scenario, args)
    finally:
        await asyncio.to_thread(ep.shutdown)
    print(f"scenario_passed={scenario.name}")


async def run_matrix(args, scenarios):
    websockets, AudioFrame, AudioStreamEndpoint, init_logging = load_deps()
    rust_log = os.environ.get("E2E_RUST_LOG", "info")
    init_logging(rust_log)
    for scenario in scenarios:
        await run_scenario(websockets, AudioFrame, AudioStreamEndpoint, scenario, args)


def run(args):
    scenarios = selected_scenarios(args.scenario, args.direction)
    print(f"dry_run={args.dry_run}")
    print(f"direction={args.direction}")
    print("scenarios=" + ",".join(scenario.name for scenario in scenarios))
    if not scenarios:
        raise E2EFailure(
            f"No scenarios selected for --direction {args.direction}; expected one of {MEDIA_SCENARIO_NAMES}"
        )
    if args.dry_run:
        print("dry run passed")
        return 0
    asyncio.run(run_matrix(args, scenarios))
    label = "matrix" if args.direction == "both" else args.direction
    print(f"audio stream {label} passed")
    return 0


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    try:
        return run(args)
    except E2EFailure as exc:
        print(f"E2E FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
