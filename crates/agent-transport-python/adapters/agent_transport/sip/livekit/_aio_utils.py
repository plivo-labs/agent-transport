"""Async utilities matching LiveKit's utils.aio."""

import asyncio
import concurrent.futures
import functools
import inspect
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ─── Dedicated call-control executor ─────────────────────────────────────────
#
# Blocking call-control ops (notably ep.hangup, a Rust block_on doing a Plivo
# REST DELETE / SIP BYE) must not run inline on the asyncio loop thread — that
# stalls every session sharing the loop. They are scheduled off-loop via
# run_in_executor. They must ALSO not share asyncio's DEFAULT ThreadPoolExecutor
# with the per-call audio hot path: each active call permanently occupies one
# default-pool worker via ``recv_audio_bytes_blocking``, so a burst of slow
# hangups (mass disconnect / provider outage) on the default pool would starve
# live calls' inbound audio. A dedicated, bounded pool caps that blast radius.

_control_executor: Optional["concurrent.futures.ThreadPoolExecutor"] = None


def control_executor() -> "concurrent.futures.ThreadPoolExecutor":
    """Lazily-created dedicated thread pool for blocking call-control ops."""
    global _control_executor
    if _control_executor is None:
        _control_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="at-callctl"
        )
    return _control_executor


def _drain_exception(fut: "asyncio.Future") -> None:
    """Retrieve a fire-and-forget future's exception so it is observable and
    never left unretrieved on an orphaned Future.

    ``loop.run_in_executor`` returns an asyncio.Future; calling ``exception()``
    on a cancelled one raises ``CancelledError`` (a ``BaseException``, so not
    caught by ``except Exception``). Guard the cancelled case explicitly so this
    done-callback never re-raises into the loop's exception handler."""
    if fut.cancelled():
        return
    try:
        exc = fut.exception()
    except asyncio.CancelledError:
        return
    except Exception:
        return
    if exc is not None:
        logger.debug("async call-control op failed: %s", exc)


def schedule_hangup(fn: Callable[..., Any], *args: Any) -> None:
    """Fire-and-forget a blocking hangup off the event loop, on the dedicated
    control executor. Used from SYNCHRONOUS callbacks that run on the loop
    thread (``session.on("close")``, ``JobContext.shutdown``) and must return
    without blocking. Falls back to an inline call when there is no running
    loop. Any exception is retrieved and logged — never dropped silently nor
    left on an orphaned Future."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            fn(*args)
        except Exception:
            logger.debug("inline hangup (no running loop) failed", exc_info=True)
        return
    fut = loop.run_in_executor(control_executor(), fn, *args)
    fut.add_done_callback(_drain_exception)


def _release_waiter(waiter: asyncio.Future[Any], *_: Any) -> None:
    if not waiter.done():
        waiter.set_result(None)


async def cancel_and_wait(*futures: asyncio.Future[Any]) -> None:
    """Cancel futures and wait for them to complete.

    Exact copy of LiveKit's utils.aio.cancel_and_wait.
    """
    loop = asyncio.get_running_loop()
    waiters = []

    for fut in futures:
        waiter = loop.create_future()
        cb = functools.partial(_release_waiter, waiter)
        waiters.append((waiter, cb))
        fut.add_done_callback(cb)
        fut.cancel()

    try:
        for waiter, _ in waiters:
            await waiter
    finally:
        for i, fut in enumerate(futures):
            _, cb = waiters[i]
            fut.remove_done_callback(cb)


async def call_setup(setup_fnc: Callable, proc: Any) -> None:
    """Invoke a user-provided setup function, supporting both sync and async.

    Supports both calling conventions:
    - New: ``setup_fnc(proc)`` — function receives a JobProcess-like object
           whose ``userdata`` dict should be populated in place.
    - Old: ``setup_fnc()`` that returns a dict — the returned dict is
           assigned to ``proc.userdata``.

    Gracefully handles coroutine functions (``async def``) and regular
    functions that return awaitables (e.g., ``return asyncio.gather(...)``).

    Raises any exception from the setup function so the caller can log
    and abort startup cleanly.
    """
    is_coro = inspect.iscoroutinefunction(setup_fnc)

    # Try new pattern: setup_fnc(proc)
    try:
        result = setup_fnc(proc) if not is_coro else await setup_fnc(proc)
    except TypeError:
        # Old pattern: setup_fnc() -> dict
        result = setup_fnc() if not is_coro else await setup_fnc()
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict):
            proc.userdata = result
        return

    # New-pattern result: if it's an awaitable (e.g., sync fn returning coroutine),
    # await it. Otherwise it's None (fn mutated proc.userdata directly).
    if inspect.isawaitable(result):
        await result


async def close_session_services(
    session: Any,
    *,
    per_service_timeout: float = 2.0,
    logger: "logging.Logger | None" = None,
) -> None:
    """Best-effort ``aclose()`` of user-supplied STT/TTS/LLM on session end.

    Upstream livekit-agents runs every job in its own subprocess; when the
    subprocess exits, the OS reclaims the WebSocket FDs that STT/TTS/LLM
    plugins keep open against their vendors. Agent-transport's LiveKit
    adapter instead runs every call as an in-process asyncio task on a
    long-lived server, so those FDs would leak per call unless we close
    each service explicitly. ``AgentSession.aclose()`` itself does not
    cascade into the user-supplied services — they're treated as caller-
    owned in upstream and freed by the process exit. Verified against
    livekit-agents 1.5.15: ``AgentSession._aclose_impl`` closes the
    activity, recorder/room IO and toolsets but never touches
    ``self._stt`` / ``self._tts`` / ``self._llm``.

    Ownership model: any STT/TTS/LLM attached to an ``AgentSession`` is
    considered session-owned and is closed when that session ends. This
    matches upstream's per-subprocess lifecycle, where each call effectively
    gets its own services anyway. Process-scoped resources that must
    survive across calls (canonically the VAD, via ``proc.userdata['vad']``)
    are *not* touched — only ``session.stt``/``tts``/``llm`` is walked.
    Plugging a single STT/TTS/LLM into many concurrent sessions is not
    supported: most plugins carry per-stream state (transcripts, history,
    audio buffers) and would corrupt regardless of any cleanup behavior.

    Walks ``session.stt`` / ``tts`` / ``llm`` and closes each independently
    with a small timeout so a hung peer cannot block call teardown. Never
    raises; logs at WARNING on timeout/error if a logger is provided.

    The canonical close method on all four kinds (STT, TTS, LLM and
    RealtimeModel) in livekit-agents is ``aclose()``; the base
    implementations are no-ops that plugins override to drop their vendor
    sockets. We therefore only look for ``aclose``.
    """
    if session is None:
        return

    services = (
        ("stt", getattr(session, "stt", None)),
        ("tts", getattr(session, "tts", None)),
        ("llm", getattr(session, "llm", None)),
    )

    for kind, svc in services:
        if svc is None:
            continue
        aclose = getattr(svc, "aclose", None)
        if not callable(aclose):
            continue

        started = time.monotonic()
        try:
            await asyncio.wait_for(aclose(), per_service_timeout)
        except asyncio.TimeoutError:
            if logger is not None:
                logger.warning(
                    "Timed out closing %s after %.1fs", kind, per_service_timeout,
                )
        except Exception:
            if logger is not None:
                logger.warning("Error closing %s", kind, exc_info=True)
        else:
            if logger is not None:
                logger.info(
                    "Closed %s in %.1fms", kind, (time.monotonic() - started) * 1000.0,
                )
