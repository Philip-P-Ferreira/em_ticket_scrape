"""Poll orchestration: one pipeline shared by ``poll-once`` and the ``run`` service loop.

A poll is fetch → parse → resale capture → baseline/rules → resolve → pending (§8). A dry
run stops there; a live poll then records it, delivers per channel, pings healthchecks and
sends the resale notices. ``run`` schedules polls, the outage/recovery notices, the daily
canary and the auto-stop, each at its own wake-up time.
"""

import logging
import random
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

from ticket_poller import messages
from ticket_poller.config import Settings
from ticket_poller.dumps import try_write_dump
from ticket_poller.fetcher import Fetcher
from ticket_poller.logsetup import redact
from ticket_poller.models import (
    Baseline,
    Channel,
    FetchResult,
    Inventory,
    InventoryType,
    Listing,
    Match,
    NoticeKind,
    PollOutcome,
    PriceSnapshot,
    ResaleCapture,
)
from ticket_poller.notify import AuditHook, Delivery, Notifier
from ticket_poller.parsing import ParseError, parse_inventory, resale_capture
from ticket_poller.rules import candidates, compute_baseline, evaluate, price_snapshot
from ticket_poller.schedule import (
    Clock,
    PollTiming,
    canary_due,
    event_over,
    failure_delay,
    next_canary_at,
    next_wake,
    outage_deadline,
    poll_delay,
)
from ticket_poller.status import canary_report, current_baseline
from ticket_poller.store import PollRecord, Resolved, RuntimeState, Store, key_matches
from ticket_poller.watchdog import NoWatchdog, Watchdog

log = logging.getLogger(__name__)

RESALE_MISSING_POLLS: Final = 3
"""Consecutive MISSING polls before the ``resale_missing`` notice (§7)."""

SUCCESS: Final = frozenset({PollOutcome.OK, PollOutcome.PARTIAL})

MAX_WAIT_SECONDS: Final = 60.0
"""Longest single ``Event.wait`` slice: a wall-clock step is noticed within a minute."""

CLOCK_STEP_TOLERANCE: Final = timedelta(seconds=5)


EMAIL_FAILURES_BEFORE_PUSH: Final = 3
"""Consecutive failed emails (any kind) before the one-time "email failing" ops push; a
failed canary or a rejected SMTP login sends it at once."""

MAX_LOGGED_PARSE_WARNINGS: Final = 3
"""Parser warnings logged one per line each poll; the rest are counted in one more line."""


class ExitReason(StrEnum):
    """Why ``run`` returned; both map to exit code 0 so systemd does not restart."""

    EVENT_OVER = "event_over"
    SIGNAL = "signal"


class Pinger(Protocol):
    def ping(self) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class MatchStatus:
    key: str
    """The stored dedup key the match resolved to."""
    match: Match
    pending: frozenset[Channel]
    """Channels owed this match when the poll ran (new, re-armed or upgraded)."""
    sent: frozenset[Channel] = frozenset()
    """Channels that delivered it during this poll (live polls only)."""


@dataclass(frozen=True, slots=True)
class Findings:
    """What an OK/PARTIAL poll found, evaluated against the stored state."""

    inventory: Inventory
    capture: ResaleCapture
    snapshot: PriceSnapshot
    baseline: Baseline
    candidates: tuple[Listing, ...]
    resolved: tuple[Resolved, ...]
    matches: tuple[MatchStatus, ...]

    @property
    def new_matches(self) -> list[MatchStatus]:
        return [status for status in self.matches if status.pending]


@dataclass(frozen=True, slots=True)
class PollReport:
    started_at: datetime
    fetch: FetchResult
    outcome: PollOutcome
    detail: str
    findings: Findings | None
    has_state: bool
    dry_run: bool

    @property
    def succeeded(self) -> bool:
        return self.outcome in SUCCESS


def audit_to(store: Store, clock: Clock) -> AuditHook:
    """A ``Notifier`` audit hook that records every channel attempt in ``store``."""

    def record(kind: NoticeKind, channel: Channel, ok: bool, subject: str) -> None:
        store.log_notification(kind, channel, ok, subject, clock.now())

    return record


