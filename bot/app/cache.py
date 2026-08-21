"""In-memory LRU, empty on restart (PLAN §6).

Two caches, both keyed by strings and both deliberately non-durable:

* note metadata, so a re-shared link inside one session costs no XHS fetch;
* Telegram file_ids, so re-sending media Telegram already holds costs no CDN
  fetch at all — neither by us nor by Telegram.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, TypeVar

V = TypeVar("V")


class LRU(Generic[V]):
    def __init__(self, maxsize: int = 128, ttl: float = 0.0):
        self.maxsize = max(0, maxsize)
        self.ttl = ttl
        self._items: "OrderedDict[str, tuple[float, V]]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: str) -> V | None:
        entry = self._items.get(key)
        if entry is None:
            self.misses += 1
            return None
        stored_at, value = entry
        if self.ttl and time.monotonic() - stored_at > self.ttl:
            del self._items[key]
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key: str, value: V) -> None:
        if self.maxsize == 0:
            return
        self._items[key] = (time.monotonic(), value)
        self._items.move_to_end(key)
        while len(self._items) > self.maxsize:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()
        self.hits = 0
        self.misses = 0
