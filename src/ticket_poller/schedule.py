"""Time: an injectable clock plus pure scheduling math (jitter, backoff, canary, outage).

Everything except ``SystemClock`` is pure. Instants are timezone-aware; local wall-clock
rules (the 08:00 canary) are evaluated in the configured zone, so DST is handled by
``zoneinfo``.
"""

import random
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Final, Protocol, Self

from ticket_poller.config import Settings
from ticket_poller.models import PollOutcome
from ticket_poller.store import RuntimeState

FAILURE_JITTER_FRACTION: Final = 0.2


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, timezone-aware."""
        ...

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; return True if stop was requested."""
        ...


class SystemClock:
    """The real clock; waits are cut short by the shared ``stop`` event."""

    def __init__(self, stop: threading.Event) -> None:
        self._stop = stop

    def now(self) -> datetime:
        return datetime.now(UTC)

    def wait(self, seconds: float) -> bool:
        return self._stop.wait(max(0.0, seconds))


@dataclass(frozen=True, slots=True)
class PollTiming:
    """Poll interval and failure backoff, in seconds (§9.1)."""

    interval_s: float
    jitter_s: float
    error_max_s: float
    block_base_s: float
    block_max_s: float

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            interval_s=settings.poll_interval_seconds,
            jitter_s=settings.poll_jitter_seconds,
            error_max_s=settings.error_backoff_max_seconds,
            block_base_s=settings.block_backoff_base_seconds,
            block_max_s=settings.block_backoff_max_seconds,
        )


def jittered(base_s: float, jitter_s: float, rng: random.Random) -> float:
    """``base_s`` plus a uniform offset in ``[-jitter_s, +jitter_s]``, never negative."""
    return max(0.0, base_s + rng.uniform(-jitter_s, jitter_s))


def exponential_backoff(attempt: int, base_s: float, max_s: float) -> float:
    """``base_s * 2^(attempt-1)``, capped at ``max_s``; ``attempt`` counts from 1."""
    if attempt < 1:
        raise ValueError("attempt counts from 1")
    return min(base_s * 2.0 ** min(attempt - 1, 32), max_s)


def failure_delay(outcome: PollOutcome, streak: int, timing: PollTiming) -> float:
    """Un-jittered delay after the ``streak``-th consecutive failure of this kind.

    ERROR backs off from the poll interval (5, 10, 20, 30 min by default);
    BLOCKED from its own base (15, 30, 60 min by default).
    """
    match outcome:
        case PollOutcome.ERROR:
            return exponential_backoff(streak, timing.interval_s, timing.error_max_s)
        case PollOutcome.BLOCKED:
            return exponential_backoff(streak, timing.block_base_s, timing.block_max_s)
        case _:
            raise ValueError(f"{outcome} is not a failure")


def poll_delay(outcome: PollOutcome, streak: int, timing: PollTiming, rng: random.Random) -> float:
    """Jittered delay before the next poll: the interval after success, else the backoff."""
    if outcome in (PollOutcome.OK, PollOutcome.PARTIAL):
        return jittered(timing.interval_s, timing.jitter_s, rng)
    delay = failure_delay(outcome, streak, timing)
    return jittered(delay, delay * FAILURE_JITTER_FRACTION, rng)


def canary_due(now: datetime, last_date: date | None, tz: tzinfo, hour: int) -> bool:
    """True once local time reaches ``hour`` on a day with no canary sent yet."""
    local = now.astimezone(tz)
    return local.time() >= time(hour) and last_date != local.date()


def next_canary_at(now: datetime, tz: tzinfo, hour: int) -> datetime:
    """The first local ``hour``:00 strictly after ``now``, as a UTC instant."""
    local = now.astimezone(tz)
    today = datetime.combine(local.date(), time(hour), tzinfo=tz)
    if today > local:
        return today.astimezone(UTC)
    tomorrow = local.date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(hour), tzinfo=tz).astimezone(UTC)


def outage_deadline(state: RuntimeState, after: timedelta) -> datetime | None:
    """When the outage notice is due, or None if healthy or already announced."""
    if state.failure_started_at is None or state.outage_announced:
        return None
    return state.failure_started_at + after


def next_wake(now: datetime, *candidates: datetime | None) -> datetime:
    """The earliest candidate strictly after ``now``; ``now`` if there is none.

    None and instants at or before ``now`` are ignored, so a deadline already acted on
    (e.g. an outage notice that failed) does not cause a busy loop. Pass the ``now``
    that due work was checked against, so an overdue poll is not skipped.
    """
    future = [instant for instant in candidates if instant is not None and instant > now]
    return min(future, default=now)


def event_over(now: datetime, stop_after: datetime) -> bool:
    return now >= stop_after
