"""The ``run`` service loop and live ``poll_once`` (design §15 poller scenarios 1-11).

A real ``Store`` on ``tmp_path`` and a real ``Notifier``; the fetcher, senders, pinger and
clock are fakes, so nothing touches a browser or the network.
"""

import logging
import random
import smtplib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from tests.builders import (
    facets,
    load_captured,
    make_group,
    make_offer,
    make_pick,
    make_resale_offer,
    quickpicks,
)
from tests.fake_clock import FakeClock
from tests.fakes import FakeEmailSender, FakePinger, FakePushSender, ScriptedFetcher
from tests.sample_env import EnvFileFactory
from ticket_poller import fetcher, messages, notify
from ticket_poller.cli import ExitCode, main
from ticket_poller.config import Settings, load_settings
from ticket_poller.fetcher import Fetcher
from ticket_poller.messages import Email
from ticket_poller.models import CapturedResponse, Channel, FetchResult, PollOutcome
from ticket_poller.notify import Notifier
from ticket_poller.poller import (
    EMAIL_FAILURES_BEFORE_PUSH,
    MAX_WAIT_SECONDS,
    ExitReason,
    Poller,
    PollReport,
    audit_to,
    render_report,
)
from ticket_poller.store import Store
from ticket_poller.watchdog import Watchdog

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)  # 06:00 in Chicago: no canary due for 2 h
PRIMARY = (load_captured("nk_facets_inventorytypes_offer"), load_captured("nk_quickpicks_qty2"))
QP3 = load_captured("nk_quickpicks_qty3")
OUTAGE = "ticket-poller: polling failing for"
RECOVERED = "ticket-poller: recovered after"


def fetched(
    *responses: CapturedResponse,
    outcome: PollOutcome = PollOutcome.OK,
    detail: str = "",
    resale_eligible: bool | None = False,
    recycled: bool = False,
) -> FetchResult:
    return FetchResult(outcome, responses, detail, resale_eligible, 3.0, recycled)


NK = fetched(*PRIMARY, QP3)
BLOCKED = fetched(outcome=PollOutcome.BLOCKED, detail="blocked: document 403", resale_eligible=None)


class Midpoint(random.Random):
    """No jitter: every delay is exactly its base."""

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2


class BoundedClock(FakeClock):
    """Requests stop (like SIGTERM) instead of waiting past ``until``, so ``run`` returns."""

    until: datetime | None = None

    def wait(self, seconds: float) -> bool:
        if (
            self.until is not None
            and self.now() + timedelta(seconds=max(0.0, seconds)) > self.until
        ):
            self.stop.set()
        return super().wait(seconds)


@dataclass
class Service:
    settings: Settings
    store: Store
    clock: BoundedClock
    fetcher: ScriptedFetcher
    email: FakeEmailSender
    alert_push: FakePushSender
    ops_push: FakePushSender
    pinger: FakePinger
    poller: Poller

    def run_for(self, duration: timedelta) -> ExitReason:
        self.clock.until = self.clock.now() + duration
        return self.poller.run()

    def poll(self, *, dry_run: bool = False) -> PollReport:
        report = self.poller.poll_once(dry_run=dry_run)
        assert report is not None
        return report

    def poll_times(self) -> list[timedelta]:
        return sorted(poll.started_at - T0 for poll in self.store.recent_polls(100))

    def minutes(self) -> timedelta:
        return self.clock.now() - T0


type ServiceFactory = Callable[..., Service]


@pytest.fixture
def make_service(make_env_file: EnvFileFactory) -> Iterator[ServiceFactory]:
    """``make_service(*fetch_script, start=T0, **env)`` builds a fully faked service."""
    stores: list[Store] = []

    def make(*script: Any, start: datetime = T0, **env: str) -> Service:
        make_env_file(PRIMARY_ALERT_MAX_PRICE="350.00", PREFERRED_MAX_PRICE="390.00", **env)
        settings = load_settings()
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        store = Store.open_writer(settings.db_path)
        stores.append(store)
        clock = BoundedClock(start)
        email, alert_push, ops_push = FakeEmailSender(), FakePushSender(), FakePushSender()
        notifier = Notifier(
            email,
            alert_push,
            ops_push,
            stop=clock.stop,
            wait=clock.wait,
            audit=audit_to(store, clock),
        )
        source = ScriptedFetcher(*script)
        pinger = FakePinger()
        service = Poller(
            settings, source, store, notifier, pinger, clock, clock.stop, rng=Midpoint()
        )
        return Service(settings, store, clock, source, email, alert_push, ops_push, pinger, service)

    yield make
    for store in stores:
        store.close()


