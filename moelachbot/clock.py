from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...

    def ctime(self, timestamp: float) -> str: ...


class SystemClock:
    """The real clock. ``ctime`` is on the protocol because it is the one call
    whose output depends on the ambient timezone."""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def ctime(self, timestamp: float) -> str:
        return time.ctime(timestamp)