class Poller:
    """Runs polls and the service loop. ``store`` is the writer, or for a dry run the
    read-only store (None before the first live poll)."""

    def __init__(
        self,
        settings: Settings,
        fetcher: Fetcher,
        store: Store | None,
        notifier: Notifier,
        pinger: Pinger,
        clock: Clock,
        stop: threading.Event,
        rng: random.Random | None = None,
        watchdog: Watchdog | None = None,
    ) -> None:
        self._settings = settings
        self._fetcher = fetcher
        self._store = store
        self._notifier = notifier
        self._pinger = pinger
        self._clock = clock
        self._stop = stop
        self._rng = rng if rng is not None else random.Random()
        self._watchdog = watchdog if watchdog is not None else NoWatchdog()
        self._rules = settings.rule_config()
        self._ctx = messages.MessageContext.from_settings(settings)
        self._timing = PollTiming.from_settings(settings)
        self._rearm_after = timedelta(hours=settings.realert_after_absent_hours)
        self._outage_after = timedelta(minutes=settings.failure_alert_after_minutes)
        self._next_poll: datetime | None = None

    # --- the service loop (§8) -------------------------------------------------------------

    def run(self) -> ExitReason:
        """Poll until the auto-stop time or a stop request; notices wake the loop on time."""
        self._next_poll = self._first_poll_at(self._clock.now())
        while not self._stop.is_set():
            now = self._clock.now()
            if event_over(now, self._settings.stop_after):
                self._watchdog.busy("finished notice")
                return self._finish()
            if now >= self._next_poll:
                self._watchdog.busy("poll")
                report = self.poll_once()
                if report is None:
                    break
                self._next_poll = self._schedule_after(report)
                now = self._clock.now()
            self._watchdog.busy("notices")
            self._send_due_notices(now)
            wake = self._next_wake(now)
            self._watchdog.idle()
            if self._sleep_until(wake):
                break
        log.info("stop requested; shutting down")
        return ExitReason.SIGNAL

    def _first_poll_at(self, now: datetime) -> datetime:
        """Now, unless a restart lands inside the persisted backoff of a failure streak."""
        state = self._db.get_state()
        last = self._db.recent_polls(1)
        if state.consecutive_failures == 0 or not last or last[0].outcome in SUCCESS:
            return now
        outcome = last[0].outcome
        delay = failure_delay(outcome, self._streak(state, outcome), self._timing)
        resume = last[0].started_at + timedelta(seconds=delay)
        if resume > now:
            log.info("resuming the failure backoff: first poll at %s", resume.isoformat())
        return max(now, resume)

    def _sleep_until(self, wake: datetime) -> bool:
        """Wait until ``wake`` in ≤ ``MAX_WAIT_SECONDS`` slices; True if stop was requested.

        Slices are monotonic, so the wait never outlasts its planned length: after a forward
        wall-clock step it ends early, after a backward step on time, and the next poll is
        moved back by the step (canary hours stay on the wall clock).
        """
        planned = (wake - self._clock.now()).total_seconds()
        waited = 0.0
        while True:
            left = (wake - self._clock.now()).total_seconds()
            step = min(MAX_WAIT_SECONDS, planned - waited, left)
            if self._clock.wait(step):
                return True
            waited += max(0.0, step)
            if waited >= planned or self._clock.now() >= wake:
                break
        behind = wake - self._clock.now()
        if behind > CLOCK_STEP_TOLERANCE and self._next_poll is not None:
            log.warning(
                "wall clock stepped back %s; next poll moved up",
                _seconds_text(behind.total_seconds()),
            )
            self._next_poll -= behind
        return False

    def _schedule_after(self, report: PollReport) -> datetime:
        streak = self._streak(self._db.get_state(), report.outcome)
        delay = poll_delay(report.outcome, streak, self._timing, self._rng)
        level = logging.INFO if report.succeeded else logging.WARNING
        log.log(level, "%s next=%s", poll_summary(report, self._ctx), _seconds_text(delay))
        return self._clock.now() + timedelta(seconds=delay)

    @staticmethod
    def _streak(state: RuntimeState, outcome: PollOutcome) -> int:
        if outcome is PollOutcome.BLOCKED:
            return state.consecutive_blocks
        return state.consecutive_failures

    def _next_wake(self, now: datetime) -> datetime:
        state = self._db.get_state()
        tz, hour = self._settings.tz, self._settings.canary_hour
        return next_wake(
            now,
            self._next_poll,
            next_canary_at(now, tz, hour),
            self._settings.stop_after,
            outage_deadline(state, self._outage_after),
        )

    def _send_due_notices(self, now: datetime) -> None:
        """Outage, recovery and canary; each is retried at later wakes until delivered.

        A bug in one notice is logged and must not stop the others or the loop.
        """
        notices = (
            ("outage", self._announce_outage),
            ("recovery", self._announce_recovery),
            ("canary", self._send_canary),
        )
        for name, send_if_due in notices:
            try:
                send_if_due(now)
            except Exception:
                log.exception("%s notice failed; retrying at a later wake", name)

    def _announce_outage(self, now: datetime) -> None:
        state = self._db.get_state()
        deadline = outage_deadline(state, self._outage_after)
        if deadline is None or now < deadline or state.failure_started_at is None:
            return
        last = self._db.recent_polls(1)[0]
        notice = messages.outage(
            failing_for=now - state.failure_started_at,
            outcome=last.outcome,
            counts=self._db.stats_since(state.failure_started_at).outcomes,
            last_detail=last.detail,
            next_retry_at=self._next_poll,
            ctx=self._ctx,
        )
        delivered = self._notify_admin(notice)
        self._db.save_state(self._db.get_state().record_outage_notice(delivered))

    def _announce_recovery(self, _now: datetime) -> None:
        state = self._db.get_state()
        if not state.recovery_pending:
            return
        duration = timedelta(seconds=state.last_outage_duration_s or 0)
        delivered = self._notify_admin(messages.recovered(outage_duration=duration))
        self._db.save_state(self._db.get_state().record_recovery_notice(delivered))

    def _send_canary(self, now: datetime) -> None:
        tz, hour = self._settings.tz, self._settings.canary_hour
        if not canary_due(now, self._db.get_state().last_canary_date, tz, hour):
            return
        email = messages.canary(canary_report(self._db, self._settings, now), self._ctx)
        delivery = self._send(NoticeKind.CANARY, to=[self._settings.admin_email], email=email)
        today = now.astimezone(self._settings.tz).date()
        self._db.save_state(
            self._db.get_state().record_canary(today, email_ok=delivery.ok(Channel.EMAIL))
        )

    def _finish(self) -> ExitReason:
        """The auto-stop time has passed: send the one-time finished notice and exit."""
        try:
            if not self._db.get_state().finished_notice_sent and self._notify_admin(
                messages.finished(ctx=self._ctx)
            ):
                self._db.save_state(replace(self._db.get_state(), finished_notice_sent=True))
        except Exception:
            log.exception("finished notice failed")
        log.info("auto-stop time %s reached; exiting", self._settings.stop_after.isoformat())
        return ExitReason.EVENT_OVER

    def _notify_admin(self, notice: messages.Notice) -> bool:
        """Admin email + ops push; the notice counts as done once any channel delivers."""
        delivery = self._send(
            notice.kind, to=[self._settings.admin_email], email=notice.email, push=notice.push
        )
        return delivery.any_ok

    def _send(
        self,
        kind: NoticeKind,
        *,
        to: Sequence[str],
        email: messages.Email | None = None,
        push: messages.Push | None = None,
    ) -> Delivery:
        """``Notifier.send``, then track the shared "email failing" episode."""
        delivery = self._notifier.send(kind, to=to, email=email, push=push)
        if delivery.email is not None:
            self._track_email(kind, delivery)
        return delivery

    def _track_email(self, kind: NoticeKind, delivery: Delivery) -> None:
        """One ops push per episode of failing email; a delivered email ends the episode."""
        state = self._db.get_state().record_email(delivery.ok(Channel.EMAIL))
        failing = state.consecutive_email_failures
        if (
            failing
            and not state.email_failing_notice_sent
            and (
                kind is NoticeKind.CANARY
                or delivery.email_auth_failed
                or failing >= EMAIL_FAILURES_BEFORE_PUSH
            )
        ):
            push = messages.email_failing(
                failures=failing, auth_rejected=delivery.email_auth_failed
            )
            pushed = self._notifier.send(NoticeKind.EMAIL_FAILING, to=(), push=push)
            state = replace(state, email_failing_notice_sent=pushed.ok(Channel.PUSH))
        self._db.save_state(state)

    # --- one poll (§8 poll_once) ---------------------------------------------------------------

    def poll_once(self, *, dry_run: bool = False) -> PollReport | None:
        """Fetch and evaluate once; None if stop was requested mid-fetch (nothing is recorded).

        A live poll is committed before anything is sent; an unexpected exception before
        that commit is recorded as an ERROR poll instead. A dry run only reads the store to
        label matches; it writes and sends nothing.
        """
        started = self._clock.now()
        if dry_run:
            result = self._fetcher.fetch()
            return None if result is None else self._evaluate(started, result, dry_run=True)
        try:
            result = self._fetcher.fetch()
            if result is None:
                return None
            report = self._evaluate(started, result, dry_run=False)
            self._record(report)
        except Exception as exc:  # nothing was committed: record the crash as this poll
            report = self._crash_report(started, exc)
            self._record(report)
        if not report.succeeded:
            self._watchdog.busy("browser close")
            self._fetcher.close()  # relaunch the browser on the next poll (§9.1)
            return report
        return self._act_on(report)

    def _crash_report(self, started: datetime, exc: Exception) -> PollReport:
        log.exception("poll crashed")
        detail = redact(f"crash: {type(exc).__name__}: {exc}", self._settings.secret_values())
        elapsed = (self._clock.now() - started).total_seconds()
        crashed = FetchResult(PollOutcome.ERROR, (), detail, None, elapsed)
        return self._report(started, crashed, PollOutcome.ERROR, detail)

    def _evaluate(self, started: datetime, result: FetchResult, *, dry_run: bool) -> PollReport:
        if result.outcome is not PollOutcome.OK:
            return self._report(started, result, result.outcome, result.detail, dry_run=dry_run)
        try:
            inventory = parse_inventory(result.responses, self._rules.wanted_quantities)
        except ParseError as exc:
            detail = f"parse: {exc}"
            try_write_dump(self._settings, detail, result.responses)
            return self._report(started, result, PollOutcome.ERROR, detail, dry_run=dry_run)
        _log_parse_warnings(inventory)
        findings = self._findings(inventory, result.resale_eligible, started)
        degraded = bool(inventory.degraded)
        if degraded or findings.capture is ResaleCapture.MISSING:
            outcome = PollOutcome.PARTIAL
        else:
            outcome = PollOutcome.OK
        detail = result.detail
        if degraded:
            detail = "; ".join(filter(None, (detail, _degraded_detail(inventory))))
        return self._report(started, result, outcome, detail, findings, dry_run=dry_run)

    def _report(
        self,
        started: datetime,
        result: FetchResult,
        outcome: PollOutcome,
        detail: str,
        findings: Findings | None = None,
        *,
        dry_run: bool = False,
    ) -> PollReport:
        has_state = self._store is not None
        return PollReport(started, result, outcome, detail, findings, has_state, dry_run)

    def _findings(self, inventory: Inventory, eligible: bool | None, now: datetime) -> Findings:
        capture = resale_capture(inventory, eligible)
        _log_capture(capture, eligible)
        baseline = (
            current_baseline(self._store, self._settings, now)
            if self._store is not None
            else compute_baseline([], self._settings.baseline_min_samples)
        )
        current = tuple(candidates(inventory, self._rules))
        resolved = tuple(self._resolve(current, now))
        keyed = key_matches(evaluate(inventory, baseline, self._rules), resolved)
        pending = {channel: self._pending_keys(keyed, channel, resolved) for channel in Channel}
        return Findings(
            inventory=inventory,
            capture=capture,
            snapshot=price_snapshot(inventory, self._rules),
            baseline=baseline,
            candidates=current,
            resolved=resolved,
            matches=tuple(
                MatchStatus(key, match, frozenset(ch for ch in Channel if key in pending[ch]))
                for key, match in keyed
            ),
        )

    def _resolve(self, current: Sequence[Listing], now: datetime) -> list[Resolved]:
        if self._store is None:
            return [Resolved(listing.identity, listing.identity, False) for listing in current]
        return self._store.resolve(current, now, self._rearm_after)

    def _pending_keys(
        self, keyed: Sequence[tuple[str, Match]], channel: Channel, resolved: Sequence[Resolved]
    ) -> set[str]:
        if self._store is None:
            return {key for key, _ in keyed}
        return {key for key, _ in self._store.pending(keyed, channel, resolved)}

    def _record(self, report: PollReport) -> None:
        """Commit a live poll: observations, the poll row and the loop state, atomically."""
        store, findings, now = self._db, report.findings, report.started_at
        with store.transaction():
            if findings is not None:
                store.observe(findings.resolved, findings.candidates, now)
            store.record_poll(_poll_record(report, self._rules.wanted_quantities))
            state = store.get_state()
            if report.succeeded:
                state = state.record_success(now)
            else:
                state = state.record_failure(report.outcome, now)
            if findings is not None:
                state = _track_resale(state, findings.capture, report.fetch.resale_eligible)
            store.save_state(state)

    def _act_on(self, report: PollReport) -> PollReport:
        """After a successful poll is committed: deliver, resale notices, then ping.

        A failure here is logged, not recorded as another poll; owed alerts stay pending and
        the missing ping lets healthchecks notice.
        """
        try:
            report = self._deliver(report)
            self._send_resale_notices(report)
        except Exception:
            log.exception("acting on the poll failed; owed alerts stay pending")
            return report
        if not self._stop.is_set():
            self._pinger.ping()
        return report

    def _deliver(self, report: PollReport) -> PollReport:
        """Per channel, email first: one batched message of what that channel still owes (§6.7)."""
        findings = report.findings
        if findings is None:
            return report
        sent: dict[str, set[Channel]] = {status.key: set() for status in findings.matches}
        for channel in Channel:
            owed = [status for status in findings.matches if channel in status.pending]
            if not owed:
                continue
            if self._stop.is_set():
                log.info("alert %s not sent: stopping; it stays pending", channel.value)
                break
            if self._send_alert(channel, owed, findings.baseline):
                keyed = [(status.key, status.match) for status in owed]
                self._db.mark_delivered(keyed, channel, self._clock.now())
                for status in owed:
                    sent[status.key].add(channel)
        matches = tuple(
            replace(status, sent=frozenset(sent[status.key])) for status in findings.matches
        )
        return replace(report, findings=replace(findings, matches=matches))

    def _send_alert(
        self, channel: Channel, owed: Sequence[MatchStatus], baseline: Baseline
    ) -> bool:
        matches = [status.match for status in owed]
        to = self._settings.alert_recipients
        if channel is Channel.PUSH:
            push = messages.alert_push(matches, self._ctx)
            return self._send(NoticeKind.ALERT, to=to, push=push).ok(channel)
        delivered = self._db.delivered_keys([status.key for status in owed], channel)
        upgraded = [status.match.listing.identity for status in owed if status.key in delivered]
        email = messages.alert_email(
            matches, baseline, self._ctx, self._clock.now(), upgraded=upgraded
        )
        return self._send(NoticeKind.ALERT, to=to, email=email).ok(channel)

    def _send_resale_notices(self, report: PollReport) -> None:
        """``resale_eligible`` once ever; ``resale_missing`` once per MISSING episode (§7)."""
        findings = report.findings
        if findings is None:
            return
        state = self._db.get_state()
        if report.fetch.resale_eligible and not state.resale_eligible_notice_sent:
            notice = messages.resale_eligible(capture=findings.capture, ctx=self._ctx)
            if self._notify_admin(notice):
                self._db.save_state(replace(self._db.get_state(), resale_eligible_notice_sent=True))
        state = self._db.get_state()
        streak = state.resale_missing_streak
        if streak >= RESALE_MISSING_POLLS and not state.resale_missing_notice_sent:
            notice = messages.resale_missing(polls=streak, ctx=self._ctx)
            if self._notify_admin(notice):
                self._db.save_state(replace(self._db.get_state(), resale_missing_notice_sent=True))

    @property
    def _db(self) -> Store:
        if self._store is None:
            raise RuntimeError("a live poll needs the state database")
        return self._store