def minutes(value: float) -> timedelta:
    return timedelta(minutes=value)


# --- 1, 2: delivery and per-channel retry ------------------------------------------------------


def test_new_matches_alert_once_per_channel(make_service: ServiceFactory) -> None:
    svc = make_service(NK)

    assert svc.run_for(minutes(12)) is ExitReason.SIGNAL

    assert svc.poll_times() == [minutes(0), minutes(5), minutes(10)]
    ((email, to),) = svc.email.sent
    assert email.subject.startswith("🎉 GREAT DEAL: 3 ")
    assert email.subject.endswith("(+2 more)")
    assert to == svc.settings.alert_recipients
    assert len(svc.alert_push.sent) == 1
    assert svc.ops_push.sent == []
    assert svc.pinger.pings == 3


def sent_on(report: PollReport) -> set[frozenset[Channel]]:
    assert report.findings is not None
    return {status.sent for status in report.findings.matches}


def test_failed_channel_is_retried_alone_on_the_next_poll(make_service: ServiceFactory) -> None:
    svc = make_service(NK)
    svc.email.fail = True

    first = svc.poll()
    svc.email.fail = False
    second = svc.poll()
    third = svc.poll()

    assert "not sent (retried next poll): email" in render_report(first, svc.settings)
    assert sent_on(first) == {frozenset({Channel.PUSH})}
    assert sent_on(second) == {frozenset({Channel.EMAIL})}
    assert len(svc.email.sent) == 1
    assert svc.alert_push.calls == 1
    assert third.findings is not None
    assert third.findings.new_matches == []


# --- 3, 4, 11: outage and recovery -------------------------------------------------------------


def test_blocked_timeline_announces_the_outage_at_60_minutes(
    make_service: ServiceFactory,
) -> None:
    svc = make_service(BLOCKED, BLOCKED, BLOCKED, BLOCKED, NK)
    notices: list[tuple[timedelta, str]] = []
    svc.ops_push.on_send = lambda push: notices.append((svc.minutes(), push.title))

    svc.run_for(minutes(168))

    assert svc.poll_times() == [minutes(m) for m in (0, 15, 45, 105, 165)]
    assert notices == [
        (minutes(60), f"{OUTAGE} 1h00m (blocked)"),
        (minutes(165), f"{RECOVERED} 2h45m"),
    ]
    outage = next(email for email, _ in svc.email.sent if email.subject.startswith(OUTAGE))
    assert "Hint: Ticketmaster is blocking the poller." in outage.body
    assert svc.store.get_state().recovery_pending is False


def test_failed_outage_notice_is_retried_until_one_channel_delivers(
    make_service: ServiceFactory,
) -> None:
    svc = make_service(BLOCKED)
    attempts: list[timedelta] = []

    def email_attempt(email: Email) -> None:
        if email.subject.startswith(OUTAGE):
            attempts.append(svc.minutes())
        svc.email.fail = svc.minutes() < minutes(100)

    svc.email.on_send = email_attempt
    svc.ops_push.fail = True

    svc.run_for(minutes(400))

    assert attempts == [minutes(60), minutes(105)]  # then never again
    assert svc.ops_push.calls == 2
    assert svc.store.get_state().outage_announced


def test_profile_in_use_backs_off_and_keeps_the_profile_lock(
    make_service: ServiceFactory,
) -> None:
    in_use = fetched(outcome=PollOutcome.ERROR, detail="profile_in_use", resale_eligible=None)
    svc = make_service(in_use)
    lock = svc.settings.browser_profile_dir / "SingletonLock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("other-host-123")

    svc.run_for(minutes(70))

    assert svc.poll_times() == [minutes(m) for m in (0, 5, 15, 35, 65)]
    ((outage, _),) = svc.email.sent
    assert outage.subject == f"{OUTAGE} 1h00m (error)"
    assert "Hint: Close the manual Chromium" in outage.body
    assert svc.fetcher.closes == 5
    assert lock.read_text() == "other-host-123"


