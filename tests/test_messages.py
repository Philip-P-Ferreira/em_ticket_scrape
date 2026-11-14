"""Message templates (§11): alert email/push per tier, canary, admin notices, test-notify."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from tests.sample_env import (
    APP_PASSWORD,
    HC_UUID,
    NTFY_OPS_TOPIC,
    NTFY_TOKEN,
    NTFY_TOPIC,
    EnvFileFactory,
)
from ticket_poller.config import RuleConfig, load_settings
from ticket_poller.messages import (
    EN_DASH,
    TIMES,
    CanaryReport,
    MessageContext,
    NoticeKind,
    PricePoint,
    alert_email,
    alert_push,
    alert_subject,
    canary,
    format_duration,
    money,
    outage,
    priority_label,
    recovered,
    resale_eligible,
    resale_missing,
    round_money,
    sample_matches,
    selftest_notice,
    sort_matches,
    why_line,
)
from ticket_poller.models import (
    Baseline,
    Channel,
    InventoryType,
    Listing,
    Match,
    PollOutcome,
    Priority,
    Reason,
    ResaleCapture,
)

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 6, 1, 15, 4, tzinfo=UTC)  # 10:04 CDT
URL = "https://www.ticketmaster.com/event/2000655CAB442FB5"
RULES = RuleConfig(
    wanted_quantities=frozenset({2, 3}),
    preferred_quantities=frozenset({3}),
    primary_alert_max_price_cents=20000,
    primary_drop_percent=20,
    preferred_max_price_cents=30000,
    baseline_min_samples=12,
)
CTX = MessageContext(
    event_name="Noah Kahan — Protective Stadium, Birmingham AL",
    event_date=date(2027, 4, 27),
    event_url=URL,
    tz=TZ,
    stop_after=datetime(2027, 4, 28, tzinfo=TZ),
    rules=RULES,
)
BASELINE = Baseline(median_cents=Decimal(43500), samples=412, ready=True)


def listing(
    price: int,
    *,
    qty: frozenset[int] = frozenset({2, 3}),
    section: str = "123",
    row: str = "M",
    resale: bool = False,
    max_together: int | None = None,
) -> Listing:
    return Listing(
        inventory_type=InventoryType.RESALE if resale else InventoryType.PRIMARY,
        offer_id=f"offer-{section}-{row}-{price}",
        listing_id=f"listing-{price}" if resale else None,
        section=section,
        row=row,
        places=frozenset() if resale else frozenset({f"P{price}"}),
        seats=f"5{EN_DASH}7",
        price_cents=price,
        fees_cents=price // 6,
        buyable_quantities=qty,
        max_together=max_together,
    )


def match(
    item: Listing,
    priority: Priority,
    *reasons: Reason,
    preferred_price: bool = False,
    pct: str | None = None,
) -> Match:
    return Match(
        listing=item,
        reasons=frozenset(reasons),
        priority=priority,
        within_preferred_price=preferred_price,
        pct_below_baseline=Decimal(pct) if pct is not None else None,
    )


TOP = match(listing(28900), Priority.TOP, Reason.PREFERRED_DEAL, preferred_price=True)
HIGH = match(listing(34000, section="210"), Priority.HIGH, Reason.BELOW_BASELINE, pct="0.2184")
PAIR = match(listing(19500, qty=frozenset({2})), Priority.NORMAL, Reason.UNDER_PRICE_CAP)


class TestFormatting:
    def test_money(self) -> None:
        assert money(123450) == "$1,234.50"
        assert money(Decimal("38350.5")) == "$383.51"  # half-cent medians round half-up
        assert round_money(30000) == "$300"
        assert round_money(34350) == "$343.50"

    def test_format_duration(self) -> None:
        assert format_duration(timedelta(minutes=45)) == "45m"
        assert format_duration(timedelta(hours=1)) == "1h00m"
        assert format_duration(timedelta(hours=3, minutes=12, seconds=59)) == "3h12m"

    def test_short_name_is_the_artist(self) -> None:
        assert CTX.short_name == "Noah Kahan"


class TestAlertEmail:
    @pytest.mark.parametrize(
        ("matches", "expected"),
        [
            ([TOP], "🎉 GREAT DEAL: 3 Noah Kahan tickets at $289.00/ticket"),
            (
                [match(listing(34000), Priority.HIGH, Reason.BELOW_BASELINE, pct="0.22")],
                "Noah Kahan: 3 tickets available — $340.00/ticket",
            ),
            ([PAIR], "Noah Kahan: pair available (2 only) — $195.00/ticket"),
            ([PAIR, HIGH, TOP], "🎉 GREAT DEAL: 3 Noah Kahan tickets at $289.00/ticket (+2 more)"),
        ],
    )
    def test_subject_per_tier(self, matches: list[Match], expected: str) -> None:
        assert alert_subject(matches, CTX) == expected

    def test_priority_labels(self) -> None:
        cheap_pair = match(
            listing(25000, qty=frozenset({2})),
            Priority.NORMAL,
            Reason.BELOW_BASELINE,
            preferred_price=True,
            pct="0.3",
        )

        assert priority_label(TOP, RULES) == "GREAT DEAL — 3 tickets ≤ $300"
        assert priority_label(HIGH, RULES) == "3 tickets"
        assert priority_label(PAIR, RULES) == "2 only"
        assert priority_label(cheap_pair, RULES) == "2 only · ≤ $300"

    def test_preferred_deal_why_line(self) -> None:
        assert why_line(TOP, BASELINE, CTX) == "Why: 3 together at ≤ $300 (preferred price)"

    def test_why_line_joins_reasons_in_order_and_rounds_percent_down(self) -> None:
        both = match(
            listing(19000),
            Priority.HIGH,
            Reason.BELOW_BASELINE,
            Reason.UNDER_PRICE_CAP,
            pct="0.5632",
        )

        assert why_line(HIGH, BASELINE, CTX) == "Why: 21% below 7-day median ($435.00)"
        assert why_line(both, BASELINE, CTX) == (
            "Why: ≤ $200 per ticket; 56% below 7-day median ($435.00)"
        )

    def test_upgrade_suffix(self) -> None:
        line = why_line(TOP, BASELINE, CTX, upgraded=True)

        assert line.endswith("(now buyable as 3)")

    def test_matches_are_sorted_by_priority_then_preferred_price_then_price(self) -> None:
        cheap_high = match(listing(20000, section="300"), Priority.HIGH, Reason.UNDER_PRICE_CAP)
        preferred_high = match(
            listing(29000, section="301"),
            Priority.HIGH,
            Reason.UNDER_PRICE_CAP,
            preferred_price=True,
        )

        ordered = sort_matches([PAIR, cheap_high, HIGH, TOP, preferred_high])

        assert ordered == [TOP, preferred_high, cheap_high, HIGH, PAIR]

    def test_body_lists_sorted_matches_with_link_baseline_and_hints(self) -> None:
        email = alert_email([PAIR, TOP], BASELINE, CTX, NOW)
        lines = email.body.splitlines()

        assert lines[0] == (
            "New ticket matches: Noah Kahan — Protective Stadium, Birmingham AL (Tue Apr 27, 2027)"
        )
        assert lines[1] == f"Open: {URL}"
        assert lines[3].startswith("1. [GREAT DEAL — 3 tickets ≤ $300] PRIMARY — Sec 123 · Row M")
        assert lines[4] == (
            "   $289.00/ticket excl. fees (≈$337.16 with fees, before tax) · can buy: 2 or 3"
        )
        assert lines[5] == "   Why: 3 together at ≤ $300 (preferred price)"
        assert lines[6].startswith("2. [2 only] PRIMARY")
        assert (
            f"Baseline: 7-day median of cheapest 2{EN_DASH}3 seat primary "
            "= $435.00 (412 samples)" in lines
        )
        assert "Seen 2026-06-01 10:04 CDT." in email.body
        assert "Choose quantity 3 (or 2) on the page" in email.body
        assert email.body.endswith("\n")

    def test_resale_and_max_together_shown(self) -> None:
        resale = match(
            listing(34350, qty=frozenset({2}), resale=True, max_together=4),
            Priority.NORMAL,
            Reason.RESALE_LISTED,
        )

        body = alert_email([resale], BASELINE, CTX, NOW).body

        assert (
            f"RESALE (Face Value Exchange) — Sec 123 · Row M · Seats 5{EN_DASH}7 "
            "(up to 4 together)" in body
        )
        assert "Why: new resale listing" in body

    def test_upgraded_identities_get_the_suffix(self) -> None:
        body = alert_email([TOP, PAIR], BASELINE, CTX, NOW, upgraded={TOP.listing.identity}).body

        assert body.count("(now buyable as") == 1

    def test_warming_up_baseline(self) -> None:
        body = alert_email([PAIR], Baseline(None, 5, ready=False), CTX, NOW).body

        assert "= warming up (5/12 samples)" in body

    def test_no_matches_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            alert_email([], BASELINE, CTX, NOW)


class TestAlertPush:
    @pytest.mark.parametrize(
        ("best", "priority", "tags", "title"),
        [
            (TOP, 5, ("rotating_light", "tada"), "🎉 GREAT DEAL: 3 tix at $289"),
            (HIGH, 4, ("tickets",), None),
            (PAIR, 3, ("tickets",), "Noah Kahan: pair at $195 (2 only)"),
        ],
    )
    def test_priority_and_tags_per_tier(
        self, best: Match, priority: int, tags: tuple[str, ...], title: str | None
    ) -> None:
        push = alert_push([best], CTX)

        assert (push.priority, push.tags, push.click) == (priority, tags, URL)
        if title is not None:
            assert push.title == title

    def test_lines_per_match_with_overflow(self) -> None:
        many = [
            match(listing(20000 + i, section=f"S{i}"), Priority.HIGH, Reason.UNDER_PRICE_CAP)
            for i in range(6)
        ]

        push = alert_push([TOP, *many], CTX)
        lines = push.message.splitlines()

        assert push.title.endswith("(+6 more)")
        assert lines[0] == f"★ 123/M $289 {TIMES}2-3"
        assert len(lines) == 5
        assert lines[-1] == "…and 3 more"


class TestCanary:
    def test_ok_with_no_data(self) -> None:
        email = canary(CanaryReport(now=NOW), CTX)

        assert email.subject == "ticket-poller daily canary — OK"
        assert "last success never · streak: none" in email.body
        assert "Cheapest primary  qty 3: none | qty 2: none" in email.body
        assert "warming up (0/12 samples)" in email.body
        assert "Resale: eligible unknown · captured n/a" in email.body

    def test_full_report(self) -> None:
        report = CanaryReport(
            now=NOW,
            outcomes={PollOutcome.OK: 280, PollOutcome.BLOCKED: 2},
            last_success_at=NOW - timedelta(minutes=4),
            primary_now={3: PricePoint(31200, "123 M"), 2: PricePoint(29000)},
            primary_low={3: 30100},
            resale_now={2: PricePoint(34350, "104 R")},
            baseline=Baseline(Decimal("38350.5"), 412, ready=True),
            resale_eligible=True,
            resale_captured_polls=270,
            resale_listings_now=4,
            alerts=2,
            great_deals=1,
            channel_failures={Channel.PUSH: 1},
            browser_recycles=1,
        )

        email = canary(report, CTX)
        lines = email.body.splitlines()

        assert email.subject == "ticket-poller daily canary — OK"
        assert lines[0] == (
            "Polls (24 h): 282 · ok 280 · partial 0 · blocked 2 · error 0 "
            "· last success 10:00 · streak: none"
        )
        assert lines[1] == (
            "Cheapest primary  qty 3: $312.00 (123 M), 24 h low $301.00 | qty 2: $290.00"
        )
        assert lines[2] == "Cheapest resale   qty 3: none | qty 2: $343.50 (104 R)"
        assert lines[3] == (
            f"Baseline (7-day median, cheapest 2{EN_DASH}3 primary): $383.51 (412 samples)"
        )
        assert lines[4] == "Resale: eligible yes · captured 270/280 polls · listings now 4"
        assert lines[5] == "Alerts (24 h): 2 (1 GREAT DEAL) · channel failures: email 0, push 1"
        assert lines[6].startswith("Browser recycles: 1 · Version ")
        assert lines[6].endswith("· Auto-stop 2027-04-28 00:00 CDT")

    def test_degraded_failing_and_resale_missing(self) -> None:
        report = CanaryReport(
            now=NOW,
            outcomes={PollOutcome.PARTIAL: 3, PollOutcome.BLOCKED: 5},
            failing_since=NOW - timedelta(hours=2, minutes=5),
            consecutive_failures=5,
            resale_eligible=True,
        )

        email = canary(report, CTX)

        assert email.subject == (
            "ticket-poller daily canary — DEGRADED: failing for 2h05m; resale not captured"
        )
        assert "streak: 5 failures since 07:59" in email.body
        assert "Resale: eligible yes · captured 0/3 polls" in email.body

    def test_short_failure_is_not_degraded(self) -> None:
        report = CanaryReport(
            now=NOW, failing_since=NOW - timedelta(minutes=20), consecutive_failures=2
        )

        assert canary(report, CTX).subject.endswith("— OK")


class TestNotices:
    def test_outage_includes_hint_and_counts(self) -> None:
        notice = outage(
            failing_for=timedelta(hours=1, minutes=5),
            outcome=PollOutcome.BLOCKED,
            counts={PollOutcome.BLOCKED: 10, PollOutcome.ERROR: 2},
            last_detail="http 403",
            next_retry_at=NOW + timedelta(minutes=5),
            ctx=CTX,
        )

        assert notice.kind is NoticeKind.OUTAGE
        assert notice.email.subject == "ticket-poller: polling failing for 1h05m (blocked)"
        assert "Failed polls this outage: blocked 10, error 2" in notice.email.body
        assert "Next retry: 2026-06-01 10:09 CDT" in notice.email.body
        assert "Hint: Ticketmaster is blocking the poller." in notice.email.body
        assert notice.push is not None
        assert (notice.push.priority, notice.push.tags) == (4, ("warning",))
        assert "blocking the poller" in notice.push.message

    def test_profile_in_use_hint(self) -> None:
        notice = outage(
            failing_for=timedelta(hours=1),
            outcome=PollOutcome.ERROR,
            counts={},
            last_detail="profile_in_use: SingletonLock",
            next_retry_at=None,
            ctx=CTX,
        )

        assert "Close the manual Chromium" in notice.email.body
        assert "Next retry: unknown" in notice.email.body

    def test_recovered(self) -> None:
        notice = recovered(outage_duration=timedelta(hours=3, minutes=12))

        assert notice.email.subject == "ticket-poller: recovered after 3h12m"
        assert notice.push is not None
        assert notice.push.priority == 3

    @pytest.mark.parametrize("capture", list(ResaleCapture))
    def test_resale_eligible(self, capture: ResaleCapture) -> None:
        notice = resale_eligible(capture=capture, ctx=CTX)

        assert notice.email.subject.endswith(f"capture: {capture.value.upper()}")
        assert notice.push is not None
        assert notice.push.priority == 4
        missing_note = "was not captured" in notice.email.body
        assert missing_note is (capture is not ResaleCapture.CAPTURED)

    def test_resale_missing_explains_how_to_capture(self) -> None:
        notice = resale_missing(polls=6, ctx=CTX)

        assert notice.email.subject == "ticket-poller: resale eligible but not captured for 6 polls"
        assert "poll-once --save" in notice.email.body


class TestSelftest:
    def test_contains_summary_and_sample_without_secrets(
        self, make_env_file: EnvFileFactory
    ) -> None:
        settings = load_settings(make_env_file())

        notice = selftest_notice(
            settings_summary=settings.summary(), ctx=MessageContext.from_settings(settings), now=NOW
        )

        assert notice.kind is NoticeKind.TEST
        assert notice.push is not None
        text = notice.email.body + notice.push.message
        assert "SENDER_APP_PASSWORD=set" in text
        assert "Subject: 🎉 GREAT DEAL: 3 Noah Kahan tickets at $289.00/ticket (+1 more)" in text
        for secret in (APP_PASSWORD, NTFY_TOPIC, NTFY_OPS_TOPIC, NTFY_TOKEN, HC_UUID):
            assert secret not in text

    def test_sample_matches_follow_the_rules(self) -> None:
        no_preferred = RuleConfig(
            wanted_quantities=frozenset({2}),
            preferred_quantities=frozenset(),
            primary_alert_max_price_cents=15000,
            primary_drop_percent=20,
            preferred_max_price_cents=30000,
            baseline_min_samples=12,
        )

        best, _ = sample_matches(no_preferred)

        assert best.priority is Priority.NORMAL
        assert best.listing.price_cents == 15000