def _log_parse_warnings(inventory: Inventory) -> None:
    """At most ``MAX_LOGGED_PARSE_WARNINGS`` + 1 WARNING lines per poll."""
    degraded = set(inventory.degraded)
    warnings = sorted(inventory.warnings, key=lambda warning: warning not in degraded)
    for warning in warnings[:MAX_LOGGED_PARSE_WARNINGS]:
        label = " (degraded)" if warning in degraded else ""
        log.warning("parse warning%s: %s", label, warning)
    if (more := len(warnings) - MAX_LOGGED_PARSE_WARNINGS) > 0:
        log.warning(
            "%d more parse warnings this poll (%d degraded in total)", more, len(inventory.degraded)
        )


def _degraded_detail(inventory: Inventory) -> str:
    first, more = inventory.degraded[0], len(inventory.degraded) - 1
    return f"parse degraded: {first}" + (f" (+{more} more)" if more else "")


def _log_capture(capture: ResaleCapture, eligible: bool | None) -> None:
    if capture is ResaleCapture.MISSING:
        log.warning("resale eligible but no resale payload captured")
    elif capture is ResaleCapture.UNKNOWN:
        log.warning("resale eligibility flag not found")
    elif capture is ResaleCapture.CAPTURED and eligible is False:
        log.info("resale captured although the page says it is not eligible")


