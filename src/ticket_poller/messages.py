"""Email and push content for alerts, the daily canary and admin notices (design §11).

Everything here is pure: builders take domain values and return ``Email``/``Push``
objects. No secret is ever an input, so none can appear in a message.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Final
from zoneinfo import ZoneInfo

from ticket_poller import __version__
from ticket_poller.config import RuleConfig, Settings
from ticket_poller.models import (
    Baseline,
    Channel,
    InventoryType,
    Listing,
    Match,
    NoticeKind,
    PollOutcome,
    Priority,
    Reason,
    ResaleCapture,
)
from ticket_poller.rules import match_sort_key

NTFY_PRIORITY: Final[Mapping[Priority, int]] = {
    Priority.TOP: 5,
    Priority.HIGH: 4,
    Priority.NORMAL: 3,
}
MAX_PUSH_LINES: Final = 5
EN_DASH: Final = "\u2013"
TIMES: Final = "\u00d7"
REASON_ORDER: Final = (
    Reason.PREFERRED_DEAL,
    Reason.UNDER_PRICE_CAP,
    Reason.BELOW_BASELINE,
    Reason.RESALE_LISTED,
)


@dataclass(frozen=True, slots=True)
class Email:
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class Push:
    title: str
    message: str
    priority: int
    tags: tuple[str, ...] = ()
    click: str | None = None


@dataclass(frozen=True, slots=True)
class Notice:
    """An admin notice: the admin email plus, unless email-only, an ops push."""

    kind: NoticeKind
    email: Email
    push: Push | None


@dataclass(frozen=True, slots=True)
class MessageContext:
    """The non-secret settings that message content depends on."""

    event_name: str
    event_date: date
    event_url: str
    tz: ZoneInfo
    stop_after: datetime
    rules: RuleConfig
    baseline_window_days: int
    failure_alert_after: timedelta

    @classmethod
    def from_settings(cls, settings: Settings) -> "MessageContext":
        return cls(
            event_name=settings.event_name,
            event_date=settings.event_date,
            event_url=settings.event_url,
            tz=settings.tz,
            stop_after=settings.stop_after,
            rules=settings.rule_config(),
            baseline_window_days=settings.baseline_window_days,
            failure_alert_after=timedelta(minutes=settings.failure_alert_after_minutes),
        )

    @property
    def short_name(self) -> str:
        """The artist part of ``event_name`` (before the first " — "), for subjects."""
        return self.event_name.split(" — ", 1)[0].strip()


@dataclass(frozen=True, slots=True)
class PricePoint:
    cents: int
    where: str | None = None
    """Where the cheapest listing is, e.g. ``123 M`` (section and row), if known."""


@dataclass(frozen=True, slots=True)
class CanaryReport:
    """Everything the daily canary shows; ``status.canary_report`` gathers it from the store.

    Counts and lows cover the last 24 h; ``*_now`` values come from the latest
    successful poll. Price mappings are keyed by quantity.
    """

    now: datetime
    outcomes: Mapping[PollOutcome, int] = field(default_factory=dict)
    last_success_at: datetime | None = None
    failing_since: datetime | None = None
    consecutive_failures: int = 0
    primary_now: Mapping[int, PricePoint | None] = field(default_factory=dict)
    resale_now: Mapping[int, PricePoint | None] = field(default_factory=dict)
    primary_low: Mapping[int, int | None] = field(default_factory=dict)
    resale_low: Mapping[int, int | None] = field(default_factory=dict)
    baseline: Baseline | None = None
    resale_eligible: bool | None = None
    resale_captured_polls: int = 0
    resale_listings_now: int = 0
    alerts: int = 0
    great_deals: int = 0
    channel_failures: Mapping[Channel, int] = field(default_factory=dict)
    browser_recycles: int = 0
    resale_missing_polls: int = 0
    parse_warning_polls: int = 0
    parse_degraded_polls: int = 0

    @property
    def total_polls(self) -> int:
        return sum(self.outcomes.values())

    @property
    def successful_polls(self) -> int:
        return sum(n for outcome, n in self.outcomes.items() if outcome.succeeded)


# --- formatting helpers -----------------------------------------------------------------------


def money(cents: int | Decimal) -> str:
    """``$1,234.50``; a half-cent ``Decimal`` (a median) is rounded half-up."""
    exact = Decimal(cents).quantize(Decimal(1), ROUND_HALF_UP)
    return f"${exact / 100:,.2f}"


def round_money(cents: int) -> str:
    """Like ``money`` but drops ``.00`` from whole-dollar amounts: ``$300``, ``$343.50``."""
    return f"${cents // 100:,}" if cents % 100 == 0 else money(cents)


def format_duration(duration: timedelta) -> str:
    """``3h12m``, ``1h00m`` or ``45m`` (whole minutes, rounded down)."""
    minutes = max(0, int(duration.total_seconds()) // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def local_time(moment: datetime, tz: tzinfo) -> str:
    """``2026-11-14 09:12 CST``."""
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def _quantities(quantities: Iterable[int], *, conjunction: str = "or") -> str:
    """``3``, ``2 or 3``, ``2, 4 or 6``."""
    items = [str(q) for q in sorted(quantities)]
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


def _compact_quantities(quantities: Iterable[int]) -> str:
    """``2-3`` for a contiguous range, else ``2,4,6``."""
    items = sorted(quantities)
    if len(items) > 1 and items == list(range(items[0], items[-1] + 1)):
        return f"{items[0]}-{items[-1]}"
    return ",".join(str(q) for q in items)


def _range_label(quantities: Iterable[int]) -> str:
    """``2-3`` with an en dash for a contiguous range, else ``2/4``."""
    return _compact_quantities(quantities).replace("-", EN_DASH).replace(",", "/")


def sort_matches(matches: Iterable[Match]) -> list[Match]:
    return sorted(matches, key=match_sort_key)


# --- alert pieces -----------------------------------------------------------------------------


def _preferred_qty(listing: Listing, rules: RuleConfig) -> frozenset[int]:
    return listing.buyable_quantities & rules.preferred_quantities


def _alert_qty(listing: Listing, rules: RuleConfig) -> frozenset[int]:
    """The wanted quantities the listing can be bought as (all of them if none is wanted)."""
    return listing.buyable_quantities & rules.wanted_quantities or listing.buyable_quantities


def priority_label(match: Match, rules: RuleConfig) -> str:
    """``GREAT DEAL — 3 tickets ≤ $300`` · ``3 tickets`` · ``2 only`` · ``2 only · ≤ $300``."""
    listing = match.listing
    preferred = _quantities(_preferred_qty(listing, rules))
    match match.priority:
        case Priority.TOP:
            return (
                f"GREAT DEAL — {preferred} tickets ≤ {round_money(rules.preferred_max_price_cents)}"
            )
        case Priority.HIGH:
            return f"{preferred} tickets"
        case Priority.NORMAL:
            label = f"{_quantities(_alert_qty(listing, rules))} only"
            if match.within_preferred_price:
                label += f" · ≤ {round_money(rules.preferred_max_price_cents)}"
            return label


def _reason_text(reason: Reason, match: Match, baseline: Baseline, ctx: MessageContext) -> str:
    rules = ctx.rules
    match reason:
        case Reason.PREFERRED_DEAL:
            together = _quantities(_preferred_qty(match.listing, rules))
            price = round_money(rules.preferred_max_price_cents)
            return f"{together} together at ≤ {price} (preferred price)"
        case Reason.UNDER_PRICE_CAP:
            return f"≤ {round_money(rules.primary_alert_max_price_cents)} per ticket"
        case Reason.BELOW_BASELINE:
            percent = (match.pct_below_baseline or Decimal(0)) * 100
            median = money(baseline.median_cents) if baseline.median_cents is not None else "n/a"
            whole = percent.to_integral_value(ROUND_DOWN)
            return f"{whole}% below {ctx.baseline_window_days}-day median ({median})"
        case Reason.RESALE_LISTED:
            return "new resale listing"


def why_line(
    match: Match, baseline: Baseline, ctx: MessageContext, *, upgraded: bool = False
) -> str:
    """``Why: …``: the triggering reasons, joined with ``; ``."""
    reasons = [_reason_text(r, match, baseline, ctx) for r in REASON_ORDER if r in match.reasons]
    line = "Why: " + "; ".join(reasons)
    if upgraded:
        line += f" (now buyable as {_quantities(_preferred_qty(match.listing, ctx.rules))})"
    return line


def _where(listing: Listing) -> str:
    parts = [f"Sec {listing.section}", f"Row {listing.row}"]
    if listing.seats:
        parts.append(f"Seats {listing.seats}")
    text = " · ".join(parts)
    biggest = max(listing.buyable_quantities, default=0)
    if listing.max_together is not None and listing.max_together > biggest:
        text += f" (up to {listing.max_together} together)"
    return text


def _kind(listing: Listing) -> str:
    if listing.inventory_type is InventoryType.RESALE:
        return "RESALE (Face Value Exchange)"
    return "PRIMARY"


def _alert_item(
    number: int, match: Match, baseline: Baseline, ctx: MessageContext, *, upgraded: bool
) -> str:
    listing = match.listing
    return "\n".join(
        (
            f"{number}. [{priority_label(match, ctx.rules)}] {_kind(listing)} — {_where(listing)}",
            f"   {money(listing.price_cents)}/ticket excl. fees "
            f"(≈{money(listing.all_in_cents)} with fees, before tax) "
            f"· can buy: {_quantities(listing.buyable_quantities)}",
            f"   {why_line(match, baseline, ctx, upgraded=upgraded)}",
        )
    )


def baseline_text(baseline: Baseline | None, ctx: MessageContext) -> str:
    """``$383.50 (412 samples)`` or ``warming up (5/12 samples)``."""
    minimum = ctx.rules.baseline_min_samples
    if baseline is None:
        return f"warming up (0/{minimum} samples)"
    if not baseline.ready or baseline.median_cents is None:
        return f"warming up ({baseline.samples:,}/{minimum} samples)"
    return f"{money(baseline.median_cents)} ({baseline.samples:,} samples)"


def _subject_qty(listing: Listing, rules: RuleConfig) -> frozenset[int]:
    return _preferred_qty(listing, rules) or _alert_qty(listing, rules)


def _more(count: int) -> str:
    return f" (+{count} more)" if count else ""


def alert_subject(matches: Sequence[Match], ctx: MessageContext) -> str:
    """The subject follows the best match, with `` (+N more)`` for the rest."""
    top = sort_matches(matches)[0]
    listing, name = top.listing, ctx.short_name
    qty = _quantities(_subject_qty(listing, ctx.rules))
    price = f"{money(listing.price_cents)}/ticket"
    match top.priority:
        case Priority.TOP:
            subject = f"🎉 GREAT DEAL: {qty} {name} tickets at {price}"
        case Priority.HIGH:
            subject = f"{name}: {qty} tickets available — {price}"
        case Priority.NORMAL:
            what = "pair" if qty == "2" else f"{qty} tickets"
            subject = f"{name}: {what} available ({qty} only) — {price}"
    return subject + _more(len(matches) - 1)


def alert_email(
    matches: Sequence[Match],
    baseline: Baseline,
    ctx: MessageContext,
    now: datetime,
    *,
    upgraded: Iterable[str] = (),
) -> Email:
    """One batched alert email; ``upgraded`` holds identities of upgrade re-alerts."""
    if not matches:
        raise ValueError("an alert needs at least one match")
    upgrades = frozenset(upgraded)
    ordered = sort_matches(matches)
    items = [
        _alert_item(n, m, baseline, ctx, upgraded=m.listing.identity in upgrades)
        for n, m in enumerate(ordered, start=1)
    ]
    rules = ctx.rules
    preferred = sorted(rules.preferred_quantities)
    others = sorted(rules.wanted_quantities - rules.preferred_quantities)
    if preferred and others:
        choose = f"Choose quantity {_quantities(preferred)} (or {_quantities(others)})"
    else:
        choose = f"Choose quantity {_quantities(rules.wanted_quantities)}"
    wanted = _range_label(rules.wanted_quantities)
    body = "\n".join(
        (
            f"New ticket matches: {ctx.event_name} ({ctx.event_date:%a %b %d, %Y})",
            f"Open: {ctx.event_url}",
            "",
            "\n".join(items),
            "",
            f"Baseline: {ctx.baseline_window_days}-day median of cheapest {wanted} seat primary "
            f"= {baseline_text(baseline, ctx)}",
            f"Seen {local_time(now, ctx.tz)}. Inventory moves fast — these may already be gone.",
            f"{choose} on the page; TM may assign different seats.",
        )
    )
    return Email(alert_subject(ordered, ctx), body + "\n")


def _push_item(match: Match) -> str:
    listing = match.listing
    star = "★ " if match.priority is Priority.TOP else ""
    resale = "RESALE " if listing.inventory_type is InventoryType.RESALE else ""
    price = round_money(listing.price_cents)
    qty = _compact_quantities(listing.buyable_quantities)
    return f"{star}{resale}{listing.section}/{listing.row} {price} {TIMES}{qty}"


def alert_push(matches: Sequence[Match], ctx: MessageContext) -> Push:
    """A short ntfy alert: title from the best match, one line per match (≤ 5 lines)."""
    if not matches:
        raise ValueError("an alert needs at least one match")
    ordered = sort_matches(matches)
    top = ordered[0]
    listing = top.listing
    qty = _quantities(_subject_qty(listing, ctx.rules))
    price = round_money(listing.price_cents)
    match top.priority:
        case Priority.TOP:
            title = f"🎉 GREAT DEAL: {qty} tix at {price}"
        case Priority.HIGH:
            title = f"{ctx.short_name}: {qty} tix at {price}"
        case Priority.NORMAL:
            what = "pair" if qty == "2" else f"{qty} tix"
            title = f"{ctx.short_name}: {what} at {price} ({qty} only)"
    lines = [_push_item(m) for m in ordered]
    if len(lines) > MAX_PUSH_LINES:
        lines = [*lines[: MAX_PUSH_LINES - 1], f"…and {len(lines) - MAX_PUSH_LINES + 1} more"]
    return Push(
        title=title + _more(len(ordered) - 1),
        message="\n".join(lines),
        priority=NTFY_PRIORITY[top.priority],
        tags=("rotating_light", "tada") if top.priority is Priority.TOP else ("tickets",),
        click=ctx.event_url,
    )


# --- canary -----------------------------------------------------------------------------------


def _degraded_reasons(report: CanaryReport, ctx: MessageContext) -> list[str]:
    reasons = []
    if report.failing_since is not None:
        failing_for = report.now - report.failing_since
        if failing_for >= ctx.failure_alert_after:
            reasons.append(f"failing for {format_duration(failing_for)}")
    if report.resale_missing_polls:
        reasons.append("resale not captured")
    if report.parse_degraded_polls:  # warnings alone stay on the informational Parser: line
        reasons.append(_parse_text(report))
    return reasons


def _parse_text(report: CanaryReport) -> str:
    """``parse warnings in 3 polls (1 degraded)``."""
    text = f"parse warnings in {report.parse_warning_polls:,} polls"
    if report.parse_degraded_polls:
        text += f" ({report.parse_degraded_polls:,} degraded)"
    return text


def _price_line(
    label: str,
    now: Mapping[int, PricePoint | None],
    low: Mapping[int, int | None],
    quantities: Iterable[int],
) -> str:
    parts = []
    for qty in sorted(quantities, reverse=True):
        point = now.get(qty)
        text = f"qty {qty}: "
        if point is None:
            text += "none"
        else:
            text += money(point.cents) + (f" ({point.where})" if point.where else "")
        if (cents := low.get(qty)) is not None:
            text += f", 24 h low {money(cents)}"
        parts.append(text)
    return f"{label}  " + " | ".join(parts)


def _resale_line(report: CanaryReport) -> str:
    eligible = {True: "yes", False: "no", None: "unknown"}[report.resale_eligible]
    if report.resale_eligible is not True and not report.resale_captured_polls:
        return f"Resale: eligible {eligible} · captured n/a"
    return (
        f"Resale: eligible {eligible} · captured {report.resale_captured_polls:,}/"
        f"{report.successful_polls:,} polls · listings now {report.resale_listings_now:,}"
    )


def _time_or_date(moment: datetime, now: datetime, tz: ZoneInfo) -> str:
    local, today = moment.astimezone(tz), now.astimezone(tz)
    return f"{local:%H:%M}" if local.date() == today.date() else f"{local:%Y-%m-%d %H:%M}"


def canary(report: CanaryReport, ctx: MessageContext) -> Email:
    """The daily canary (admin email only)."""
    degraded = _degraded_reasons(report, ctx)
    status = "DEGRADED: " + "; ".join(degraded) if degraded else "OK"
    counts = " · ".join(f"{o.value} {report.outcomes.get(o, 0):,}" for o in PollOutcome)
    last = (
        _time_or_date(report.last_success_at, report.now, ctx.tz)
        if report.last_success_at is not None
        else "never"
    )
    streak = "none"
    if report.consecutive_failures:
        streak = f"{report.consecutive_failures} failures"
        if report.failing_since is not None:
            streak += f" since {_time_or_date(report.failing_since, report.now, ctx.tz)}"
    wanted = ctx.rules.wanted_quantities
    failures = ", ".join(f"{c.value} {report.channel_failures.get(c, 0)}" for c in Channel)
    lines: tuple[str, ...] = (
        f"Polls (24 h): {report.total_polls:,} · {counts} · last success {last} · streak: {streak}",
        _price_line("Cheapest primary", report.primary_now, report.primary_low, wanted),
        _price_line("Cheapest resale ", report.resale_now, report.resale_low, wanted),
        f"Baseline ({ctx.baseline_window_days}-day median, cheapest {_range_label(wanted)} "
        f"primary): {baseline_text(report.baseline, ctx)}",
        _resale_line(report),
        f"Alerts (24 h): {report.alerts:,} ({report.great_deals:,} GREAT DEAL) "
        f"· channel failures: {failures}",
        f"Browser recycles: {report.browser_recycles:,} · Version {__version__} "
        f"· Auto-stop {local_time(ctx.stop_after, ctx.tz)}",
    )
    if report.parse_warning_polls:
        lines += (f"Parser: {_parse_text(report)} — see journalctl -p warning",)
    return Email(f"ticket-poller daily canary — {status}", "\n".join(lines) + "\n")


# --- admin notices ----------------------------------------------------------------------------


def failure_hint(outcome: PollOutcome, detail: str) -> str | None:
    """What Philip can do about a failure, if anything specific."""
    if detail.startswith("profile_in_use"):
        return (
            "Close the manual Chromium that is using the poller's profile; "
            "the poller retries with backoff."
        )
    if detail.startswith("no_display"):
        return "No display: check that the graphical session is running (see README)."
    if outcome is PollOutcome.BLOCKED:
        return (
            "Ticketmaster is blocking the poller. Stop the service and open the event page "
            "in the poller's Chromium profile manually (README: manual use of the profile)."
        )
    if "missing quickpicks" in detail:
        return "The page layout may have changed (no quickpicks); check STATE_DIR/debug."
    return None


def _notice(
    kind: NoticeKind,
    subject: str,
    body: Sequence[str],
    priority: int,
    tags: tuple[str, ...],
    *,
    push_lines: Sequence[str] = (),
    click: str | None = None,
) -> Notice:
    """Email body ``body``; the push shows ``push_lines`` (default: the first body line)."""
    push = Push(subject, "\n".join(push_lines or body[:1]), priority, tags, click)
    return Notice(kind, Email(subject, "\n".join(body) + "\n"), push)


def outage(
    *,
    failing_for: timedelta,
    outcome: PollOutcome,
    counts: Mapping[PollOutcome, int],
    last_detail: str,
    next_retry_at: datetime | None,
    ctx: MessageContext,
) -> Notice:
    """Polling has been failing for ``FAILURE_ALERT_AFTER_MINUTES`` (one notice per outage)."""
    duration = format_duration(failing_for)
    tally = ", ".join(f"{o.value} {n}" for o in PollOutcome if (n := counts.get(o, 0)))
    retry = local_time(next_retry_at, ctx.tz) if next_retry_at is not None else "unknown"
    body = [
        f"Polling has been failing for {duration} ({outcome.value}).",
        f"Last detail: {last_detail or '(none)'}",
        f"Failed polls this outage: {tally or 'none recorded'}",
        f"Next retry: {retry}",
    ]
    hint = failure_hint(outcome, last_detail)
    if hint is not None:
        body.append(f"Hint: {hint}")
    body.append("No reminders follow; the daily canary says DEGRADED until polling recovers.")
    return _notice(
        NoticeKind.OUTAGE,
        f"ticket-poller: polling failing for {duration} ({outcome.value})",
        body,
        4,
        ("warning",),
        push_lines=[body[0], *([hint] if hint else [])],
    )


def recovered(*, outage_duration: timedelta) -> Notice:
    duration = format_duration(outage_duration)
    return _notice(
        NoticeKind.RECOVERED,
        f"ticket-poller: recovered after {duration}",
        [f"Polling works again after failing for {duration}."],
        3,
        ("white_check_mark",),
    )


def resale_eligible(*, capture: ResaleCapture, ctx: MessageContext) -> Notice:
    status = capture.value.upper()
    body = [
        f"The event page now says resale (Face Value Exchange) is eligible. Capture: {status}.",
        "Resale listings buyable as "
        f"{_quantities(ctx.rules.wanted_quantities)} will alert from now on.",
    ]
    if capture is not ResaleCapture.CAPTURED:
        body.append(
            "Resale data was not captured on this poll; a resale_missing notice follows "
            "if that persists."
        )
    return _notice(
        NoticeKind.RESALE_ELIGIBLE,
        f"ticket-poller: FVE resale now eligible — capture: {status}",
        body,
        4,
        ("information_source",),
        click=ctx.event_url,
    )


def resale_missing(*, polls: int, ctx: MessageContext) -> Notice:
    body = [
        f"Resale is eligible but no resale data was captured for {polls} polls in a row.",
        "Primary alerts still work; resale (Face Value Exchange) listings may be missed.",
        "Limitation: if the page fetches resale only inside an unfiltered offer facet and "
        "there are no listings, a correct page also looks like this. Check whether any FVE "
        "listings show on the page.",
        "To capture evidence:",
        "  1. systemctl --user stop ticket-poller",
        "  2. uv run ticket-poller poll-once --save ./resale-dump",
        "  3. systemctl --user start ticket-poller",
        "  4. Send the dump so a UI step that enables resale can be added.",
    ]
    return _notice(
        NoticeKind.RESALE_MISSING,
        f"ticket-poller: resale eligible but not captured for {polls} polls",
        body,
        4,
        ("warning",),
        click=ctx.event_url,
    )


def finished(*, ctx: MessageContext) -> Notice:
    return _notice(
        NoticeKind.FINISHED,
        "ticket-poller stopped: event date passed",
        [
            f"Auto-stop time {local_time(ctx.stop_after, ctx.tz)} has passed; the poller exited.",
            "Disable the service: systemctl --user disable --now ticket-poller",
        ],
        3,
        ("checkered_flag",),
    )


def email_failing(*, failures: int, auth_rejected: bool) -> Push:
    """Ops push, once per episode: admin emails (and the canary) are not getting through."""
    cause = (
        "The SMTP login was rejected: check SENDER_EMAIL and SENDER_APP_PASSWORD."
        if auth_rejected
        else f"{failures} email send(s) in a row failed; see journalctl --user -u ticket-poller."
    )
    message = (
        f"{cause} Alert pushes still work; the daily canary and admin emails do not. "
        "You get this once until an email is delivered again."
    )
    return Push("ticket-poller: email delivery failing", message, 4, ("warning",))


def service_failed(*, unit: str | None = None, config_problem: str | None = None) -> Push:
    """Ops push from ``notify-failure`` (systemd ``OnFailure=``); needs no state database.

    ``config_problem`` is a short, secret-free reason the service refuses to start.
    """
    title = "Ticket poller service FAILED — check journalctl --user -u ticket-poller"
    lines = [f"systemd unit {unit} entered the failed state." if unit else title]
    if config_problem:
        lines.append(f"Config problem: {config_problem}.")
    return Push(title, "\n".join(lines), 5, ("rotating_light",))


def sample_matches(rules: RuleConfig) -> list[Match]:
    """Two made-up matches (a best-tier primary and a pair-only resale) for test messages."""
    has_preferred = bool(rules.preferred_quantities)
    cap = rules.preferred_max_price_cents if has_preferred else rules.primary_alert_max_price_cents
    great = Listing(
        inventory_type=InventoryType.PRIMARY,
        offer_id="SAMPLE",
        listing_id=None,
        section="123",
        row="M",
        places=frozenset({"SAMPLE1", "SAMPLE2", "SAMPLE3"}),
        seats=f"5{EN_DASH}7",
        price_cents=min(28900, cap),
        fees_cents=5185,
        buyable_quantities=rules.wanted_quantities,
        max_together=4,
    )
    pair = Listing(
        inventory_type=InventoryType.RESALE,
        offer_id="SAMPLE-RESALE",
        listing_id="SAMPLE-LISTING",
        section="104",
        row="R",
        places=frozenset(),
        seats=f"9{EN_DASH}10",
        price_cents=34350,
        fees_cents=6185,
        buyable_quantities=frozenset({2}),
        max_together=None,
    )
    return [
        Match(
            listing=great,
            reasons=frozenset({Reason.PREFERRED_DEAL if has_preferred else Reason.UNDER_PRICE_CAP}),
            priority=Priority.TOP if has_preferred else Priority.NORMAL,
            within_preferred_price=True,
            pct_below_baseline=None,
        ),
        Match(
            listing=pair,
            reasons=frozenset({Reason.RESALE_LISTED}),
            priority=Priority.NORMAL,
            within_preferred_price=False,
            pct_below_baseline=None,
        ),
    ]


def selftest_notice(*, settings_summary: str, ctx: MessageContext, now: datetime) -> Notice:
    """``test-notify``: the (secret-free) settings summary plus a rendered sample alert."""
    sample_baseline = Baseline(median_cents=Decimal(38350), samples=412, ready=True)
    sample = alert_email(sample_matches(ctx.rules), sample_baseline, ctx, now)
    body = (
        "This is a test notification from ticket-poller; email delivery works.",
        "",
        "Settings (secrets show as set/unset):",
        settings_summary,
        "",
        "--- sample alert (made-up tickets) ---",
        f"Subject: {sample.subject}",
        "",
        sample.body.rstrip("\n"),
    )
    return Notice(
        NoticeKind.TEST,
        Email("ticket-poller test notification", "\n".join(body) + "\n"),
        Push(
            "ticket-poller test notification",
            "Push delivery works. Sample alert title: "
            + alert_push(sample_matches(ctx.rules), ctx).title,
            3,
            ("test_tube",),
        ),
    )