def test_a_crashing_poll_is_recorded_and_backed_off(make_service: ServiceFactory) -> None:
    def crash() -> FetchResult:
        raise RuntimeError("boom")

    svc = make_service(crash, NK)

    svc.run_for(minutes(6))

    (latest, crashed) = svc.store.recent_polls(2)
    assert (crashed.outcome, crashed.detail) == (PollOutcome.ERROR, "crash: RuntimeError: boom")
    assert latest.started_at - crashed.started_at == minutes(5)
    assert latest.outcome is PollOutcome.OK


# --- 5: canary ----------------------------------------------------------------------------------


def test_canary_at_8am_across_dst_retries_a_failed_email(make_service: ServiceFactory) -> None:
    start = datetime(2027, 3, 13, 12, 10, tzinfo=UTC)  # 06:10 CST; DST starts 2027-03-14
    svc = make_service(NK, start=start, POLL_INTERVAL_SECONDS="3600", POLL_JITTER_SECONDS="0")
    canaries: list[datetime] = []

    def email_attempt(email: Email) -> None:
        is_canary = "daily canary" in email.subject
        if is_canary:
            canaries.append(svc.clock.now())
        svc.email.fail = is_canary and len(canaries) == 1

    svc.email.on_send = email_attempt

    svc.clock.until = datetime(2027, 3, 15, 0, 0, tzinfo=UTC)
    svc.poller.run()

    assert canaries == [
        datetime(2027, 3, 13, 14, 0, tzinfo=UTC),  # 08:00 CST: the email fails
        datetime(2027, 3, 13, 14, 10, tzinfo=UTC),  # retried at the next wake (a poll)
        datetime(2027, 3, 14, 13, 0, tzinfo=UTC),  # 08:00 CDT
    ]
    canary_to = {to for email, to in svc.email.sent if "daily canary" in email.subject}
    assert canary_to == {(svc.settings.admin_email,)}
    assert svc.ops_push.titles == ["ticket-poller: email delivery failing"]
    state = svc.store.get_state()
    assert state.last_canary_date == date(2027, 3, 14)
    assert (state.consecutive_email_failures, state.email_failing_notice_sent) == (0, False)


# --- email failing: one ops push per episode ----------------------------------------------------

EMAIL_FAILING = "ticket-poller: email delivery failing"


def test_repeatedly_failing_alert_emails_push_once_until_an_email_gets_through(
    make_service: ServiceFactory,
) -> None:
    svc = make_service(NK)
    svc.email.fail = True

    for _ in range(EMAIL_FAILURES_BEFORE_PUSH - 1):
        svc.poll()
    assert svc.ops_push.titles == []
    for _ in range(3):
        svc.poll()

    assert svc.ops_push.titles == [EMAIL_FAILING]
    assert "3 email send(s) in a row failed" in svc.ops_push.sent[0].message
    state = svc.store.get_state()
    assert (state.consecutive_email_failures, state.email_failing_notice_sent) == (5, True)

    svc.email.fail = False
    svc.poll()  # the owed alert email is delivered: the episode ends

    state = svc.store.get_state()
    assert (state.consecutive_email_failures, state.email_failing_notice_sent) == (0, False)
    assert svc.ops_push.titles == [EMAIL_FAILING]


def test_a_rejected_smtp_login_pushes_at_once(make_service: ServiceFactory) -> None:
    svc = make_service(NK)

    def reject(email: Email) -> None:
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    svc.email.on_send = reject

    svc.poll()

    (push,) = svc.ops_push.sent
    assert push.title == EMAIL_FAILING
    assert "SENDER_APP_PASSWORD" in push.message


