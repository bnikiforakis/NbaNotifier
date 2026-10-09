"""A small TTL cache for awaitables, shared by the game trackers and the ``/nba`` command."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

FETCH_TIMEOUT = 5.0  # well inside PiButler's 8 s command timeout


class TtlCache:
    """Caches awaitable results for ``ttl`` seconds; concurrent callers share one in-flight fetch.

    Failures aren't cached. A fetch that outlives ``timeout`` keeps running and fills the cache for
    the next caller, while this one gets ``TimeoutError``.
    """

    def __init__(
        self,
        ttl: float,
        timeout: float | None = FETCH_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ttl = ttl
        self.timeout = timeout
        self.clock = clock
        self._entries: dict[Any, tuple[float, asyncio.Future]] = {}

    async def get(self, key: Any, fetch: Callable[[], Awaitable[Any]]) -> Any:
        now = self.clock()
        entry = self._entries.get(key)
        if entry is None or (entry[0] <= now and entry[1].done()) or _failed(entry[1]):
            # An unfinished fetch is never replaced: a slow upstream gets one request, not many.
            self._entries = {k: e for k, e in self._entries.items() if e[0] > now or not e[1].done()}
            future = asyncio.ensure_future(fetch())
            future.add_done_callback(_failed)  # retrieves the error even if every caller timed out
            entry = (now + self.ttl, future)
            self._entries[key] = entry
        try:
            return await asyncio.wait_for(asyncio.shield(entry[1]), self.timeout)
        except Exception:
            if self._entries.get(key) is entry and entry[1].done():
                del self._entries[key]
            raise


def _failed(future: asyncio.Future) -> bool:
    return future.done() and (future.cancelled() or future.exception() is not None)
