import random
import threading
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.fake_clock import FakeClock
from tests.sample_env import EnvFileFactory
from ticket_poller.config import load_settings
from ticket_poller.models import PollOutcome
from ticket_poller.schedule import (
    Clock,
    PollTiming,
    SystemClock,
    canary_due,
    event_over,
    exponential_backoff,
    failure_delay,
    jittered,
    next_canary_at,
    next_wake,
    outage_deadline,
    poll_delay,
)
from ticket_poller.store import RuntimeState

CHICAGO = ZoneInfo("America/Chicago")
T0 = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
TIMING = PollTiming(
    interval_s=300, jitter_s=60, error_max_s=1800, block_base_s=900, block_max_s=3600
)
OUTAGE_AFTER = timedelta(minutes=60)


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class StubRandom(random.Random):
    """Always draws one end of the range."""

    def __init__(self, end: str) -> None:
        super().__init__()
        self.end = end

    def uniform(self, a: float, b: float) -> float:
        return a if self.end == "low" else b


class TestJitterAndBackoff:
    @pytest.mark.parametrize(("end", "expected"), [("low", 240.0), ("high", 360.0)])
    def test_jitter_bounds(self, end: str, expected: float) -> None:
        assert jittered(300, 60, StubRandom(end)) == expected

    def test_jitter_stays_in_range(self) -> None:
        rng = random.Random(7)
        delays = [jittered(300, 60, rng) for _ in range(1000)]

        assert all(240 <= delay <= 360 for delay in delays)
        assert len(set(delays)) > 900

    def test_jitter_is_never_negative(self) -> None:
        assert jittered(10, 60, StubRandom("low")) == 0.0

    def test_exponential_backoff_doubles_then_caps(self) -> None:
        delays = [exponential_backoff(n, 300, 1800) for n in range(1, 6)]

        assert delays == [300, 600, 1200, 1800, 1800]
        assert exponential_backoff(10_000, 300, 1800) == 1800

    def test_backoff_attempts_count_from_one(self) -> None:
        with pytest.raises(ValueError, match="from 1"):
            exponential_backoff(0, 300, 1800)

    @pytest.mark.parametrize(
        ("outcome", "minutes"),
        [(PollOutcome.ERROR, [5, 10, 20, 30, 30]), (PollOutcome.BLOCKED, [15, 30, 60, 60, 60])],
    )
    def test_failure_delay_sequences(self, outcome: PollOutcome, minutes: list[int]) -> None:
        delays = [failure_delay(outcome, n, TIMING) / 60 for n in range(1, 6)]

        assert delays == minutes

    @pytest.mark.parametrize("outcome", [PollOutcome.OK, PollOutcome.PARTIAL])
    def test_failure_delay_rejects_success(self, outcome: PollOutcome) -> None:
        with pytest.raises(ValueError, match="not a failure"):
            failure_delay(outcome, 1, TIMING)

    @pytest.mark.parametrize("outcome", [PollOutcome.OK, PollOutcome.PARTIAL])
    def test_poll_delay_after_success_is_interval_with_jitter(self, outcome: PollOutcome) -> None:
        assert poll_delay(outcome, 0, TIMING, StubRandom("low")) == 240
        assert poll_delay(outcome, 0, TIMING, StubRandom("high")) == 360

    def test_poll_delay_after_failure_jitters_the_backoff_by_20_percent(self) -> None:
        assert poll_delay(PollOutcome.BLOCKED, 2, TIMING, StubRandom("low")) == 1440
        assert poll_delay(PollOutcome.BLOCKED, 2, TIMING, StubRandom("high")) == 2160

    def test_timing_from_settings(self, make_env_file: EnvFileFactory) -> None:
        make_env_file(POLL_INTERVAL_SECONDS="240", BLOCK_BACKOFF_BASE_SECONDS="600")

        timing = PollTiming.from_settings(load_settings())

        assert timing == PollTiming(240, 60, 1800, 600, 3600)


class TestCanary:
    @pytest.mark.parametrize(
        ("now", "last", "due"),
        [
            (utc(2026, 11, 14, 13, 59), None, False),  # 07:59 CST
            (utc(2026, 11, 14, 14, 0), None, True),  # 08:00 CST
            (utc(2026, 11, 14, 14, 0), date(2026, 11, 14), False),  # already sent today
            (utc(2026, 11, 15, 3, 0), date(2026, 11, 13), True),  # 21:00 CST, sent yesterday
            (utc(2026, 11, 15, 5, 59), date(2026, 11, 14), False),  # 23:59 CST on the 14th
        ],
    )
    def test_canary_due(self, now: datetime, last: date | None, due: bool) -> None:
        assert canary_due(now, last, CHICAGO, 8) is due

    @pytest.mark.parametrize(
        ("before", "at"),
        [
            # Spring forward (2027-03-14): 08:00 is CDT, UTC-5.
            (utc(2027, 3, 14, 12, 59), utc(2027, 3, 14, 13, 0)),
            # Fall back (2027-11-07): 08:00 is CST, UTC-6.
            (utc(2027, 11, 7, 13, 59), utc(2027, 11, 7, 14, 0)),
        ],
    )
    def test_canary_due_at_local_8_on_dst_days(self, before: datetime, at: datetime) -> None:
        last = before.date() - timedelta(days=1)

        assert canary_due(before, last, CHICAGO, 8) is False
        assert canary_due(at, last, CHICAGO, 8) is True

    def test_next_canary_later_today(self) -> None:
        assert next_canary_at(utc(2026, 11, 14, 12, 0), CHICAGO, 8) == utc(2026, 11, 14, 14, 0)

    def test_next_canary_at_8_is_tomorrow(self) -> None:
        assert next_canary_at(utc(2026, 11, 14, 14, 0), CHICAGO, 8) == utc(2026, 11, 15, 14, 0)

    def test_spring_forward_day_is_23_hours(self) -> None:
        sent = utc(2027, 3, 13, 14, 0)  # 08:00 CST

        following = next_canary_at(sent, CHICAGO, 8)

        assert following == utc(2027, 3, 14, 13, 0)  # 08:00 CDT
        assert following - sent == timedelta(hours=23)
        assert following.astimezone(CHICAGO).hour == 8

    def test_fall_back_day_is_25_hours(self) -> None:
        sent = utc(2027, 11, 6, 13, 0)  # 08:00 CDT

        following = next_canary_at(sent, CHICAGO, 8)

        assert following == utc(2027, 11, 7, 14, 0)  # 08:00 CST
        assert following - sent == timedelta(hours=25)


