"""Unit tests for ``agent_transport._ffi_queue``.

Mirrors LiveKit's FfiQueue + Queue.wait_for contract:
- ``subscribe`` returns a fresh queue that receives ALL subsequent puts
- ``put`` fans out to every subscribed queue, optionally filtered
- ``wait_for(predicate)`` drains items until predicate matches, dropping rest
- ``unsubscribe`` is idempotent and detaches the queue

These tests do not touch the Rust extension — pure Python.
"""

import asyncio
import pytest

from agent_transport._ffi_queue import (
    ASYNC_ID_EVENT_TYPES,
    DEFAULT_WAIT_TIMEOUT,
    FfiQueue,
    Queue,
)


@pytest.mark.asyncio
async def test_subscribe_put_unsubscribe_roundtrip():
    fq = FfiQueue()
    sub = fq.subscribe()
    assert fq.subscriber_count() == 1
    fq.put({"k": 1})
    ev = await sub.wait_for(lambda e: True, timeout=1.0)
    assert ev == {"k": 1}
    fq.unsubscribe(sub)
    assert fq.subscriber_count() == 0


@pytest.mark.asyncio
async def test_unsubscribe_is_idempotent():
    fq = FfiQueue()
    sub = fq.subscribe()
    fq.unsubscribe(sub)
    fq.unsubscribe(sub)  # second call must not raise
    assert fq.subscriber_count() == 0


@pytest.mark.asyncio
async def test_multiple_subscribers_all_receive_event():
    fq = FfiQueue()
    a = fq.subscribe()
    b = fq.subscribe()
    fq.put({"k": "broadcast"})
    ev_a = await a.wait_for(lambda e: True, timeout=1.0)
    ev_b = await b.wait_for(lambda e: True, timeout=1.0)
    assert ev_a == ev_b == {"k": "broadcast"}
    fq.unsubscribe(a)
    fq.unsubscribe(b)


@pytest.mark.asyncio
async def test_filter_fn_drops_non_matching_at_broker():
    """A subscriber with filter_fn only receives items where the
    filter returns True. The filter runs in ``put`` (at the broker) so
    non-matching items never enter the subscriber's queue at all —
    saves Queue.wait_for from having to scan past noise."""
    fq = FfiQueue()
    sub = fq.subscribe(filter_fn=lambda e: e.get("ours") is True)
    fq.put({"ours": False, "k": 1})
    fq.put({"ours": True, "k": 2})
    fq.put({"ours": False, "k": 3})
    # Only the ours=True item should arrive.
    ev = await sub.wait_for(lambda e: True, timeout=1.0)
    assert ev == {"ours": True, "k": 2}
    # And there should be nothing else queued.
    assert sub.empty()


@pytest.mark.asyncio
async def test_filter_fn_exception_falls_through_delivers_item():
    """LiveKit's contract: if filter_fn raises, the item is delivered
    anyway. A bad filter must not silently drop events.
    (`_ffi_client.py:111-112` mirrors this.)"""
    fq = FfiQueue()
    def broken_filter(e):
        raise RuntimeError("oops")
    sub = fq.subscribe(filter_fn=broken_filter)
    fq.put({"k": 1})
    ev = await sub.wait_for(lambda e: True, timeout=1.0)
    assert ev == {"k": 1}
    fq.unsubscribe(sub)


@pytest.mark.asyncio
async def test_put_after_unsubscribe_is_safe():
    """Once a queue is unsubscribed, subsequent put() calls must not
    raise — a bystander unsubscribe shouldn't crash the broker."""
    fq = FfiQueue()
    sub = fq.subscribe()
    fq.unsubscribe(sub)
    fq.put({"k": 1})  # must not raise


@pytest.mark.asyncio
async def test_subscribe_before_put_event_arrives():
    """The critical race-closing property: subscribe → put → wait_for
    works even if put happens before wait_for is called."""
    fq = FfiQueue()
    sub = fq.subscribe()
    fq.put({"async_id": 42})
    # Yield so the call_soon_threadsafe lands the item.
    await asyncio.sleep(0)
    ev = await sub.wait_for(lambda e: e["async_id"] == 42, timeout=1.0)
    assert ev["async_id"] == 42


@pytest.mark.asyncio
async def test_wait_for_scans_past_non_matching_events():
    """Queue.wait_for must drop events that don't match the predicate
    until one does — mirrors livekit/rtc/_utils.py:97-114."""
    fq = FfiQueue()
    sub = fq.subscribe()
    fq.put({"async_id": 1, "type": "noise"})
    fq.put({"async_id": 2, "type": "noise"})
    fq.put({"async_id": 3, "type": "target"})
    await asyncio.sleep(0)
    ev = await sub.wait_for(lambda e: e.get("type") == "target", timeout=1.0)
    assert ev["async_id"] == 3


@pytest.mark.asyncio
async def test_wait_for_timeout_raises():
    fq = FfiQueue()
    sub = fq.subscribe()
    with pytest.raises(asyncio.TimeoutError):
        await sub.wait_for(lambda e: True, timeout=0.05)


@pytest.mark.asyncio
async def test_wait_for_no_timeout_when_explicit_none():
    """``timeout=None`` mirrors LiveKit's stock behavior (no timeout).
    We just confirm the predicate matches when an item arrives."""
    fq = FfiQueue()
    sub = fq.subscribe()

    async def fire_later():
        await asyncio.sleep(0.05)
        fq.put({"async_id": 7})

    fire_task = asyncio.create_task(fire_later())
    ev = await asyncio.wait_for(
        sub.wait_for(lambda e: e["async_id"] == 7, timeout=None),
        timeout=1.0,
    )
    assert ev["async_id"] == 7
    await fire_task


def test_async_id_event_types_constants():
    """The frozenset of event types audio sources care about — used as
    a quick pre-filter in subscribe(filter_fn=...)."""
    assert "audio_capture_complete" in ASYNC_ID_EVENT_TYPES
    assert "audio_playout_complete" in ASYNC_ID_EVENT_TYPES
    assert "audio_buffer_drained" in ASYNC_ID_EVENT_TYPES
    assert "audio_capture_error" in ASYNC_ID_EVENT_TYPES
    # Lifecycle events should NOT be in here — they bypass the FfiQueue
    # in our current dispatcher.
    assert "call_answered" not in ASYNC_ID_EVENT_TYPES
    assert "dtmf_received" not in ASYNC_ID_EVENT_TYPES


def test_default_wait_timeout_is_reasonable():
    assert 5.0 <= DEFAULT_WAIT_TIMEOUT <= 60.0


@pytest.mark.asyncio
async def test_queue_can_be_used_directly():
    """The Queue subclass is a fully functional asyncio.Queue."""
    q: Queue = Queue()
    q.put_nowait({"k": 1})
    item = await q.wait_for(lambda e: True, timeout=1.0)
    assert item == {"k": 1}