def test_a_failed_email_failing_push_is_retried_on_the_next_failed_email(
    make_service: ServiceFactory,
) -> None:
    svc = make_service(NK)
    svc.email.fail = svc.ops_push.fail = True
    for _ in range(EMAIL_FAILURES_BEFORE_PUSH):
        svc.poll()
    assert svc.store.get_state().email_failing_notice_sent is False

    svc.ops_push.fail = False
    svc.poll()

    assert svc.ops_push.titles == [EMAIL_FAILING]
    assert svc.store.get_state().email_failing_notice_sent is True


# --- 6, 7: upgrade and re-arm -------------------------------------------------------------------


def seats(*groups: tuple[list[int], int]) -> FetchResult:
    """Primary row 101 N at $300: each ``(seats, qty)`` is a quickpicks group for that qty."""
    offer = make_offer(list_price=300.0, sellable=(2, 3))
    responses = [
        quickpicks(qty, [make_pick(make_group(group), max_quantity=3)] if group else [], [offer])
        for group, qty in groups
    ]
    return fetched(facets([offer]), *responses)


def test_pair_upgraded_to_a_triple_alerts_once_more(make_service: ServiceFactory) -> None:
    pair = seats(([1, 2], 2), ([], 3))
    triple = seats(([1, 2], 2), ([1, 2, 3], 3))
    svc = make_service(pair, triple, triple)

    svc.poll()
    svc.poll()
    svc.poll()

    first, upgrade = (email for email, _ in svc.email.sent)
    assert ": pair available (2 only) — $300.00/ticket" in first.subject
    assert upgrade.subject.startswith("🎉 GREAT DEAL: 3 ")
    assert "now buyable as 3" in upgrade.body
    assert len(svc.alert_push.sent) == 2


@pytest.mark.parametrize(("absent_hours", "alerts"), [(25, 2), (2, 1)])
def test_listing_back_after_24h_absence_alerts_again(
    make_service: ServiceFactory, absent_hours: int, alerts: int
) -> None:
    svc = make_service(NK, seats(([7, 8], 2)), NK)

    svc.poll()
    svc.clock.advance(5 * 60)
    svc.poll()  # the NK listings are gone
    svc.clock.advance(absent_hours * 3600)
    svc.poll()

    nk_alerts = [email for email, _ in svc.email.sent if "(+2 more)" in email.subject]
    assert len(nk_alerts) == alerts


RESALE_L1 = fetched(facets([make_resale_offer("L1")]), resale_eligible=True)
RESALE_MISSING = fetched(facets([]), resale_eligible=True)
RESALE_NONE = fetched(
    replace(facets([]), url=facets([]).url + "&includeResale=true"), resale_eligible=True
)


@pytest.mark.parametrize(
    ("absent", "outcome", "alerts"),
    [(RESALE_MISSING, PollOutcome.PARTIAL, 1), (RESALE_NONE, PollOutcome.OK, 2)],
    ids=["resale-not-captured", "resale-captured-without-it"],
)
def test_resale_listing_back_after_24h_of_partial_polls_does_not_alert_again(
    make_service: ServiceFactory, absent: FetchResult, outcome: PollOutcome, alerts: int
) -> None:
    svc = make_service(RESALE_L1, absent, absent, RESALE_L1)

    svc.poll()
    svc.clock.advance(5 * 60)
    assert svc.poll().outcome is outcome
    svc.clock.advance(25 * 3600)
    assert svc.poll().outcome is outcome
    svc.clock.advance(5 * 60)
    svc.poll()

    assert len(svc.alert_push.sent) == alerts


# --- 8: resale MISSING --------------------------------------------------------------------------


def test_missing_resale_capture_is_partial_and_noticed_once(make_service: ServiceFactory) -> None:
    svc = make_service(fetched(*PRIMARY, QP3, resale_eligible=True))

    outcomes = [svc.poll().outcome for _ in range(5)]

    assert outcomes == [PollOutcome.PARTIAL] * 5
    assert svc.pinger.pings == 5
    assert len(svc.alert_push.sent) == 1
    assert svc.ops_push.titles == [
        "ticket-poller: FVE resale now eligible — capture: MISSING",
        "ticket-poller: resale eligible but not captured for 3 polls",
    ]
    admin = [to for email, to in svc.email.sent if email.subject.startswith("ticket-poller:")]
    assert admin == [(svc.settings.admin_email,)] * 2


