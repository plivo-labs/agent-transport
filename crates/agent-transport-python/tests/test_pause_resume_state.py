"""Regression tests for TransportAudioOutput's pause/resume state consistency.

Previously, the per-transport audio output classes set ``_rust_paused = True``
BEFORE calling the FFI. If the FFI raised, the flag stayed True but Rust
wasn't paused, so the next pause() was short-circuited and Rust kept
sending audio.

These tests instantiate TransportAudioOutput without invoking
``_ParticipantAudioOutput.__init__`` (which allocates an FFI handle for
its orphan rtc.AudioSource), then verify ``_rust_paused`` transitions on
both success and failure paths.
"""

import asyncio
from unittest.mock import patch

from agent_transport.sip.livekit._audio_io import TransportAudioOutput


class FakeEndpointSuccess:
    def __init__(self):
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self, sid):
        self.pause_calls += 1

    def resume(self, sid):
        self.resume_calls += 1


class FakeEndpointFailing:
    def __init__(self):
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self, sid):
        self.pause_calls += 1
        raise RuntimeError("simulated pause failure")

    def resume(self, sid):
        self.resume_calls += 1
        raise RuntimeError("simulated resume failure")


def _make_output(ep):
    """Construct a TransportAudioOutput without calling the base __init__."""
    o = TransportAudioOutput.__new__(TransportAudioOutput)
    o._ep = ep
    o._sid = "session-test"
    o._rust_paused = False
    # The parent's pause/resume touch _playback_enabled / _first_frame_event;
    # initialise them as plain Events so the no-op patches still mutate them.
    o._playback_enabled = asyncio.Event()
    o._playback_enabled.set()
    o._first_frame_event = asyncio.Event()
    return o


def test_output_pause_resume_success_flow():
    ep = FakeEndpointSuccess()
    o = _make_output(ep)

    # Patch parent's pause/resume to no-ops so super().pause()/resume()
    # doesn't blow up on the missing base-class state.
    parent = type(o).__mro__[1]
    with patch.object(parent, "pause", lambda self: None), \
         patch.object(parent, "resume", lambda self: None):
        assert not o._rust_paused
        o.pause()
        assert o._rust_paused
        assert ep.pause_calls == 1

        # Double pause is idempotent (short-circuited by _rust_paused).
        o.pause()
        assert o._rust_paused
        assert ep.pause_calls == 1, "second pause() should be short-circuited"

        o.resume()
        assert not o._rust_paused
        assert ep.resume_calls == 1


def test_output_pause_failure_does_not_set_flag():
    """Regression: if ep.pause() raises, ``_rust_paused`` must stay False
    so the next ``pause()`` retries the FFI call.
    """
    ep = FakeEndpointFailing()
    o = _make_output(ep)

    parent = type(o).__mro__[1]
    with patch.object(parent, "pause", lambda self: None), \
         patch.object(parent, "resume", lambda self: None):
        o.pause()
        assert not o._rust_paused, "pause failure must leave _rust_paused False"
        assert ep.pause_calls == 1

        # Retry attempt
        o.pause()
        assert ep.pause_calls == 2, "failed pause must allow retry on next call"


def test_output_resume_failure_does_not_clear_flag():
    """Mirror: if ep.resume() raises, ``_rust_paused`` must stay True so
    the next ``resume()`` retries.
    """
    ep_ok = FakeEndpointSuccess()
    o = _make_output(ep_ok)

    parent = type(o).__mro__[1]
    with patch.object(parent, "pause", lambda self: None), \
         patch.object(parent, "resume", lambda self: None):
        o.pause()
        assert o._rust_paused

        o._ep = FakeEndpointFailing()
        o.resume()
        assert o._rust_paused, "resume failure must leave _rust_paused True"

        o._ep = ep_ok
        o.resume()
        assert not o._rust_paused
        assert ep_ok.resume_calls == 1