def _track_resale(
    state: RuntimeState, capture: ResaleCapture, eligible: bool | None
) -> RuntimeState:
    """Keep the latest eligibility flag and the MISSING streak; CAPTURED ends an episode."""
    return replace(
        state,
        resale_eligible=state.resale_eligible if eligible is None else eligible,
        resale_missing_streak=(
            state.resale_missing_streak + 1 if capture is ResaleCapture.MISSING else 0
        ),
        resale_missing_notice_sent=(
            state.resale_missing_notice_sent and capture is not ResaleCapture.CAPTURED
        ),
    )


def _poll_record(report: PollReport, wanted: frozenset[int]) -> PollRecord:
    fetch, findings = report.fetch, report.findings
    record = PollRecord(
        started_at=report.started_at,
        outcome=report.outcome,
        detail=report.detail,
        elapsed_s=fetch.elapsed_s,
        browser_recycled=fetch.browser_recycled,
    )
    if findings is None:
        return record
    inventory = findings.inventory
    return replace(
        record,
        resale_capture=findings.capture,
        primary_listings=len(inventory.primary),
        resale_listings=len(inventory.resale),
        snapshot=findings.snapshot,
        matches=len(findings.matches),
        new_matches=len(findings.new_matches),
        primary_labels=_cheapest_labels(inventory.primary, wanted),
        resale_labels=_cheapest_labels(inventory.resale, wanted),
        parse_warnings=len(inventory.warnings),
        parse_degraded=bool(inventory.degraded),
    )


