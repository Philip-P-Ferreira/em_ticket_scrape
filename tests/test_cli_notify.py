"""``test-notify`` and ``canary-now`` (no real delivery: the notifier is faked)."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.sample_env import APP_PASSWORD, NTFY_OPS_TOPIC, NTFY_TOKEN, NTFY_TOPIC, EnvFileFactory
from ticket_poller import notify
from ticket_poller.cli import main
from ticket_poller.config import Settings, load_settings
from ticket_poller.messages import Email, Push
from ticket_poller.models import NoticeKind, PollOutcome, PriceSnapshot, ResaleCapture
from ticket_poller.notify import Delivery
from ticket_poller.state import PollRecord
from ticket_poller.store import Store


@dataclass
class FakeNotifier:
    email_ok: bool = True
    push_ok: bool = True
    sent: list[tuple[NoticeKind, Email | None, Push | None, Sequence[str]]] = field(
        default_factory=list
    )

    def send(
        self,
        kind: NoticeKind,
        *,
        to: Sequence[str],
        email: Email | None = None,
        push: Push | None = None,
    ) -> Delivery:
        self.sent.append((kind, email, push, to))
        return Delivery(
            email=None if email is None else self.email_ok,
            push=None if push is None else self.push_ok,
        )


@pytest.fixture
def fake_notifier(monkeypatch: pytest.MonkeyPatch) -> FakeNotifier:
    fake = FakeNotifier()

    def build(*args: Any, **kwargs: Any) -> FakeNotifier:
        return fake

    monkeypatch.setattr(notify, "build_notifier", build)
    return fake


class TestTestNotify:
    def test_sends_to_admin_only_and_reports_each_channel(
        self,
        make_env_file: EnvFileFactory,
        fake_notifier: FakeNotifier,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_env_file(ALERT_RECIPIENTS="someone@example.com,philip@example.com")

        assert main(["test-notify"]) == 0

        ((kind, email, push, to),) = fake_notifier.sent
        assert kind is NoticeKind.TEST
        assert to == ["philip@ferreira-family.com"]
        assert email is not None
        assert push is not None
        for secret in (APP_PASSWORD, NTFY_TOPIC, NTFY_OPS_TOPIC, NTFY_TOKEN):
            assert secret not in email.body
        assert capsys.readouterr().out.splitlines()[-2:] == ["email: ok", "push: ok"]

    def test_skipped_channel_and_failure_exit_code(
        self,
        make_env_file: EnvFileFactory,
        fake_notifier: FakeNotifier,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_env_file()
        fake_notifier.email_ok = False

        assert main(["test-notify", "--no-push"]) == 1

        assert fake_notifier.sent[0][2] is None
        assert capsys.readouterr().out.splitlines()[-2:] == ["email: FAILED", "push: skipped"]


def make_state(settings: Settings) -> None:
    """A state DB with one successful poll in the last 24 h and a browser recycle."""
    settings.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with Store.open_writer(settings.db_path) as store:
        store.record_poll(
            PollRecord(
                started_at=datetime.now(UTC) - timedelta(minutes=10),
                outcome=PollOutcome.OK,
                resale_capture=ResaleCapture.NOT_ELIGIBLE,
                primary_listings=6,
                resale_listings=0,
                snapshot=PriceSnapshot(34350, {2: 34350, 3: 38350}, {2: None, 3: None}),
                primary_labels={2: "104 R", 3: "123 M"},
                browser_recycled=True,
            )
        )


class TestCanaryNow:
    @pytest.mark.parametrize("argv", [["canary-now", "--print"], ["canary-now"]])
    def test_no_state_yet(
        self,
        argv: list[str],
        make_env_file: EnvFileFactory,
        fake_notifier: FakeNotifier,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_env_file()

        assert main(argv) == 0

        assert "no state yet" in capsys.readouterr().out
        assert fake_notifier.sent == []

    def test_print_renders_from_the_store_without_sending(
        self,
        make_env_file: EnvFileFactory,
        fake_notifier: FakeNotifier,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_env_file()
        settings = load_settings()
        make_state(settings)
        before = settings.db_path.stat().st_mtime_ns

        assert main(["canary-now", "--print"]) == 0

        out = capsys.readouterr().out
        assert out.startswith("Subject: ticket-poller daily canary — OK\n\nPolls (24 h): 1")
        assert "Cheapest primary  qty 3: $383.50 (123 M)" in out
        assert "qty 2: $343.50 (104 R)" in out
        assert "Browser recycles: 1" in out
        assert fake_notifier.sent == []
        assert settings.db_path.stat().st_mtime_ns == before

    def test_sends_to_admin_email_only_and_keeps_the_canary_date(
        self,
        make_env_file: EnvFileFactory,
        fake_notifier: FakeNotifier,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_env_file(ALERT_RECIPIENTS="someone@example.com,philip@example.com")
        settings = load_settings()
        make_state(settings)

        assert main(["canary-now"]) == 0

        ((kind, email, push, to),) = fake_notifier.sent
        assert kind is NoticeKind.CANARY
        assert to == [settings.admin_email]
        assert push is None
        assert email is not None
        assert email.subject == "ticket-poller daily canary — OK"
        assert "Cheapest primary  qty 3: $383.50 (123 M)" in email.body
        assert capsys.readouterr().out.splitlines()[-2:] == ["email: ok", "push: skipped"]
        with Store.open_readonly(settings.db_path) as store:
            assert store.get_state().last_canary_date is None

    def test_send_failure_exit_code(
        self, make_env_file: EnvFileFactory, fake_notifier: FakeNotifier
    ) -> None:
        make_env_file()
        make_state(load_settings())
        fake_notifier.email_ok = False

        assert main(["canary-now"]) == 1
        assert len(fake_notifier.sent) == 1


class TestNotifyFailure:
    def test_pushes_to_ops_only_even_with_a_corrupt_database(
        self,
        make_env_file: EnvFileFactory,
        fake_notifier: FakeNotifier,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_env_file()
        settings = load_settings()
        settings.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        settings.db_path.write_bytes(b"not a database")

        assert main(["notify-failure", "ticket-poller.service"]) == 0

        ((kind, email, push, _to),) = fake_notifier.sent
        assert (kind, email) == (NoticeKind.SERVICE_FAILED, None)
        assert push is not None
        assert push.title == (
            "Ticket poller service FAILED — check journalctl --user -u ticket-poller"
        )
        assert push.message == "systemd unit ticket-poller.service entered the failed state."
        assert capsys.readouterr().out.splitlines()[-2:] == ["email: skipped", "push: ok"]

    def test_unit_is_optional_and_a_failed_push_exits_1(
        self, make_env_file: EnvFileFactory, fake_notifier: FakeNotifier
    ) -> None:
        make_env_file()
        fake_notifier.push_ok = False

        assert main(["notify-failure"]) == 1

        ((_kind, _email, push, _to),) = fake_notifier.sent
        assert push is not None
        assert push.message == push.title
