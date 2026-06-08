"""Shared pytest configuration for agent_transport adapter tests."""

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Guarantee a current event loop for every test.

    Several sync tests construct objects (e.g. ``TransportAudioOutput`` →
    LiveKit's ``rtc.AudioSource``) that call ``asyncio.get_event_loop()`` at
    construction time. On Python 3.12+ that raises ``RuntimeError`` in a thread
    with no running/current loop (3.11 silently auto-created one). In production
    these objects are always built inside the agent's running loop, so this
    fixture restores that invariant for the sync tests and keeps the suite green
    on both 3.11 and 3.12+.
    """
    try:
        asyncio.get_running_loop()  # no-op for async tests; not deprecated
    except RuntimeError:
        # Sync test, no running loop — install a current one so construction-time
        # `get_event_loop()` calls (e.g. in rtc.AudioSource) resolve it cleanly.
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield


def pytest_collection_modifyitems(config, items):
    """Auto-apply asyncio marker to any async test that lacks one."""
    for item in items:
        if (
            "asyncio" not in item.keywords
            and getattr(item.obj, "__code__", None)
            and item.obj.__code__.co_flags & 0x100  # CO_COROUTINE
        ):
            item.add_marker(pytest.mark.asyncio)