def _cheapest_labels(listings: Iterable[Listing], wanted: Iterable[int]) -> dict[int, str]:
    """Per quantity, ``section row`` of the cheapest listing buyable as that quantity."""
    labels = {}
    for qty in wanted:
        buyable = [listing for listing in listings if listing.buyable_as(qty)]
        if buyable:
            cheapest = min(buyable, key=lambda listing: listing.price_cents)
            labels[qty] = f"{cheapest.section} {cheapest.row}"
    return labels


def poll_summary(report: PollReport, ctx: messages.MessageContext) -> str:
    """The one-line log of a poll (§14), e.g. ``poll ok 3.1s: primary=6 … new=1 top=HIGH``."""
    head = f"poll {report.outcome.value} {report.fetch.elapsed_s:.1f}s"
    findings = report.findings
    if findings is None:
        return f"{head}: {report.detail or 'no detail'}"
    inventory, snapshot = findings.inventory, findings.snapshot
    prices = " ".join(
        f"q{qty}{'~' if qty in inventory.inferred_quantities else ''}="
        f"{'none' if cents is None else messages.money(cents)}"
        for qty, cents in sorted(snapshot.primary_by_qty.items())
    )
    baseline = findings.baseline
    baseline_text = (
        f"{messages.money(baseline.median_cents)}(n={baseline.samples})"
        if baseline.ready and baseline.median_cents is not None
        else f"warming({baseline.samples}/{ctx.rules.baseline_min_samples})"
    )
    new = findings.new_matches
    top = f" top={min(status.match.priority for status in new).name}" if new else ""
    warnings = f" parse_warnings={len(inventory.warnings)}" if inventory.warnings else ""
    return (
        f"{head}: primary={len(inventory.primary)} resale={len(inventory.resale)}"
        f" resale_capture={findings.capture.value} {prices} baseline={baseline_text}"
        f" matches={len(findings.matches)} new={len(new)}{top}{warnings}"
    )


