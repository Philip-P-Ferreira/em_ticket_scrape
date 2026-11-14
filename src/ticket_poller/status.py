"""Read-only reports of the poller's persisted state: ``status`` and the daily canary."""

from collections.abc import Mapping
from datetime import datetime, timedelta, tzinfo
from typing import Final

from ticket_poller.config import Settings
from ticket_poller.messages import CanaryReport, PricePoint, local_time, money
from ticket_poller.models import Baseline, Channel, PollOutcome
from ticket_poller.rules import compute_baseline
from ticket_poller.schedule import canary_due, next_canary_at, outage_deadline
from ticket_poller.state import PollRecord, RuntimeState
from ticket_poller.store import AlertAttempt, Store

RECENT_POLLS: Final = 5
CANARY_WINDOW: Final = timedelta(hours=24)


def current_baseline(store: Store, settings: Settings, now: datetime) -> Baseline:
    """The baseline over the configured window ending at ``now`` (rules.compute_baseline)."""
    samples = store.baseline_samples(now - timedelta(days=settings.baseline_window_days), now)
    return compute_baseline(samples, settings.baseline_min_samples)


def canary_report(store: Store, settings: Settings, now: datetime) -> CanaryReport:
    """Gather the daily canary's figures (the last 24 h of polls, plus current state)."""
    stats = store.stats_since(now - CANARY_WINDOW)
    state = store.get_state()
    return CanaryReport(
        now=now,
        outcomes=stats.outcomes,
        last_success_at=stats.last_success_at,
        failing_since=state.failure_started_at,
        consecutive_failures=state.consecutive_failures,
        primary_now=_price_points(stats.cheapest_primary, stats.cheapest_primary_label),
        resale_now=_price_points(stats.cheapest_resale, stats.cheapest_resale_label),
        primary_low=stats.low_primary,
        resale_low=stats.low_resale,
        baseline=current_baseline(store, settings, now),
        resale_eligible=state.resale_eligible,
        resale_captured_polls=stats.resale_captured,
        resale_listings_now=stats.resale_listings_now,
        alerts=stats.alerts,
        great_deals=stats.top_alerts,
        channel_failures=stats.channel_failures,
        browser_recycles=stats.browser_recycles,
        resale_missing_polls=stats.resale_missing,
        parse_warning_polls=stats.parse_warning_polls,
        parse_degraded_polls=stats.parse_degraded_polls,
    )


def _price_points(
    cents: Mapping[int, int | None], labels: Mapping[int, str | None]
) -> dict[int, PricePoint | None]:
    return {
        qty: None if price is None else PricePoint(price, labels.get(qty))
        for qty, price in cents.items()
    }


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
            f"Auto-stop: {local_time(settings.stop_after, tz)}",
        ]
    )


def _poll_line(poll: PollRecord, tz: tzinfo) -> str:
    elapsed = "" if poll.elapsed_s is None else f"{poll.elapsed_s:.1f}s"
    head = f"{local_time(poll.started_at, tz)}  {poll.outcome.value:<7} {elapsed:>6}"
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
    outage = "announced" if deadline is None else f"alert due {local_time(deadline, tz)}"
    return (
        f"Failures: {state.consecutive_failures} in a row"
        f" ({state.consecutive_blocks} blocked) since {local_time(state.failure_started_at, tz)}"
        f" · outage {outage}{recovery}"
    )


def _baseline_line(store: Store, settings: Settings, now: datetime) -> str:
    baseline = current_baseline(store, settings, now)
    warmup = (
        ""
        if baseline.ready
        else f" · warming up {baseline.samples}/{settings.baseline_min_samples}"
    )
    if baseline.median_cents is None:
        return f"Baseline: no samples yet{warmup}"
    return (
        f"Baseline ({settings.baseline_window_days}-day median of cheapest primary):"
        f" {money(baseline.median_cents)} (n={baseline.samples}){warmup}"
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
            return f"{channel.value} ok {local_time(attempt.at, tz)}"
        return f"{channel.value} FAILED {local_time(attempt.at, tz)} (retried next poll)"

    return "Alert delivery: " + " · ".join(describe(channel) for channel in Channel)


def _canary_line(state: RuntimeState, settings: Settings, now: datetime) -> str:
    tz, hour = settings.tz, settings.canary_hour
    last = state.last_canary_date.isoformat() if state.last_canary_date else "never"
    due = canary_due(now, state.last_canary_date, tz, hour)
    upcoming = "due now" if due else local_time(next_canary_at(now, tz, hour), tz)
    return f"Canary: last sent {last} · next {upcoming}"
