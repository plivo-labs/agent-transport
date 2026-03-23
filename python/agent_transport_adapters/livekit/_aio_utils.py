"""Async utilities matching LiveKit's utils.aio."""

import asyncio
import functools
from typing import Any


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
