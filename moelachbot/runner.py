from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol


class TaskRunner(Protocol):
    def submit(self, fn: Callable[[], None]) -> None: ...


class ThreadTaskRunner:
    """Production runner: a daemon thread, so a pending post never keeps the
    process alive after shutdown.

    Note it does NOT own the delay. The original sleeps *inside* the posting
    try/except, so an interrupted sleep is swallowed and logged; if the runner
    slept instead, that exception would escape to threading.excepthook.
    """

    def submit(self, fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True).start()

