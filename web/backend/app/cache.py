"""Cache à durée de vie courte avec dédoublonnage des requêtes concurrentes (single-flight)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


class TTLCache:
    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _fresh(self, key: str) -> tuple[bool, Any]:
        hit = self._data.get(key)
        if hit is not None and time.monotonic() - hit[0] < self.ttl:
            return True, hit[1]
        return False, None

    async def get(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        ok, value = self._fresh(key)
        if ok:
            return value
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            ok, value = self._fresh(key)
            if ok:
                return value
            value = await factory()
            self._data[key] = (time.monotonic(), value)
            return value

    def invalidate(self, prefix: str = "") -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            del self._data[key]
