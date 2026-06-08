"""FfiQueue + Queue — port of LiveKit's FFI event-queue pattern.

Behavioral mirror of:
- ``livekit/rtc/_ffi_client.py:95-149`` (``FfiQueue``)
- ``livekit/rtc/_utils.py:97-114`` (``Queue.wait_for``)

The point of this pattern is to close a small but real race: any
"emit event from a worker thread, await it on the asyncio loop" design
where the await is registered *after* the FFI call returns has a window
where the event can fire before the await is set up — and silently
disappear. LiveKit avoids it by ``subscribe → request → wait_for(predicate)
→ unsubscribe``: the queue starts collecting events the moment subscribe
returns, so even if an event fires synchronously during the FFI request,
it sits in the queue until ``wait_for`` scans it.

One :class:`FfiQueue` instance per endpoint (not process-global). We may
host ``SipEndpoint`` + ``AudioStreamEndpoint`` in the same process, and a
shared queue would fan events from one endpoint to subscribers of the
other. Per-endpoint avoids that cross-contamination.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Generic, List, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_WAIT_TIMEOUT = 30.0
"""Default timeout (seconds) for :meth:`Queue.wait_for`.

LiveKit's stock pattern has no timeout — calls hang forever if the FFI
event never arrives. We diverge here deliberately (gap-audit item #12):
a true hang in production should surface in logs rather than silently
freezing a session. Pass ``timeout=None`` for LiveKit-exact behavior.
"""

ASYNC_ID_EVENT_TYPES = frozenset(
    [
        "audio_capture_complete",
        "audio_playout_complete",
        "audio_buffer_drained",
        "audio_capture_error",
    ]
)
"""Event types carrying an ``async_id``. Use in subscribe filter_fn to
narrow to per-call completion events and skip lifecycle events."""


class Queue(asyncio.Queue, Generic[T]):
    """``asyncio.Queue`` with LiveKit's ``wait_for(predicate)`` extension.

    ``wait_for`` drains items from the head until one matches the
    predicate; non-matching items are discarded (``task_done`` called).
    This is what makes the subscribe-before-request pattern race-free —
    an event that arrived before the await started is still in the queue
    when wait_for scans.

    Mirrors ``livekit/rtc/_utils.py:97-114`` with one addition: a
    ``timeout`` parameter, so a stuck FFI doesn't hang forever.
    """

    async def wait_for(
        self,
        predicate: Callable[[T], bool],
        timeout: Optional[float] = DEFAULT_WAIT_TIMEOUT,
    ) -> T:
        """Wait for an item that matches ``predicate``.

        Items that don't match are discarded (``task_done`` is called for
        them; the caller is responsible for ``task_done`` on the returned
        item, per LiveKit's contract).

        Raises:
            asyncio.TimeoutError: ``timeout`` elapsed before a match.
        """

        async def _wait() -> T:
            while True:
                event = await self.get()
                if predicate(event):
                    return event
                # No match: this event isn't ours, drop it.
                self.task_done()

        if timeout is None:
            return await _wait()
        return await asyncio.wait_for(_wait(), timeout=timeout)


_Subscriber = Tuple[
    Queue[T],
    asyncio.AbstractEventLoop,
    Optional[Callable[[T], bool]],
]

_UNSET = object()


class FfiQueue(Generic[T]):
    """Multi-subscriber event broker for endpoint events.

    The endpoint's central event pump (driven by a background asyncio
    task that drains ``ep.wait_for_event`` via ``run_in_executor``) calls
    :meth:`put` for every async-id-bearing event. Subscribers (audio
    sources, audio streams, future per-call awaiters) call
    :meth:`subscribe` to get their own :class:`Queue` and
    :meth:`unsubscribe` to detach. Subscribe/unsubscribe is fast (list
    append/scan) and is expected per-operation in the steady state.

    Thread safety: :meth:`put` is called from the asyncio event-loop
    thread (after ``run_in_executor`` resumes the central pump task),
    but the protocol allows ``put`` from any thread —
    ``loop.call_soon_threadsafe`` shields the queue mutation.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Subscribers bucketed by routing key (the session id they care about).
        # The ``None`` bucket is the catch-all: those subscribers receive every
        # item regardless of its key (filterless / multi-session consumers).
        self._by_key: dict[Any, List[_Subscriber[T]]] = {}
        # id(queue) -> key, so unsubscribe() finds the right bucket in O(1).
        self._key_of: dict[int, Any] = {}

    def put(self, item: T, key: Any = None) -> None:
        """Deliver ``item`` to the subscribers routed by ``key``, filtered.

        ``key`` is the item's routing key (its session id), supplied by the
        producer. Delivery set = subscribers registered for exactly ``key``
        PLUS the catch-all (``None``-key) subscribers. If ``key`` is ``None``
        (an unroutable item), the item is broadcast to ALL subscribers so
        nothing is missed. ``filter_fn`` is still applied per subscriber, so
        keying only narrows delivery to a superset of what the filter accepts —
        it can never drop a wanted event. This turns the per-frame fan-out from
        O(total subscribers) into O(this session's subscribers).

        If a subscriber's ``filter_fn`` raises, the item is delivered anyway
        (LiveKit's behavior — filter errors must not silently drop events).
        """
        with self._lock:
            if key is None:
                subscribers = [s for bucket in self._by_key.values() for s in bucket]
            else:
                subscribers = list(self._by_key.get(key, ()))
                catch_all = self._by_key.get(None)
                if catch_all:
                    subscribers.extend(catch_all)
        delivered = 0
        for queue, loop, filter_fn in subscribers:
            if filter_fn is not None:
                try:
                    if not filter_fn(item):
                        continue
                except Exception:
                    logger.exception(
                        "FfiQueue filter_fn raised; delivering item anyway"
                    )
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
                delivered += 1
            except RuntimeError as e:
                # Loop is closed (subscriber abandoned the queue without
                # unsubscribing — usually a session teardown timing issue).
                logger.debug("FfiQueue put failed (loop closed?): %s", e)
            except Exception:
                logger.exception("FfiQueue put failed")
        if isinstance(item, dict) and item.get("type") in ASYNC_ID_EVENT_TYPES:
            logger.debug(
                "FfiQueue.put: type=%s async_id=%s session=%s subs=%d delivered=%d",
                item.get("type"), item.get("async_id"),
                item.get("session_id", "?")[:8],
                len(subscribers), delivered,
            )

    def subscribe(
        self,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        filter_fn: Optional[Callable[[T], bool]] = None,
        key: Any = None,
    ) -> Queue[T]:
        """Create a fresh queue and register it as a subscriber.

        From the moment this returns, the queue receives every item passed to
        :meth:`put` whose routing key matches ``key`` (plus keyless/broadcast
        items), subject to ``filter_fn``. ``key`` should be the session id the
        subscriber cares about; ``None`` (the default) makes it a catch-all that
        receives every item — preserving the old broadcast behavior for callers
        that don't route. Callers SHOULD always unsubscribe in a finally block —
        otherwise the queue retains references and grows unbounded.
        """
        queue: Queue[T] = Queue()
        loop = loop or asyncio.get_event_loop()
        with self._lock:
            self._by_key.setdefault(key, []).append((queue, loop, filter_fn))
            self._key_of[id(queue)] = key
        return queue

    def unsubscribe(self, queue: Queue[T]) -> None:
        """Detach the queue from its bucket.

        Idempotent: calling unsubscribe on a queue that isn't subscribed
        is a no-op (covers the double-unsubscribe-in-finally case).
        """
        with self._lock:
            key = self._key_of.pop(id(queue), _UNSET)
            # Normally the bucket is known from _key_of; fall back to scanning
            # every bucket if that mapping was somehow lost (defensive).
            buckets = [key] if key is not _UNSET else list(self._by_key.keys())
            for k in buckets:
                bucket = self._by_key.get(k)
                if not bucket:
                    continue
                for i, (q, _, _) in enumerate(bucket):
                    if q is queue:
                        bucket.pop(i)
                        if not bucket:
                            del self._by_key[k]
                        return

    def subscriber_count(self) -> int:
        """Current number of subscribed queues (for tests / metrics)."""
        with self._lock:
            return sum(len(bucket) for bucket in self._by_key.values())


