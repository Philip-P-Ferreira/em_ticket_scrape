"""An in-process hang watchdog for the service loop.

Some Playwright calls (``Page.title``/``content``, ``BrowserContext.close``,
``Response.body``) take no timeout, so a hung renderer could block the loop forever. The
loop marks itself busy at the start of each step (a poll, the notices, a browser close)
and idle before it waits; a daemon thread exits the process if one step stays busy longer
than the limit, so systemd's ``Restart=on-failure`` starts a fresh service. Idle waits —
the poll interval and every failure backoff — never count.
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Final

log = logging.getLogger(__name__)

EXIT_CODE: Final = 1
"""A plain failure exit: ``Restart=on-failure`` restarts it."""


class Watchdog:
    """Busy/idle heartbeat plus a checker; ``exit_fn`` and ``monotonic`` are injectable."""

    def __init__(
        self,
        timeout_s: float,
        *,
        exit_fn: Callable[[int], object] = os._exit,
        monotonic: Callable[[], float] = time.monotonic,
        interval_s: float | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._exit = exit_fn
        self._monotonic = monotonic
        self._interval_s = interval_s if interval_s is not None else min(30.0, timeout_s / 4)
        self._lock = threading.Lock()
        self._busy: tuple[str, float] | None = None
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    def busy(self, what: str) -> None:
        """A step starts now; its own limit starts now too."""
        with self._lock:
            self._busy = (what, self._monotonic())

    def idle(self) -> None:
        with self._lock:
            self._busy = None

    def check(self) -> bool:
        """Exit (via ``exit_fn``) if a step is overdue; True if it fired."""
        with self._lock:
            busy = self._busy
        if busy is None:
            return False
        what, since = busy
        elapsed = self._monotonic() - since
        if elapsed <= self._timeout_s:
            return False
        log.critical(
            "watchdog: %s busy for %.0fs (limit %.0fs); exiting for a restart",
            what,
            elapsed,
            self._timeout_s,
        )
        self._exit(EXIT_CODE)
        return True

    def start(self) -> None:
        self._thread = threading.Thread(target=self._watch, name="watchdog", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _watch(self) -> None:
        while not self._closed.wait(self._interval_s):
            if self.check():
                return


class NoWatchdog(Watchdog):
    """For one-shot commands and tests: marks are accepted and nothing ever fires."""

    def __init__(self) -> None:
        super().__init__(float("inf"), exit_fn=lambda _code: None)
