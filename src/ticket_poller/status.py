"""The ``status`` command: a read-only report of the poller's persisted state."""

import statistics
from datetime import datetime, timedelta, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from ticket_poller.config import Settings
from ticket_poller.models import Channel, PollOutcome
from ticket_poller.schedule import canary_due, next_canary_at, outage_deadline
from ticket_poller.store import AlertAttempt, PollRecord, RuntimeState, Store

RECENT_POLLS: Final = 5


def render_status(store: Store, settings: Settings, now: datetime) -> str:
    tz = settings.tz
    state = store.get_state()
    polls = store.recent_polls(RECENT_POLLS)
    return "\n".join(
        [
            "Recent polls:",
            *([f"  {_poll_line(poll, tz)}" for poll in polls] or ["  none yet"]),
            _failure_line(state, settings, tz),
            _baseline_line(store, settings, now),
            _resale_line(state, polls),
            _delivery_line(store.last_alert_attempts(), tz),
            _canary_line(state, settings, now),
            f"Auto-stop: {_local(settings.stop_after, tz)}",
        ]
    )


def _poll_line(poll: PollRecord, tz: tzinfo) -> str:
    elapsed = "" if poll.elapsed_s is None else f"{poll.elapsed_s:.1f}s"
    head = f"{_local(poll.started_at, tz)}  {poll.outcome.value:<7} {elapsed:>6}"
    if poll.outcome not in (PollOutcome.OK, PollOutcome.PARTIAL):
        return f"{head}  {poll.detail}"
    capture = poll.resale_capture.value if poll.resale_capture else "n/a"
    return (
        f"{head}  primary={poll.primary_listings} resale={poll.resale_listings}"
        f" resale_capture={capture} matches={poll.matches} new={poll.new_matches}"
    )


def _failure_line(state: RuntimeState, settings: Settings, tz: tzinfo) -> str:
    recovery = " · recovery notice pending" if state.recovery_pending else ""
    if state.failure_started_at is None:
        return f"Failures: none{recovery}"
    deadline = outage_deadline(state, timedelta(minutes=settings.failure_alert_after_minutes))
    outage = "announced" if deadline is None else f"alert due {_local(deadline, tz)}"
    return (
        f"Failures: {state.consecutive_failures} in a row"
        f" ({state.consecutive_blocks} blocked) since {_local(state.failure_started_at, tz)}"
        f" · outage {outage}{recovery}"
    )


def _baseline_line(store: Store, settings: Settings, now: datetime) -> str:
    samples = store.baseline_samples(now - timedelta(days=settings.baseline_window_days), now)
    warmup = (
        f" · warming up {len(samples)}/{settings.baseline_min_samples}"
        if len(samples) < settings.baseline_min_samples
        else ""
    )
    if not samples:
        return f"Baseline: no samples yet{warmup}"
    median = statistics.median(Decimal(cents) for cents in samples)
    return (
        f"Baseline ({settings.baseline_window_days}-day median of cheapest primary):"
        f" {_dollars(median)} (n={len(samples)}){warmup}"
    )


def _resale_line(state: RuntimeState, polls: list[PollRecord]) -> str:
    eligible = {True: "yes", False: "no", None: "unknown"}[state.resale_eligible]
    capture = next((poll.resale_capture for poll in polls if poll.resale_capture), None)
    return (
        f"Resale: eligible {eligible} · last capture {capture.value if capture else 'n/a'}"
        f" · missing streak {state.resale_missing_streak}"
    )


def _delivery_line(attempts: dict[Channel, AlertAttempt], tz: tzinfo) -> str:
    def describe(channel: Channel) -> str:
        attempt = attempts.get(channel)
        if attempt is None:
            return f"{channel.value} never sent"
        if attempt.ok:
            return f"{channel.value} ok {_local(attempt.at, tz)}"
        return f"{channel.value} FAILED {_local(attempt.at, tz)} (retried next poll)"

    return "Alert delivery: " + " · ".join(describe(channel) for channel in Channel)


def _canary_line(state: RuntimeState, settings: Settings, now: datetime) -> str:
    tz, hour = settings.tz, settings.canary_hour
    last = state.last_canary_date.isoformat() if state.last_canary_date else "never"
    due = canary_due(now, state.last_canary_date, tz, hour)
    upcoming = "due now" if due else _local(next_canary_at(now, tz, hour), tz)
    return f"Canary: last sent {last} · next {upcoming}"


def _local(instant: datetime, tz: tzinfo) -> str:
    return instant.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _dollars(cents: Decimal | int) -> str:
    amount = (Decimal(cents) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${amount:,}"