# --- parser health ------------------------------------------------------------------------------


def unreadable_quickpicks(picks: int) -> FetchResult:
    """Quickpicks whose every group points at an offer missing from the response."""
    groups = [
        make_pick(make_group([1, 2], offers=["GONE"], row=row), row=row)
        for row in "ABCDEFGH"[:picks]
    ]
    return fetched(*PRIMARY, quickpicks(3, groups, []))


def test_unreadable_quickpicks_are_a_degraded_partial_poll(
    make_service: ServiceFactory, caplog: pytest.LogCaptureFixture
) -> None:
    svc = make_service(unreadable_quickpicks(6))

    with caplog.at_level(logging.WARNING, logger="ticket_poller"):
        report = svc.poll()

    assert report.outcome is PollOutcome.PARTIAL
    assert report.detail.startswith("parse degraded: quickpicks qty=3: no seat group parsed")
    assert svc.pinger.pings == 1
    (poll,) = svc.store.recent_polls(1)
    assert (poll.outcome, poll.parse_warnings, poll.parse_degraded) == (
        PollOutcome.PARTIAL,
        7,
        True,
    )
    parse_lines = [r.getMessage() for r in caplog.records if "parse warning" in r.getMessage()]
    assert len(parse_lines) == 4
    assert parse_lines[0].startswith("parse warning (degraded): quickpicks qty=3: no seat group")
    assert parse_lines[-1] == "4 more parse warnings this poll (1 degraded in total)"
    assert all(r.levelno == logging.WARNING for r in caplog.records if "parse" in r.getMessage())


def test_parse_warnings_degrade_the_next_canary(make_service: ServiceFactory) -> None:
    svc = make_service(unreadable_quickpicks(1), NK)

    svc.run_for(timedelta(hours=2, minutes=5))

    (canary,) = (email for email, _ in svc.email.sent if "daily canary" in email.subject)
    assert canary.subject.endswith("DEGRADED: parse warnings in 1 polls (1 degraded)")


# --- 9: shutdown --------------------------------------------------------------------------------


def test_stop_mid_capture_records_nothing(make_service: ServiceFactory) -> None:
    def interrupted() -> None:
        svc.clock.stop.set()

    svc = make_service(interrupted)

    assert svc.poller.run() is ExitReason.SIGNAL

    assert svc.store.recent_polls(1) == []
    assert svc.email.sent == []
    assert svc.alert_push.calls == 0
    assert svc.pinger.pings == 0


def test_stop_during_the_email_leaves_both_channels_pending(make_service: ServiceFactory) -> None:
    svc = make_service(NK)

    def hang_until_stopped(_: Email) -> None:
        svc.clock.stop.set()  # SIGTERM arrives while SMTP blocks; the socket then times out
        raise TimeoutError("timed out")

    svc.email.on_send = hang_until_stopped

    assert svc.poller.run() is ExitReason.SIGNAL

    assert svc.alert_push.calls == 0
    assert svc.store.recent_polls(1)[0].outcome is PollOutcome.OK
    svc.clock.stop.clear()
    after = svc.poll(dry_run=True)
    assert after.findings is not None
    assert {status.pending for status in after.findings.matches} == {frozenset(Channel)}


# --- 10: event over -----------------------------------------------------------------------------


def test_event_over_sends_the_finished_notice_once(make_service: ServiceFactory) -> None:
    svc = make_service(NK, STOP_AFTER="2026-11-14T12:10:00+00:00")

    assert svc.poller.run() is ExitReason.EVENT_OVER

    assert svc.minutes() == minutes(10)
    assert svc.poll_times() == [minutes(0), minutes(5)]
    assert svc.ops_push.titles == ["ticket-poller stopped: event date passed"]

    svc.clock.advance(3600)  # a restart later exits at once, silently
    assert svc.poller.run() is ExitReason.EVENT_OVER
    assert svc.fetcher.fetches == 2
    assert len(svc.ops_push.sent) == 1


