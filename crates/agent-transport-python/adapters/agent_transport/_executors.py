"""Shared thread-pool executors for blocking Rust calls invoked from asyncio.

Both the LiveKit and pipecat adapters bridge blocking Rust endpoint calls onto
the asyncio loop via ``run_in_executor``. Two of those call shapes must NOT use
asyncio's *default* ThreadPoolExecutor:

  * The per-call inbound-audio forward loop parks one worker ~continuously in
    ``recv_audio_bytes_blocking`` (it blocks until the next 20ms frame). The
    default pool has only ``min(32, cpu+4)`` workers, so on the default pool a
    process caps out at ~12-32 concurrent calls and every other default-pool
    user (DNS, other blocking calls) starves behind the parked recv threads.

``recv_audio_bytes_blocking`` releases the GIL for the entire wait (the PyO3
binding runs it inside ``py.detach``), so a parked recv thread holds no GIL and
burns no CPU — it only touches the GIL for the microsecond hand-off when a frame
returns. That makes a large dedicated pool cheap: size it for the target
concurrent-call count rather than the core count.
"""

import concurrent.futures
import os
from typing import Optional

_audio_io_executor: Optional["concurrent.futures.ThreadPoolExecutor"] = None


def audio_io_executor() -> "concurrent.futures.ThreadPoolExecutor":
    """Lazily-created dedicated pool for per-call blocking audio recv loops.

    Sized for concurrent-call count (one parked thread per active call), not core
    count — parked recv threads are GIL-free and cheap. Override the size with
    ``AGENT_TRANSPORT_AUDIO_IO_THREADS`` (default 512).
    """
    global _audio_io_executor
    if _audio_io_executor is None:
        try:
            n = int(os.environ.get("AGENT_TRANSPORT_AUDIO_IO_THREADS", "512"))
        except ValueError:
            n = 512
        n = max(1, n)
        _audio_io_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=n, thread_name_prefix="at-audioio"
        )
    return _audio_io_executor
