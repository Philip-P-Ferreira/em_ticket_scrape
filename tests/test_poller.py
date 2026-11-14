"""``poll-once``: the dry run (fetch → parse → rules, read-only) and ``--notify``."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.builders import EVENT_API, load_captured
from tests.fake_clock import FakeClock
from tests.fakes import FakeEmailSender, FakeFetcher, FakePinger, FakePushSender
from tests.sample_env import EnvFileFactory
from ticket_poller import fetcher, notify
from ticket_poller.cli import ExitCode, main
from ticket_poller.config import Settings, load_settings
from ticket_poller.models import CapturedResponse, Channel, FetchResult, PollOutcome
from ticket_poller.notify import Notifier
from ticket_poller.parsing import parse_inventory
from ticket_poller.poller import Poller, PollReport
from ticket_poller.report import render_report
from ticket_poller.rules import candidates, compute_baseline, evaluate
from ticket_poller.store import Store, key_matches

NOW = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
PRIMARY = (load_captured("nk_facets_inventorytypes_offer"), load_captured("nk_quickpicks_qty2"))
QP3 = load_captured("nk_quickpicks_qty3")


def fetched(
    *responses: CapturedResponse,
    outcome: PollOutcome = PollOutcome.OK,
    detail: str = "",
    resale_eligible: bool | None = False,
    recycled: bool = False,
) -> FetchResult:
    return FetchResult(outcome, responses, detail, resale_eligible, 11.24, recycled)


@pytest.fixture
def settings(make_env_file: EnvFileFactory) -> Settings:
    make_env_file(PRIMARY_ALERT_MAX_PRICE="350.00", PREFERRED_MAX_PRICE="390.00")
    return load_settings()


@pytest.fixture
def fake_fetcher(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeFetcher]:
    fake = FakeFetcher(fetched(*PRIMARY, QP3))
    monkeypatch.setattr(fetcher, "build_fetcher", lambda *args: fake)
    yield fake
    assert fake.closed


def dry_poller(settings: Settings, result: FetchResult | None, store: Store) -> Poller:
    """A poller whose notifier and pinger would fail the test if a dry run used them."""
    clock = FakeClock(NOW)
    notifier = Notifier(
        FakeEmailSender(fail=True), *[FakePushSender(fail=True)] * 2, stop=clock.stop
    )
    return Poller(settings, FakeFetcher(result), store, notifier, FakePinger(), clock, clock.stop)


def preview(
    settings: Settings, result: FetchResult | None, store: Store | None = None
) -> PollReport:
    if store is None:
        with Store.open_empty() as empty:
            found = dry_poller(settings, result, empty).poll_once(dry_run=True)
    else:
        found = dry_poller(settings, result, store).poll_once(dry_run=True)
    assert found is not None
    return found


class TestPreview:
    def test_without_state_every_match_is_new(self, settings: Settings) -> None:
        report = render_report(preview(settings, fetched(*PRIMARY, QP3)), settings)

        lines = report.splitlines()
        assert lines[:14] == [
            "poll ok in 11.2s",
            "listings: primary=6 resale=0 · resale_capture=not_eligible"
            " (page isResaleEligible=false)",
            "  PRIMARY Sec 104 Row R Seats 9\u201310 · $343.50 (≈$405.35 all-in) · qty 2",
            "  PRIMARY Sec 123 Row M Seats 5\u20137 · $383.50 (≈$452.55 all-in) · qty 2,3",
            "  PRIMARY Sec 101 Row N Seats 12\u201314 · $383.50 (≈$452.55 all-in) · qty 2,3",
            "  PRIMARY Sec 104 Row Q Seats 13\u201314 · $383.50 (≈$452.55 all-in) · qty 2",
            "  PRIMARY Sec 103 Row M Seats 17\u201318 · $383.50 (≈$452.55 all-in) · qty 2",
            "  PRIMARY Sec 126 Row J Seats 5\u20136 · $403.50 (≈$476.15 all-in) · qty 2",
            "quickpicks: qty 2 captured, qty 3 captured",
            "cheapest primary: q2=$343.50 · q3=$383.50",
            "cheapest resale: q2=none · q3=none",
            "baseline: warming up (0/12 samples)",
            "candidates: 6 · matches: 3",
            "(no state yet: every match counts as new)",
        ]
        great = "[GREAT DEAL — 3 tickets ≤ $390] PRIMARY"
        preferred = "   Why: 3 together at ≤ $390 (preferred price) · would alert: email, push"
        assert lines[14:] == [
            f"1. {great} Sec 101 Row N Seats 12\u201314 · $383.50 (≈$452.55 all-in) · qty 2,3",
            preferred,
            f"2. {great} Sec 123 Row M Seats 5\u20137 · $383.50 (≈$452.55 all-in) · qty 2,3",
            preferred,
            "3. [2 only · ≤ $390] PRIMARY Sec 104 Row R Seats 9\u201310 · $343.50 (≈$405.35 all-in)"
            " · qty 2",
            "   Why: ≤ $350 per ticket · would alert: email, push",
        ]

    def test_inferred_qty3_is_reported(self, settings: Settings) -> None:
        report = render_report(preview(settings, fetched(*PRIMARY)), settings)

        assert "quickpicks: qty 2 captured, qty 3 inferred from maxQuantity" in report
        assert "warning: no quickpicks for qty=3; inferred from maxQuantity" in report

    def test_delivered_matches_are_already_sent(self, settings: Settings) -> None:
        result = fetched(*PRIMARY, QP3)
        deliver(settings, result, sent=2, email_only=1)
        before = settings.db_path.read_bytes()

        with Store.open_readonly(settings.db_path) as store:
            report = render_report(preview(settings, result, store), settings)

        assert settings.db_path.read_bytes() == before
        alerts = [line.rsplit(" · ", 1)[1] for line in report.splitlines() if "Why:" in line]
        assert sorted(alerts) == ["already sent", "already sent", "would alert: push"]
        assert "no state yet" not in report

    def test_failed_fetch_is_passed_through_with_a_hint(self, settings: Settings) -> None:
        blocked = fetched(
            outcome=PollOutcome.BLOCKED, detail="blocked: document 403", resale_eligible=None
        )

        report = render_report(preview(settings, blocked), settings)

        assert report.splitlines()[:2] == ["poll blocked in 11.2s", "detail: blocked: document 403"]
        assert report.splitlines()[2].startswith("hint: Ticketmaster is blocking the poller.")
        assert "listings" not in report

    def test_parse_error_writes_a_debug_dump(self, settings: Settings) -> None:
        broken = CapturedResponse(f"{EVENT_API}/quickpicks?qty=2", 200, {"picks": "x"})

        found = preview(settings, fetched(PRIMARY[0], broken))

        assert found.outcome is PollOutcome.ERROR
        assert found.detail == "parse: 'picks': expected a list, got str"
        assert found.findings is None
        (dump,) = settings.debug_dir.iterdir()
        assert dump.name.endswith("-parse-picks-expected-a-list-got-str.json")

    def test_unwritable_debug_dir_only_warns(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings.debug_dir.parent.mkdir(parents=True, exist_ok=True)
        settings.debug_dir.write_text("not a directory")
        broken = CapturedResponse(f"{EVENT_API}/quickpicks?qty=2", 200, {"picks": "x"})

        found = preview(settings, fetched(PRIMARY[0], broken))

        assert found.outcome is PollOutcome.ERROR
        assert "could not write a debug dump" in caplog.text

    def test_missing_resale_capture_is_partial(self, settings: Settings) -> None:
        found = preview(settings, fetched(*PRIMARY, resale_eligible=True, recycled=True))

        report = render_report(found, settings)

        assert found.outcome is PollOutcome.PARTIAL
        assert report.splitlines()[0] == "poll partial in 11.2s (browser recycled)"
        assert "resale_capture=missing (page isResaleEligible=true)" in report

    def test_stopped_fetch(self, settings: Settings) -> None:
        with Store.open_empty() as store:
            assert dry_poller(settings, None, store).poll_once(dry_run=True) is None


def deliver(settings: Settings, result: FetchResult, *, sent: int, email_only: int) -> None:
    """Mark the first ``sent`` matches delivered everywhere and the next ``email_only`` by email."""
    rules = settings.rule_config()
    inventory = parse_inventory(result.responses, rules.wanted_quantities)
    current = candidates(inventory, rules)
    baseline = compute_baseline([], settings.baseline_min_samples)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    with Store.open_writer(settings.db_path) as store:
        seen_at = NOW - timedelta(hours=1)
        resolved = store.resolve(current, seen_at, timedelta(hours=24))
        store.observe(resolved, current, seen_at)
        keyed = key_matches(evaluate(inventory, baseline, rules), resolved)
        for channel in Channel:
            store.mark_delivered(keyed[:sent], channel, seen_at)
        store.mark_delivered(keyed[sent : sent + email_only], Channel.EMAIL, seen_at)


class TestPollOnceCommand:
    def test_prints_the_preview_without_creating_state(
        self, settings: Settings, fake_fetcher: FakeFetcher, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["poll-once"]) == ExitCode.OK

        out = capsys.readouterr().out
        assert out.startswith("poll ok in 11.2s\n")
        assert "(no state yet: every match counts as new)" in out
        assert fake_fetcher.fetches == 1
        assert not settings.db_path.exists()

    def test_save_writes_the_responses(
        self, settings: Settings, fake_fetcher: FakeFetcher, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["poll-once", "--save", "captured"]) == ExitCode.OK

        assert "saved 3 responses to captured" in capsys.readouterr().out
        assert sorted(p.name for p in Path("captured").iterdir()) == [
            "01-facets-200.json",
            "02-quickpicks-200.json",
            "03-quickpicks-200.json",
        ]

    def test_failed_poll_exits_1(
        self, settings: Settings, fake_fetcher: FakeFetcher, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_fetcher.result = fetched(outcome=PollOutcome.ERROR, detail="profile_in_use")

        assert main(["poll-once"]) == ExitCode.CRASH

        assert "hint: Close the manual Chromium" in capsys.readouterr().out

    def test_stopped(
        self, settings: Settings, fake_fetcher: FakeFetcher, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_fetcher.result = None

        assert main(["poll-once"]) == ExitCode.OK

        assert capsys.readouterr().out == "poll-once: stopped\n"

    def test_notify_records_the_poll_and_sends_the_alerts(
        self,
        settings: Settings,
        fake_fetcher: FakeFetcher,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        email, push, pinger = FakeEmailSender(), FakePushSender(), FakePinger()

        def fake_notifier(settings: Settings, client: object, **kwargs: Any) -> Notifier:
            return Notifier(email, push, push, **kwargs)

        monkeypatch.setattr(notify, "build_notifier", fake_notifier)
        monkeypatch.setattr(notify, "build_pinger", lambda *args, **kwargs: pinger)

        assert main(["poll-once", "--notify"]) == ExitCode.OK

        out = capsys.readouterr().out
        assert out.count("sent: email, push") == 3
        assert len(email.sent) == 1
        assert len(push.sent) == 1
        assert pinger.pings == 1
        with Store.open_readonly(settings.db_path) as store:
            (poll,) = store.recent_polls(5)
            assert poll.outcome is PollOutcome.OK
            assert store.get_state().consecutive_failures == 0