def _seconds_text(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


# --- rendering (poll-once) --------------------------------------------------------------------


def render_report(report: PollReport, settings: Settings) -> str:
    fetch = report.fetch
    head = f"poll {report.outcome.value} in {fetch.elapsed_s:.1f}s"
    if fetch.browser_recycled:
        head += " (browser recycled)"
    lines = [head]
    if report.detail:
        lines.append(f"detail: {report.detail}")
    if (hint := messages.failure_hint(report.outcome, report.detail)) is not None:
        lines.append(f"hint: {hint}")
    if report.findings is not None:
        lines.extend(_findings_lines(report.findings, report, settings))
    return "\n".join(lines)


def _findings_lines(found: Findings, report: PollReport, settings: Settings) -> list[str]:
    ctx = messages.MessageContext.from_settings(settings)
    inventory, eligible = found.inventory, report.fetch.resale_eligible
    lines = [
        f"listings: primary={len(inventory.primary)} resale={len(inventory.resale)}"
        f" · resale_capture={found.capture.value}"
        f" (page isResaleEligible={'unknown' if eligible is None else str(eligible).lower()})",
        *(f"  {_describe(listing)}" for listing in _by_price(inventory)),
        "quickpicks: " + _quantity_sources(settings.wanted_quantities, inventory),
        "cheapest primary: " + _cheapest(found.snapshot.primary_by_qty),
        "cheapest resale: " + _cheapest(found.snapshot.resale_by_qty),
        f"baseline: {messages.baseline_text(found.baseline, ctx)}",
        *(f"warning: {warning}" for warning in inventory.warnings),
        f"candidates: {len(found.candidates)} · matches: {len(found.matches)}",
    ]
    if not report.has_state:
        lines.append("(no state yet: every match counts as new)")
    for number, item in enumerate(found.matches, start=1):
        lines.extend(_match_lines(number, item, found.baseline, ctx, dry_run=report.dry_run))
    return lines


def _quantity_sources(wanted: frozenset[int], inventory: Inventory) -> str:
    def source(qty: int) -> str:
        inferred = qty in inventory.inferred_quantities
        return f"qty {qty} {'inferred from maxQuantity' if inferred else 'captured'}"

    return ", ".join(source(qty) for qty in sorted(wanted))


def _cheapest(by_qty: Mapping[int, int | None]) -> str:
    return " · ".join(
        f"q{qty}={'none' if cents is None else messages.money(cents)}"
        for qty, cents in sorted(by_qty.items())
    )


def _by_price(inventory: Inventory) -> list[Listing]:
    return sorted((*inventory.primary, *inventory.resale), key=lambda item: item.price_cents)


def _describe(listing: Listing) -> str:
    """``PRIMARY Sec 123 Row M Seats 5-7 · $383.50 (≈$452.55 all-in) · qty 2,3``."""
    kind = "RESALE" if listing.inventory_type is InventoryType.RESALE else "PRIMARY"
    seats = f" Seats {listing.seats}" if listing.seats else ""
    quantities = ",".join(str(q) for q in sorted(listing.buyable_quantities)) or "none"
    return (
        f"{kind} Sec {listing.section} Row {listing.row}{seats}"
        f" · {messages.money(listing.price_cents)} (≈{messages.money(listing.all_in_cents)} all-in)"
        f" · qty {quantities}"
    )


def _channels(channels: Iterable[Channel]) -> str:
    return ", ".join(sorted(channel.value for channel in channels))


def _delivery_text(item: MatchStatus, *, dry_run: bool) -> str:
    if not item.pending:
        return "already sent"
    if dry_run:
        return f"would alert: {_channels(item.pending)}"
    parts = []
    if item.sent:
        parts.append(f"sent: {_channels(item.sent)}")
    if unsent := item.pending - item.sent:
        parts.append(f"not sent (retried next poll): {_channels(unsent)}")
    return " · ".join(parts)


def _match_lines(
    number: int,
    item: MatchStatus,
    baseline: Baseline,
    ctx: messages.MessageContext,
    *,
    dry_run: bool,
) -> list[str]:
    label = messages.priority_label(item.match, ctx.rules)
    return [
        f"{number}. [{label}] {_describe(item.match.listing)}",
        f"   {messages.why_line(item.match, baseline, ctx)}"
        f" · {_delivery_text(item, dry_run=dry_run)}",
    ]
