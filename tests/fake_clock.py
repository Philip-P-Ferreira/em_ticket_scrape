"""A deterministic ``Clock``: ``wait()`` advances time instantly and honours ``stop``."""

import threading
from datetime import datetime, timedelta


class FakeClock:
    def __init__(self, start: datetime, stop: threading.Event | None = None) -> None:
        self._now = start
        self.stop = stop if stop is not None else threading.Event()
        self.waits: list[float] = []

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        if self.stop.is_set():
            return True
        self.advance(max(0.0, seconds))
        return self.stop.is_set()
