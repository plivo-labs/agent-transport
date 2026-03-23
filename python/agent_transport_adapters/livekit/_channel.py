"""Async channel matching LiveKit's utils.aio.Chan line-for-line.

Supports bounded and unbounded modes, close() semantics, and
async iteration that terminates cleanly on close.
"""

import asyncio
import contextlib
from collections import deque
from typing import AsyncIterator, Generic, TypeVar

T = TypeVar("T")


class ChanClosed(Exception):
    pass


class ChanFull(Exception):
    pass


class ChanEmpty(Exception):
    pass


class Chan(Generic[T]):
    """Async channel matching LiveKit's utils.aio.Chan."""

    def __init__(
        self,
        maxsize: int = 0,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._loop = loop or asyncio.get_event_loop()
        self._maxsize = max(maxsize, 0)
        self._close_ev = asyncio.Event()
        self._closed = False
        self._gets: deque[asyncio.Future[T | None]] = deque()
        self._puts: deque[asyncio.Future[T | None]] = deque()
        self._queue: deque[T] = deque()

    def _wakeup_next(self, waiters: deque[asyncio.Future[T | None]]) -> None:
        while waiters:
            waiter = waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                break

    async def send(self, value: T) -> None:
        while self.full() and not self._close_ev.is_set():
            p = self._loop.create_future()
            self._puts.append(p)
            try:
                await p
            except ChanClosed:
                raise
            except:
                p.cancel()
                with contextlib.suppress(ValueError):
                    self._puts.remove(p)

                if not self.full() and not p.cancelled():
                    self._wakeup_next(self._puts)
                raise

        self.send_nowait(value)

    def send_nowait(self, value: T) -> None:
        if self._close_ev.is_set():
            raise ChanClosed

        if self.full():
            raise ChanFull

        self._queue.append(value)
        self._wakeup_next(self._gets)

    async def recv(self) -> T:
        while self.empty() and not self._close_ev.is_set():
            g = self._loop.create_future()
            self._gets.append(g)

            try:
                await g
            except ChanClosed:
                raise
            except BaseException:
                g.cancel()
                with contextlib.suppress(ValueError):
                    self._gets.remove(g)

                if not self.empty() and not g.cancelled():
                    self._wakeup_next(self._gets)

                raise

        return self.recv_nowait()

    def recv_nowait(self) -> T:
        if self.empty():
            if self._close_ev.is_set():
                raise ChanClosed
            else:
                raise ChanEmpty
        item = self._queue.popleft()
        self._wakeup_next(self._puts)
        return item

    def close(self) -> None:
        self._closed = True
        self._close_ev.set()
        for putter in self._puts:
            if not putter.cancelled():
                putter.set_exception(ChanClosed())

        while len(self._gets) > self.qsize():
            getter = self._gets.pop()
            if not getter.cancelled():
                getter.set_exception(ChanClosed())

        while self._gets:
            self._wakeup_next(self._gets)

    @property
    def closed(self) -> bool:
        return self._closed

    def qsize(self) -> int:
        """the number of elements queued (unread) in the channel buffer"""
        return len(self._queue)

    def full(self) -> bool:
        if self._maxsize <= 0:
            return False
        else:
            return self.qsize() >= self._maxsize

    def empty(self) -> bool:
        return not self._queue

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        try:
            return await self.recv()
        except ChanClosed:
            raise StopAsyncIteration from None
