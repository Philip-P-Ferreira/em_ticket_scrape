from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest

from tests.sample_env import EnvFileFactory
from ticket_poller.cli import ExitCode, main
from ticket_poller.config import Settings, load_settings
from ticket_poller.models import Channel, PollOutcome, PriceSnapshot, ResaleCapture
from ticket_poller.status import render_status
from ticket_poller.store import ALERT_KIND, PollRecord, RuntimeState, Store

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
    store.log_notification(ALERT_KIND, Channel.EMAIL, True, "alert", NOW - timedelta(minutes=30))
    store.log_notification(ALERT_KIND, Channel.PUSH, False, "alert", NOW - timedelta(minutes=30))
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

    def test_newer_schema_is_an_error(
        self, store: Store, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        store._conn.execute("PRAGMA user_version = 99")

        assert main(["status"]) == ExitCode.CRASH
        assert "newer than this code" in caplog.text
