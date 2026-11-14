"""The hang watchdog: the exit function and the monotonic clock are injected."""

import logging
import threading

import pytest

from ticket_poller.watchdog import NoWatchdog, Watchdog


class Monotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def mono() -> Monotonic:
    return Monotonic()


def make(mono: Monotonic, exits: list[int], timeout_s: float = 900) -> Watchdog:
    return Watchdog(timeout_s, exit_fn=exits.append, monotonic=mono)


def test_a_step_busy_past_the_limit_logs_critical_and_exits_1(
    mono: Monotonic, caplog: pytest.LogCaptureFixture
) -> None:
    exits: list[int] = []
    watchdog = make(mono, exits)

    watchdog.busy("poll")
    mono.now += 900
    assert watchdog.check() is False
    mono.now += 1
    assert watchdog.check() is True

    assert exits == [1]
    (record,) = caplog.records
    assert record.levelno == logging.CRITICAL
    assert record.getMessage() == (
        "watchdog: poll busy for 901s (limit 900s); exiting for a restart"
    )


def test_idle_never_fires_however_long_the_wait(mono: Monotonic) -> None:
    exits: list[int] = []
    watchdog = make(mono, exits)

    assert watchdog.check() is False  # never marked busy
    watchdog.busy("poll")
    mono.now += 60
    watchdog.idle()
    mono.now += 24 * 3600  # a long sleep or a 1 h failure backoff
    assert watchdog.check() is False
    assert exits == []


def test_each_new_step_restarts_the_limit(mono: Monotonic) -> None:
    exits: list[int] = []
    watchdog = make(mono, exits)

    watchdog.busy("poll")
    mono.now += 800
    watchdog.busy("notices")
    mono.now += 800

    assert watchdog.check() is False
    assert exits == []


def test_the_thread_checks_periodically(mono: Monotonic) -> None:
    fired = threading.Event()
    watchdog = Watchdog(10, exit_fn=lambda _code: fired.set(), monotonic=mono, interval_s=0.01)
    watchdog.start()
    try:
        watchdog.busy("browser close")
        mono.now += 11
        assert fired.wait(5)
    finally:
        watchdog.close()


def test_close_stops_the_thread(mono: Monotonic) -> None:
    exits: list[int] = []
    watchdog = Watchdog(10, exit_fn=exits.append, monotonic=mono, interval_s=0.01)
    watchdog.start()
    watchdog.close()

    watchdog.busy("poll")
    mono.now += 100
    threading.Event().wait(0.05)
    assert exits == []


def test_no_watchdog_never_fires() -> None:
    watchdog = NoWatchdog()
    watchdog.busy("poll")

    assert watchdog.check() is False