# ─── Process-wide singleton ──────────────────────────────────────────────────
#
# Matches LiveKit's ``FfiClient.instance.queue`` pattern: one broker per
# process, all FFI events flow through it, subscribers filter by transport /
# session_id / event type. Replaces the per-endpoint queues we used in early
# 0.2.x — they were over-designed for a phantom multi-transport-in-one-process
# case that doesn't happen in real deployments.

GLOBAL: "FfiQueue" = FfiQueue()
"""Process-wide FfiQueue singleton — mirror of LiveKit's
``FfiClient.instance.queue``. Imported as
``from agent_transport._ffi_queue import GLOBAL`` everywhere.

Tests that need to swap the broker should use :func:`get_global` and
:func:`set_global`, NOT mutate this name directly (avoids stale-import
hazards when modules are reloaded)."""


GLOBAL_DICT: "FfiQueue" = FfiQueue()
"""Process-wide dict-shaped FfiQueue — sibling of :data:`GLOBAL`.

LiveKit-shape consumers subscribe to :data:`GLOBAL` and receive
``FfiEvent`` dataclass instances. Pipecat-shape consumers subscribe
here and receive the raw Rust event dict (``{"type": "...",
"session_id": "...", ...}``) directly. The event sink fans out to
both, so a process can host LiveKit + pipecat adapters
simultaneously without either fighting the other for the single
``set_event_sink`` slot."""


def get_global() -> "FfiQueue":
    """Accessor for the singleton — preferred over direct ``GLOBAL`` access
    in code that may need to be re-bound under test."""
    return GLOBAL


def set_global(queue: "FfiQueue") -> None:
    """Test hook: replace the singleton. Production code should not call
    this. Used by ``test_event_shape.py`` etc. to verify behaviour without
    cross-test contamination."""
    global GLOBAL
    GLOBAL = queue
