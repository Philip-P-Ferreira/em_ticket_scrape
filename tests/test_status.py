import dataclasses
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tests.sample_env import EnvFileFactory
from ticket_poller.cli import ExitCode, main
from ticket_poller.config import Settings, load_settings
from ticket_poller.messages import PricePoint
from ticket_poller.models import Channel, NoticeKind, PollOutcome, PriceSnapshot, ResaleCapture
from ticket_poller.state import PollRecord, RuntimeState
from ticket_poller.status import canary_report, render_status
from ticket_poller.store import Store

NOW = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)  # 09:00 CST


@pytest.fixture
def settings(make_env_file: EnvFileFactory) -> Settings:
    make_env_file()
    settings = load_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings


@pytest.fixture
def store(settings: Settings) -> Iterator[Store]:
    with Store.open_writer(settings.db_path) as writer:
        yield writer


def ok_poll(started_at: datetime, baseline_cents: int) -> PollRecord:
    return PollRecord(
        started_at=started_at,
        outcome=PollOutcome.OK,
        elapsed_s=11.2,
        resale_capture=ResaleCapture.CAPTURED,
        primary_listings=5,
        resale_listings=2,
        snapshot=PriceSnapshot(baseline_cents, {2: baseline_cents, 3: None}, {2: None, 3: None}),
        matches=1,
        new_matches=1,
    )


def test_empty_state(store: Store, settings: Settings) -> None:
    report = render_status(store, settings, NOW)

    assert "Recent polls:\n  none yet" in report
    assert "Failures: none" in report
    assert "Baseline: no samples yet · warming up 0/12" in report
    assert "Resale: eligible unknown · last capture n/a" in report
    assert "Alert delivery: email never sent · push never sent" in report
    assert "Canary: last sent never · next due now" in report
    assert "Auto-stop: 2027-04-28 00:00 CDT" in report


def test_populated_state(store: Store, settings: Settings) -> None:
    for minutes, cents in [(50, 30000), (40, 32000), (30, 31001)]:
        store.record_poll(ok_poll(NOW - timedelta(minutes=minutes), cents))
    store.record_poll(
        PollRecord(started_at=NOW - timedelta(minutes=5), outcome=PollOutcome.BLOCKED, detail="403")
    )
    sent_at = NOW - timedelta(minutes=30)
    store.log_notification(NoticeKind.ALERT, Channel.EMAIL, True, "alert", sent_at)
    store.log_notification(NoticeKind.ALERT, Channel.PUSH, False, "alert", sent_at)
    store.save_state(
        RuntimeState(last_canary_date=date(2026, 11, 14), resale_eligible=True).record_failure(
            PollOutcome.BLOCKED, NOW - timedelta(minutes=5)
        )
    )

    report = render_status(store, settings, NOW)

    assert "2026-11-14 08:55 CST  blocked         403" in report
    assert "primary=5 resale=2 resale_capture=captured matches=1 new=1" in report
    assert "Failures: 1 in a row (1 blocked) since 2026-11-14 08:55 CST" in report
    assert "outage alert due 2026-11-14 09:55 CST" in report
    assert "$310.01 (n=3) · warming up 3/12" in report
    assert "Resale: eligible yes · last capture captured" in report
    assert "email ok 2026-11-14 08:30 CST · push FAILED 2026-11-14 08:30 CST" in report
    assert "Canary: last sent 2026-11-14 · next 2026-11-15 08:00 CST" in report


def test_announced_outage_and_pending_recovery(store: Store, settings: Settings) -> None:
    failing = RuntimeState().record_failure(PollOutcome.ERROR, NOW).record_outage_notice(True)
    store.save_state(failing)

    assert "outage announced" in render_status(store, settings, NOW)

    store.save_state(failing.record_success(NOW + timedelta(hours=2)))

    assert "Failures: none · recovery notice pending" in render_status(store, settings, NOW)


class TestCanaryReport:
    def test_empty_state(self, store: Store, settings: Settings) -> None:
        report = canary_report(store, settings, NOW)

        assert report.now == NOW
        assert report.total_polls == 0
        assert report.primary_now == report.resale_now == {2: None, 3: None}
        assert report.baseline is not None
        assert report.baseline.samples == 0
        assert report.resale_eligible is None

    def test_maps_stats_and_state(self, store: Store, settings: Settings) -> None:
        store.record_poll(ok_poll(NOW - timedelta(days=2), 1000))  # baseline only, not 24 h
        store.record_poll(
            dataclasses.replace(
                ok_poll(NOW - timedelta(hours=1), 34350),
                snapshot=PriceSnapshot(34350, {2: 34350, 3: 38350}, {2: 30000, 3: None}),
                primary_labels={2: "104 R", 3: "123 M"},
                resale_labels={2: "201 A"},
                browser_recycled=True,
            )
        )
        store.log_notification(NoticeKind.ALERT, Channel.PUSH, False, "alert", NOW)
        failing = RuntimeState(resale_eligible=True).record_failure(PollOutcome.ERROR, NOW)
        store.save_state(failing)

        report = canary_report(store, settings, NOW)

        assert report.outcomes[PollOutcome.OK] == 1
        assert report.last_success_at == NOW - timedelta(hours=1)
        assert (report.failing_since, report.consecutive_failures) == (NOW, 1)
        assert report.primary_now == {2: PricePoint(34350, "104 R"), 3: PricePoint(38350, "123 M")}
        assert report.resale_now == {2: PricePoint(30000, "201 A"), 3: None}
        assert report.primary_low == {2: 34350, 3: 38350}
        assert report.baseline is not None
        assert (report.baseline.median_cents, report.baseline.samples) == (Decimal(17675), 2)
        assert report.resale_eligible is True
        assert (report.resale_captured_polls, report.resale_listings_now) == (1, 2)
        assert report.channel_failures == {Channel.EMAIL: 0, Channel.PUSH: 1}
        assert report.browser_recycles == 1


class TestStatusCommand:
    def test_before_first_run(
        self, make_env_file: EnvFileFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_env_file()

        assert main(["status"]) == ExitCode.OK
        assert "no state yet" in capsys.readouterr().out

    def test_on_an_empty_database(
        self, store: Store, settings: Settings, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["status"]) == ExitCode.OK
        assert "Recent polls:\n  none yet" in capsys.readouterr().out

    def test_does_not_modify_the_database(self, store: Store, settings: Settings) -> None:
        store.record_poll(ok_poll(NOW, 30000))
        before = settings.db_path.stat().st_mtime_ns

        assert main(["status"]) == ExitCode.OK
        assert settings.db_path.stat().st_mtime_ns == before
        assert len(store.recent_polls(10)) == 1

    def test_corrupt_database_is_an_error(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings.db_path.write_bytes(b"this is not a sqlite database" * 100)

        assert main(["status"]) == ExitCode.CRASH
        assert "cannot open" in caplog.text

    def test_newer_schema_is_an_error(
        self, store: Store, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        store._conn.execute("PRAGMA user_version = 99")

        assert main(["status"]) == ExitCode.CRASH
        assert "newer than this code" in caplog.text