def test_run_command_exits_0_after_the_event(
    make_env_file: EnvFileFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_env_file(STOP_AFTER="2026-01-01T00:00:00")
    source = ScriptedFetcher(NK)
    email, push = FakeEmailSender(), FakePushSender()

    def fake_notifier(settings: Settings, client: object, **kwargs: Any) -> Notifier:
        return Notifier(email, push, push, **kwargs)

    monkeypatch.setattr(fetcher, "build_fetcher", lambda *args: source)
    monkeypatch.setattr(notify, "build_notifier", fake_notifier)
    monkeypatch.setattr(notify, "build_pinger", lambda *args, **kwargs: FakePinger())

    assert main(["run"]) == ExitCode.OK

    assert source.fetches == 0
    assert email.subjects == ["ticket-poller stopped: event date passed"]
    assert source.closes == 1


# --- dry run and browser recycling --------------------------------------------------------------


def test_dry_run_writes_and_sends_nothing(make_service: ServiceFactory) -> None:
    svc = make_service(NK)
    svc.poll()
    svc.store.close()
    before = svc.settings.db_path.read_bytes()

    with Store.open_readonly(svc.settings.db_path) as readonly:
        source: Fetcher = ScriptedFetcher(fetched(*PRIMARY, QP3, recycled=True))
        email, push, pinger = FakeEmailSender(), FakePushSender(), FakePinger()
        notifier = Notifier(email, push, push, stop=svc.clock.stop)
        dry = Poller(svc.settings, source, readonly, notifier, pinger, svc.clock, svc.clock.stop)
        report = dry.poll_once(dry_run=True)

    assert report is not None
    assert report.dry_run
    assert svc.settings.db_path.read_bytes() == before
    assert email.sent == []
    assert push.calls == 0
    assert pinger.pings == 0


def test_browser_recycle_is_recorded(make_service: ServiceFactory) -> None:
    svc = make_service(fetched(*PRIMARY, QP3, recycled=True))

    svc.poll()

    assert svc.store.recent_polls(1)[0].browser_recycled


def test_failed_poll_closes_the_browser_for_a_relaunch(make_service: ServiceFactory) -> None:
    svc = make_service(BLOCKED, NK)

    svc.poll()
    svc.poll()

    assert svc.fetcher.closes == 1


# --- loop robustness: wall-clock steps and the watchdog -----------------------------------------


def test_idle_waits_are_capped_at_60s_without_extra_polls_or_logs(
    make_service: ServiceFactory, caplog: pytest.LogCaptureFixture
) -> None:
    svc = make_service(BLOCKED, BLOCKED, NK)

    with caplog.at_level(logging.DEBUG, logger="ticket_poller"):
        svc.run_for(minutes(49))

    assert svc.poll_times() == [minutes(m) for m in (0, 15, 45)]
    assert max(svc.clock.waits) <= MAX_WAIT_SECONDS
    assert len(svc.clock.waits) > 45
    assert len(caplog.records) < 15


def test_a_backward_clock_step_does_not_delay_the_next_poll(
    make_service: ServiceFactory, caplog: pytest.LogCaptureFixture
) -> None:
    waited_at_fetch: list[float] = []

    def poll() -> FetchResult:
        waited_at_fetch.append(sum(svc.clock.waits))
        return NK

    svc = make_service(poll)
    stepped = False
    real_wait = svc.clock.wait

    def wait(seconds: float) -> bool:
        nonlocal stepped
        if not stepped and waited_at_fetch:
            stepped = True
            svc.clock.advance(-2 * 3600)  # NTP steps the wall clock back by 2 h
        return real_wait(seconds)

    svc.clock.wait = wait  # type: ignore[method-assign]
    svc.clock.until = T0 + minutes(7)
    svc.poller.run()

    first, second, *_ = waited_at_fetch
    assert second - first == 300
    steps = [r for r in caplog.records if "wall clock stepped back" in r.getMessage()]
    assert len(steps) == 1


class RecordingWatchdog(Watchdog):
    def __init__(self) -> None:
        self.exits: list[int] = []
        self.marks: list[str] = []
        super().__init__(900, exit_fn=self.exits.append)

    def busy(self, what: str) -> None:
        self.marks.append(what)
        super().busy(what)

    def idle(self) -> None:
        self.marks.append("idle")
        super().idle()


def test_the_loop_is_busy_only_while_working(make_service: ServiceFactory) -> None:
    svc = make_service(BLOCKED, NK)
    watchdog = RecordingWatchdog()
    svc.poller._watchdog = watchdog
    waits_while_busy: list[str] = []
    real_wait = svc.clock.wait

    def wait(seconds: float) -> bool:
        if watchdog._busy is not None and seconds > 30:  # notifier retries stay short
            waits_while_busy.append(watchdog._busy[0])
        return real_wait(seconds)

    svc.clock.wait = wait  # type: ignore[method-assign]
    svc.run_for(minutes(19))

    assert watchdog.marks == ["poll", "browser close", "notices", "idle", "poll", "notices", "idle"]
    assert waits_while_busy == []
    assert watchdog.exits == []


# --- robustness: bugs and restarts must not kill the loop or hammer the site ------------------


def raise_bug(*args: object, **kwargs: object) -> Any:
    raise RuntimeError("bug")


def test_a_bug_after_the_commit_is_not_a_second_failed_poll(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = make_service(NK)
    monkeypatch.setattr(messages, "alert_email", raise_bug)

    report = svc.poll()

    assert report.outcome is PollOutcome.OK
    assert [poll.outcome for poll in svc.store.recent_polls(10)] == [PollOutcome.OK]
    assert svc.store.get_state().consecutive_failures == 0
    assert svc.pinger.pings == 0
    assert svc.fetcher.closes == 0
    pending = svc.poll(dry_run=True).findings
    assert pending is not None
    assert {status.pending for status in pending.matches} == {frozenset(Channel)}


def test_a_failing_notice_does_not_stop_the_loop(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime(2026, 11, 14, 13, 58, tzinfo=UTC)  # 07:58 in Chicago
    svc = make_service(NK, start=start)
    monkeypatch.setattr(messages, "canary", raise_bug)

    assert svc.run_for(minutes(12)) is ExitReason.SIGNAL

    polls = [poll.started_at - start for poll in svc.store.recent_polls(10)]
    assert sorted(polls) == [minutes(0), minutes(5), minutes(10)]
    assert svc.store.get_state().last_canary_date is None


def test_a_failing_finished_notice_still_exits_for_the_event(
    make_service: ServiceFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = make_service(NK, STOP_AFTER="2026-11-14T11:00:00+00:00")
    monkeypatch.setattr(messages, "finished", raise_bug)

    assert svc.poller.run() is ExitReason.EVENT_OVER
    assert not svc.store.get_state().finished_notice_sent


def test_a_restart_resumes_the_persisted_failure_backoff(make_service: ServiceFactory) -> None:
    svc = make_service(BLOCKED, NK)
    svc.poll()
    svc.clock.advance(2 * 60)  # systemd restarts the crashed service

    svc.run_for(minutes(16))

    assert svc.poll_times() == [minutes(0), minutes(15)]


def test_a_failed_notice_due_during_a_slow_poll_waits_for_the_next_wake(
    make_service: ServiceFactory,
) -> None:
    def slow_block() -> FetchResult:
        svc.clock.advance(20 * 60)  # 45 -> 65 min, past the 60-min outage deadline
        return BLOCKED

    svc = make_service(BLOCKED, BLOCKED, slow_block, BLOCKED)
    attempts: list[timedelta] = []
    svc.ops_push.on_send = lambda push: (
        attempts.append(svc.minutes()) if push.title.startswith(OUTAGE) else None
    )
    svc.email.fail = svc.ops_push.fail = True

    svc.run_for(minutes(130))

    assert svc.poll_times() == [minutes(m) for m in (0, 15, 45, 125)]
    assert attempts == [minutes(65), minutes(120), minutes(125)]  # wakes: canary, poll


def test_no_healthchecks_ping_once_stop_is_requested(make_service: ServiceFactory) -> None:
    svc = make_service(NK)
    svc.alert_push.on_send = lambda push: svc.clock.stop.set()

    svc.poll()

    assert svc.alert_push.sent
    assert svc.pinger.pings == 0
