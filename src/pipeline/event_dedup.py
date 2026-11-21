"""Duplicate-delivery guard. In process and bounded: a restart forgets it, which PTB's offset
acknowledgement on a graceful stop makes acceptable (Q5)."""

from __future__ import annotations

from collections import OrderedDict

DEFAULT_CAPACITY = 4096


class RecentEventIds:
    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity
        self._ids: OrderedDict[str, None] = OrderedDict()

    def claim(self, event_id: str) -> bool:
        """True the first time an id is seen; False for a redelivery still remembered."""
        if event_id in self._ids:
            self._ids.move_to_end(event_id)
            return False
        self._ids[event_id] = None
        if len(self._ids) > self._capacity:
            self._ids.popitem(last=False)
        return True
