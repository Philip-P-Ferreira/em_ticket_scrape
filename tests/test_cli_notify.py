"""``test-notify`` and ``canary-now --print`` (no real delivery: the notifier is faked)."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from tests.sample_env import APP_PASSWORD, NTFY_OPS_TOPIC, NTFY_TOKEN, NTFY_TOPIC, EnvFileFactory
from ticket_poller import notify
from ticket_poller.cli import main
from ticket_poller.messages import Email, NoticeKind, Push
from ticket_poller.notify import Delivery


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


class TestCanaryNow:
    def test_print_renders_without_sending(
        self,
        make_env_file: EnvFileFactory,
        fake_notifier: FakeNotifier,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_env_file()

        assert main(["canary-now", "--print"]) == 0

        out = capsys.readouterr().out
        assert out.startswith("Subject: ticket-poller daily canary — OK\n\nPolls (24 h): 0")
        assert "Cheapest primary  qty 3: none | qty 2: none" in out
        assert fake_notifier.sent == []

    def test_sending_is_not_wired_yet(
        self, make_env_file: EnvFileFactory, fake_notifier: FakeNotifier
    ) -> None:
        make_env_file()

        assert main(["canary-now"]) == 1
        assert fake_notifier.sent == []