class TestOutageAndWake:
    def test_no_deadline_when_healthy(self) -> None:
        assert outage_deadline(RuntimeState(), OUTAGE_AFTER) is None

    def test_deadline_is_failure_start_plus_threshold(self) -> None:
        state = RuntimeState().record_failure(PollOutcome.BLOCKED, T0)

        assert outage_deadline(state, OUTAGE_AFTER) == T0 + OUTAGE_AFTER

    def test_no_deadline_once_announced(self) -> None:
        state = RuntimeState().record_failure(PollOutcome.ERROR, T0).record_outage_notice(True)

        assert outage_deadline(state, OUTAGE_AFTER) is None

    def test_next_wake_picks_earliest_future_instant(self) -> None:
        later, sooner = T0 + timedelta(minutes=30), T0 + timedelta(minutes=5)

        assert next_wake(T0, later, None, sooner) == sooner

    def test_next_wake_ignores_past_and_present(self) -> None:
        future = T0 + timedelta(hours=1)

        assert next_wake(T0, T0 - timedelta(seconds=1), T0, future) == future

    def test_next_wake_without_candidates_is_now(self) -> None:
        assert next_wake(T0, None, T0 - timedelta(minutes=1)) == T0

    def test_event_over(self) -> None:
        stop_after = datetime(2027, 4, 28, tzinfo=CHICAGO)

        assert event_over(stop_after - timedelta(seconds=1), stop_after) is False
        assert event_over(stop_after, stop_after) is True


def simulate_blocked(notice_results: list[bool], until_minutes: int) -> tuple[list[int], list[int]]:
    """Drive the §8 wake logic with a FakeClock while every poll is BLOCKED.

    Returns the minutes at which polls ran and outage notices were attempted.
    """
    clock = FakeClock(T0)
    state, next_poll = RuntimeState(), clock.now()
    polls: list[int] = []
    notices: list[int] = []

    def minute(instant: datetime) -> int:
        return int((instant - T0).total_seconds() // 60)

    while clock.now() < T0 + timedelta(minutes=until_minutes):
        now = clock.now()
        deadline = outage_deadline(state, OUTAGE_AFTER)
        if deadline is not None and now >= deadline:
            notices.append(minute(now))
            state = state.record_outage_notice(delivered=notice_results.pop(0))
        if now >= next_poll:
            polls.append(minute(now))
            state = state.record_failure(PollOutcome.BLOCKED, now)
            delay = failure_delay(PollOutcome.BLOCKED, state.consecutive_blocks, TIMING)
            next_poll = now + timedelta(seconds=delay)
        wake = next_wake(now, next_poll, outage_deadline(state, OUTAGE_AFTER))
        clock.wait((wake - clock.now()).total_seconds())
    return polls, notices


class TestBlockedTimeline:
    def test_outage_alert_fires_at_60_minutes_between_polls(self) -> None:
        polls, notices = simulate_blocked([True], until_minutes=110)

        assert polls == [0, 15, 45, 105]
        assert notices == [60]

    def test_failed_notice_is_retried_at_next_wake_without_busy_loop(self) -> None:
        polls, notices = simulate_blocked([False, True], until_minutes=170)

        assert polls == [0, 15, 45, 105, 165]
        assert notices == [60, 105]


class TestClocks:
    def test_fake_clock_is_a_clock(self) -> None:
        clock: Clock = FakeClock(T0)

        assert clock.wait(90) is False
        assert clock.now() == T0 + timedelta(seconds=90)

    def test_fake_clock_stops(self) -> None:
        clock = FakeClock(T0)
        clock.stop.set()

        assert clock.wait(90) is True
        assert clock.now() == T0

    def test_system_clock_now_is_aware_utc(self) -> None:
        now = SystemClock(threading.Event()).now()

        assert now.tzinfo is UTC

    def test_system_clock_wait_returns_false_after_timeout(self) -> None:
        assert SystemClock(threading.Event()).wait(0.001) is False
        assert SystemClock(threading.Event()).wait(-5) is False

    def test_system_clock_wait_is_cut_short_by_stop(self) -> None:
        stop = threading.Event()
        stop.set()

        assert SystemClock(stop).wait(3600) is True
