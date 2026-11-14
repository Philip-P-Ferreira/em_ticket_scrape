"""Delivery (§9.3): stop-aware retry, SMTP/ntfy/healthchecks senders and the Notifier.

No test touches the network: SMTP is a fake ``smtplib.SMTP`` subclass and HTTP goes
through ``httpx.MockTransport``.
"""

import json
import logging
import os
import random
import signal
import smtplib
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from email.message import EmailMessage, Message
from types import FrameType
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from tests.sample_env import (
    APP_PASSWORD,
    HC_URL,
    HC_UUID,
    NTFY_OPS_TOPIC,
    NTFY_TOKEN,
    NTFY_TOPIC,
    SENDER_EMAIL,
    EnvFileFactory,
)
from ticket_poller.config import Settings, load_settings
from ticket_poller.messages import Email, NoticeKind, Push
from ticket_poller.models import Channel
from ticket_poller.notify import (
    Delivery,
    HealthcheckPinger,
    Notifier,
    NtfyPushSender,
    RetryPolicy,
    SmtpEmailSender,
    Stopped,
    backoff_delay,
    build_notifier,
    build_pinger,
    retry,
)

SECRETS = (APP_PASSWORD, APP_PASSWORD.replace(" ", ""), NTFY_TOPIC, NTFY_OPS_TOPIC, NTFY_TOKEN)
SECRETS_AND_HC = (*SECRETS, HC_URL, HC_UUID)
EMAIL = Email("Noah Kahan: pair available (2 only) — $195.00/ticket", "Body line\n")
PUSH = Push("Noah Kahan: pair at $195 (2 only)", "123/M $195 x2", 3, ("tickets",), "https://x.y/")
RECIPIENTS = ("philip@example.com",)


def assert_no_secrets(text: str, secrets: tuple[str, ...] = SECRETS_AND_HC) -> None:
    leaked = [secret for secret in secrets if secret in text]
    assert not leaked, f"secrets leaked into the logs: {len(leaked)}"


@pytest.fixture
def notify_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger="ticket_poller")
    return caplog


class RecordingWait:
    """A ``Wait`` that records requested delays without sleeping; returns ``stop_after``."""

    def __init__(self, stop_after: int | None = None) -> None:
        self.delays: list[float] = []
        self._stop_after = stop_after

    def __call__(self, seconds: float) -> bool:
        self.delays.append(seconds)
        return self._stop_after is not None and len(self.delays) >= self._stop_after


# --- fakes: SMTP ------------------------------------------------------------------------------


@dataclass
class FakeSmtpServer:
    """Scripted SMTP endpoint: ``errors`` are raised by successive connections (None = ok)."""

    errors: list[tuple[str, BaseException] | None] = field(default_factory=list)
    connections: list[tuple[str, int, float]] = field(default_factory=list)
    tls: list[ssl.SSLContext | None] = field(default_factory=list)
    logins: list[tuple[str, str]] = field(default_factory=list)
    sent: list[Message] = field(default_factory=list)
    closed: int = 0
    connect_delay: float = 0.0

    def factory(self, host: str, port: int, *, timeout: float) -> smtplib.SMTP:
        self.connections.append((host, port, timeout))
        step = self.errors.pop(0) if self.errors else None
        if self.connect_delay:
            time.sleep(self.connect_delay)  # like a connect blocked until the socket timeout
        if step is not None and step[0] == "connect":
            raise step[1]
        return FakeSMTP(self, step)


class FakeSMTP(smtplib.SMTP):
    def __init__(self, server: FakeSmtpServer, step: tuple[str, BaseException] | None) -> None:
        super().__init__()  # no host: does not connect
        self._server = server
        self._step = step

    def _fail(self, stage: str) -> None:
        if self._step is not None and self._step[0] == stage:
            raise self._step[1]

    def starttls(self, *, context: ssl.SSLContext | None = None) -> tuple[int, bytes]:
        self._server.tls.append(context)
        self._fail("starttls")
        return 220, b"ready"

    def login(
        self, user: str, password: str, *, initial_response_ok: bool = True
    ) -> tuple[int, bytes]:
        self._server.logins.append((user, password))
        self._fail("login")
        return 235, b"ok"

    def send_message(self, msg: Message, *args: Any, **kwargs: Any) -> dict[str, tuple[int, bytes]]:
        self._fail("send")
        self._server.sent.append(msg)
        return {}

    def __exit__(self, *args: object) -> None:
        self._server.closed += 1


def smtp_sender(server: FakeSmtpServer, timeout: float = 20.0) -> SmtpEmailSender:
    return SmtpEmailSender(
        "smtp.gmail.com",
        587,
        SENDER_EMAIL,
        SecretStr(APP_PASSWORD),
        timeout,
        smtp_factory=server.factory,
    )


# --- fakes: HTTP ------------------------------------------------------------------------------


class FakeHttp:
    """``httpx.MockTransport`` handler replaying scripted responses (or raising errors)."""

    def __init__(self, *script: int | Exception) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.script.pop(0) if self.script else 200
        if isinstance(step, Exception):
            raise step
        return httpx.Response(step, json={"id": "abc"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


@pytest.fixture
def http() -> Iterator[Callable[..., tuple[FakeHttp, httpx.Client]]]:
    clients: list[httpx.Client] = []

    def make(*script: int | Exception) -> tuple[FakeHttp, httpx.Client]:
        fake = FakeHttp(*script)
        clients.append(fake.client())
        return fake, clients[-1]

    yield make
    for client in clients:
        client.close()


type HttpFactory = Callable[..., tuple[FakeHttp, httpx.Client]]


def ntfy_sender(client: httpx.Client, topic: str = NTFY_TOPIC) -> NtfyPushSender:
    return NtfyPushSender("https://ntfy.sh", SecretStr(topic), SecretStr(NTFY_TOKEN), client)


# --- retry ------------------------------------------------------------------------------------


class Flaky:
    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "done"


POLICY = RetryPolicy(attempts=3, first_delay=2.0, max_delay=30.0)


class TestRetry:
    def test_backoff_is_exponential_jittered_and_capped(self) -> None:
        rng = random.Random(1)

        first = [backoff_delay(1, POLICY, rng) for _ in range(50)]
        second = [backoff_delay(2, POLICY, rng) for _ in range(50)]

        assert all(1.6 <= d <= 2.4 for d in first)
        assert all(3.2 <= d <= 4.8 for d in second)
        assert backoff_delay(10, POLICY, rng) <= 30.0

    def test_retries_then_succeeds(self) -> None:
        fn, wait = Flaky(OSError("a"), OSError("b")), RecordingWait()

        result = retry(fn, policy=POLICY, retryable=lambda _: True, wait=wait)

        assert (result, fn.calls, len(wait.delays)) == ("done", 3, 2)

    def test_reraises_the_last_error_after_the_last_attempt(self) -> None:
        fn = Flaky(OSError("1"), OSError("2"), OSError("3"))

        with pytest.raises(OSError, match="3"):
            retry(fn, policy=POLICY, retryable=lambda _: True, wait=RecordingWait())

        assert fn.calls == 3

    def test_non_retryable_error_is_raised_at_once(self) -> None:
        fn, wait = Flaky(ValueError("permanent")), RecordingWait()

        with pytest.raises(ValueError, match="permanent"):
            retry(fn, policy=POLICY, retryable=lambda e: isinstance(e, OSError), wait=wait)

        assert (fn.calls, wait.delays) == (1, [])

    def test_stop_during_backoff_raises_stopped_without_another_attempt(self) -> None:
        error = OSError("down")
        fn = Flaky(error)

        with pytest.raises(Stopped) as stopped:
            retry(fn, policy=POLICY, retryable=lambda _: True, wait=RecordingWait(stop_after=1))

        assert fn.calls == 1
        assert stopped.value.__cause__ is error


# --- SMTP -------------------------------------------------------------------------------------


class TestSmtpEmailSender:
    def test_sends_plain_text_over_starttls(self) -> None:
        server = FakeSmtpServer()

        smtp_sender(server).send(EMAIL, to=["a@example.com", "b@example.com"])

        assert server.connections == [("smtp.gmail.com", 587, 20.0)]
        assert isinstance(server.tls[0], ssl.SSLContext)
        assert server.logins == [(SENDER_EMAIL, APP_PASSWORD.replace(" ", ""))]
        message = server.sent[0]
        assert isinstance(message, EmailMessage)
        assert message["From"] == f"ticket-poller <{SENDER_EMAIL}>"
        assert message["To"] == "a@example.com, b@example.com"
        assert message["Subject"] == EMAIL.subject
        assert message["Message-ID"].endswith("@example.com>")
        assert message["Date"]
        assert message.get_content_type() == "text/plain"
        assert message.get_content() == "Body line\n"
        assert server.closed == 1

    def test_needs_recipients(self) -> None:
        with pytest.raises(ValueError, match="no recipients"):
            smtp_sender(FakeSmtpServer()).send(EMAIL, to=[])


def make_notifier(
    *,
    smtp: FakeSmtpServer | None = None,
    client: httpx.Client | None = None,
    stop: threading.Event | None = None,
    wait: RecordingWait | None = None,
    **kwargs: Any,
) -> Notifier:
    stop = stop or threading.Event()
    client = client or httpx.Client(transport=httpx.MockTransport(FakeHttp()))
    return Notifier(
        smtp_sender(smtp or FakeSmtpServer()),
        ntfy_sender(client, NTFY_TOPIC),
        ntfy_sender(client, NTFY_OPS_TOPIC),
        stop=stop,
        wait=wait or RecordingWait(),
        secrets=SECRETS_AND_HC,
        rng=random.Random(0),
        **kwargs,
    )


class TestNotifierEmail:
    def test_retries_a_dropped_connection(self) -> None:
        server = FakeSmtpServer(errors=[("send", smtplib.SMTPServerDisconnected("dropped"))])
        wait = RecordingWait()

        delivery = make_notifier(smtp=server, wait=wait).send(
            NoticeKind.ALERT, email=EMAIL, to=RECIPIENTS
        )

        assert delivery == Delivery(email=True, push=None)
        assert len(server.connections) == 2
        assert len(server.sent) == 1
        assert len(wait.delays) == 1
        assert 1.6 <= wait.delays[0] <= 2.4

    @pytest.mark.parametrize(
        ("error", "attempts"),
        [
            (("connect", TimeoutError("timed out")), 3),
            (("starttls", ConnectionResetError("reset")), 3),
            (("send", smtplib.SMTPDataError(451, b"try again later")), 3),
            (("send", smtplib.SMTPDataError(550, b"rejected")), 1),
            (("send", smtplib.SMTPRecipientsRefused({})), 1),
        ],
    )
    def test_retry_policy(self, error: tuple[str, BaseException], attempts: int) -> None:
        server = FakeSmtpServer(errors=[error] * 3)

        delivery = make_notifier(smtp=server).send(NoticeKind.ALERT, email=EMAIL, to=RECIPIENTS)

        assert delivery.email is False
        assert len(server.connections) == attempts

    def test_auth_error_is_not_retried_and_names_the_settings(
        self, notify_logs: pytest.LogCaptureFixture
    ) -> None:
        rejected = smtplib.SMTPAuthenticationError(
            535, f"Username and Password not accepted {APP_PASSWORD}".encode()
        )
        server = FakeSmtpServer(errors=[("login", rejected)])

        delivery = make_notifier(smtp=server).send(NoticeKind.ALERT, email=EMAIL, to=RECIPIENTS)

        assert delivery.email is False
        assert len(server.connections) == 1
        assert "SMTPAuthenticationError 535" in notify_logs.text
        assert "check SENDER_EMAIL and SENDER_APP_PASSWORD" in notify_logs.text
        assert_no_secrets(notify_logs.text)

    def test_no_recipients_fails_without_attempting(
        self, notify_logs: pytest.LogCaptureFixture
    ) -> None:
        server = FakeSmtpServer()
        recorded: list[bool] = []
        notifier = make_notifier(smtp=server, audit=lambda *args: recorded.append(args[2]))

        delivery = notifier.send(NoticeKind.ALERT, email=EMAIL, to=[])

        assert delivery == Delivery(email=False, push=None)
        assert server.connections == []
        assert recorded == [False]
        assert "alert email not sent: no recipients" in notify_logs.text

    def test_local_value_errors_are_described(self, notify_logs: pytest.LogCaptureFixture) -> None:
        email = Exploding(ValueError("bad header"))
        notifier = Notifier(email, email, email, stop=threading.Event(), wait=RecordingWait())

        assert notifier.send(NoticeKind.ALERT, email=EMAIL, to=RECIPIENTS).email is False
        assert "ValueError: bad header" in notify_logs.text


class TestNotifierPush:
    def test_ntfy_json_publish_keeps_topic_out_of_the_url(self, http: HttpFactory) -> None:
        fake, client = http()

        delivery = make_notifier(client=client).send(NoticeKind.ALERT, push=PUSH, to=())

        assert delivery == Delivery(email=None, push=True)
        (request,) = fake.requests
        assert (request.method, str(request.url)) == ("POST", "https://ntfy.sh/")
        assert request.headers["Authorization"] == f"Bearer {NTFY_TOKEN}"
        assert fake.bodies() == [
            {
                "topic": NTFY_TOPIC,
                "title": PUSH.title,
                "message": PUSH.message,
                "priority": 3,
                "tags": ["tickets"],
                "click": "https://x.y/",
            }
        ]

    def test_without_token_there_is_no_auth_header(self, http: HttpFactory) -> None:
        fake, client = http()
        sender = NtfyPushSender("https://ntfy.example/", SecretStr(NTFY_TOPIC), None, client)

        sender.send(Push("t", "m", 4))

        assert "Authorization" not in fake.requests[0].headers
        assert str(fake.requests[0].url) == "https://ntfy.example/"
        assert fake.bodies()[0] == {
            "topic": NTFY_TOPIC,
            "title": "t",
            "message": "m",
            "priority": 4,
        }

    @pytest.mark.parametrize(
        ("kind", "topic"),
        [
            (NoticeKind.ALERT, NTFY_TOPIC),
            (NoticeKind.OUTAGE, NTFY_OPS_TOPIC),
            (NoticeKind.TEST, NTFY_OPS_TOPIC),
        ],
    )
    def test_alerts_use_the_alert_topic_and_the_rest_the_ops_topic(
        self, http: HttpFactory, kind: NoticeKind, topic: str
    ) -> None:
        fake, client = http()

        make_notifier(client=client).send(kind, push=PUSH, to=())

        assert fake.bodies()[0]["topic"] == topic

    @pytest.mark.parametrize(
        ("script", "ok", "requests"),
        [
            ((503, 200), True, 2),
            ((429, 429, 200), True, 3),
            ((httpx.ConnectError("refused"), 200), True, 2),
            ((500, 502, 503), False, 3),
            ((400,), False, 1),
            ((403,), False, 1),
        ],
    )
    def test_retry_policy(
        self, http: HttpFactory, script: tuple[int | Exception, ...], ok: bool, requests: int
    ) -> None:
        fake, client = http(*script)

        delivery = make_notifier(client=client).send(NoticeKind.ALERT, push=PUSH, to=())

        assert delivery.push is ok
        assert len(fake.requests) == requests

    def test_failure_logs_carry_no_topic_or_token(
        self, http: HttpFactory, notify_logs: pytest.LogCaptureFixture
    ) -> None:
        error = httpx.ConnectError(f"cannot reach https://ntfy.sh/{NTFY_TOPIC}?auth={NTFY_TOKEN}")
        _, client = http(error, error, 404)

        delivery = make_notifier(client=client).send(NoticeKind.ALERT, push=PUSH, to=())

        assert delivery.push is False
        assert "HTTP 404" in notify_logs.text
        assert "ConnectError" in notify_logs.text
        assert_no_secrets(notify_logs.text)


class TestHealthcheckPinger:
    def test_unset_url_is_a_no_op(self, http: HttpFactory) -> None:
        fake, client = http()

        assert HealthcheckPinger(None, client, wait=RecordingWait()).ping() is None
        assert fake.requests == []

    def test_pings_the_url(self, http: HttpFactory) -> None:
        fake, client = http()

        assert HealthcheckPinger(SecretStr(HC_URL), client, wait=RecordingWait()).ping() is True
        assert [(r.method, str(r.url)) for r in fake.requests] == [("GET", HC_URL)]

    def test_retries_once_then_reports_failure_without_the_url(
        self, http: HttpFactory, notify_logs: pytest.LogCaptureFixture
    ) -> None:
        fake, client = http(httpx.ConnectError(f"cannot reach {HC_URL}"), 503)
        pinger = HealthcheckPinger(
            SecretStr(HC_URL), client, wait=RecordingWait(), secrets=SECRETS_AND_HC
        )

        assert pinger.ping() is False
        assert len(fake.requests) == 2
        assert "healthchecks ping failed: HTTP 503" in notify_logs.text
        assert_no_secrets(notify_logs.text)

    def test_never_raises(self, http: HttpFactory) -> None:
        _, client = http(RuntimeError(f"bug {HC_URL}"))
        pinger = HealthcheckPinger(SecretStr(HC_URL), client, wait=RecordingWait())

        assert pinger.ping() is False


# --- Notifier: per-channel results, never raises, stop awareness ------------------------------


class Exploding:
    """A sender that fails with an unexpected error whose message contains secrets."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error or RuntimeError(f"unexpected {APP_PASSWORD} {NTFY_TOPIC} {HC_URL}")

    def send(self, *args: object, **kwargs: object) -> None:
        self.calls += 1
        raise self.error


class TestNotifier:
    def test_reports_each_channel(self, http: HttpFactory) -> None:
        _, client = http()
        server = FakeSmtpServer(errors=[("login", smtplib.SMTPAuthenticationError(535, b"no"))])

        delivery = make_notifier(smtp=server, client=client).send(
            NoticeKind.ALERT, email=EMAIL, push=PUSH, to=RECIPIENTS
        )

        assert delivery == Delivery(email=False, push=True)
        assert delivery.result(Channel.EMAIL) is False
        assert delivery.ok(Channel.PUSH)
        assert (delivery.any_ok, delivery.any_failed) == (True, True)
        assert Delivery().result(Channel.PUSH) is None
        assert not Delivery().any_ok
        assert not Delivery().any_failed

    def test_never_raises_and_logs_no_secrets(self, notify_logs: pytest.LogCaptureFixture) -> None:
        email, push = Exploding(), Exploding()
        notifier = Notifier(
            email, push, push, stop=threading.Event(), wait=RecordingWait(), secrets=SECRETS_AND_HC
        )

        delivery = notifier.send(NoticeKind.ALERT, email=EMAIL, push=PUSH, to=RECIPIENTS)

        assert delivery == Delivery(email=False, push=False)
        assert (email.calls, push.calls) == (1, 1)  # unexpected errors are not retried
        assert "RuntimeError" in notify_logs.text
        assert "Traceback" not in notify_logs.text
        assert_no_secrets(notify_logs.text)

    def test_audit_hook_records_each_attempt_and_its_errors_are_contained(
        self, http: HttpFactory, notify_logs: pytest.LogCaptureFixture
    ) -> None:
        _, client = http(400)
        calls: list[tuple[NoticeKind, Channel, bool, str]] = []

        def audit(kind: NoticeKind, channel: Channel, ok: bool, subject: str) -> None:
            calls.append((kind, channel, ok, subject))
            raise OSError(f"database is locked {APP_PASSWORD}")

        delivery = make_notifier(client=client, audit=audit).send(
            NoticeKind.ALERT, email=EMAIL, push=PUSH, to=RECIPIENTS
        )

        assert delivery == Delivery(email=True, push=False)
        assert calls == [
            (NoticeKind.ALERT, Channel.EMAIL, True, EMAIL.subject),
            (NoticeKind.ALERT, Channel.PUSH, False, PUSH.title),
        ]
        assert "could not record the alert email attempt" in notify_logs.text
        assert_no_secrets(notify_logs.text)

    def test_nothing_is_attempted_once_stopping(self, http: HttpFactory) -> None:
        fake, client = http()
        server, stop = FakeSmtpServer(), threading.Event()
        stop.set()

        delivery = make_notifier(smtp=server, client=client, stop=stop).send(
            NoticeKind.ALERT, email=EMAIL, push=PUSH, to=RECIPIENTS
        )

        assert delivery == Delivery(email=None, push=None)
        assert (server.connections, fake.requests) == ([], [])

    def test_stop_during_backoff_ends_the_channel_and_skips_the_rest(
        self, http: HttpFactory
    ) -> None:
        fake, client = http()
        stop = threading.Event()
        server = FakeSmtpServer(errors=[("connect", TimeoutError("timed out"))] * 3)

        def wait(seconds: float) -> bool:
            stop.set()  # SIGTERM arrives while backing off
            return True

        notifier = Notifier(
            smtp_sender(server),
            ntfy_sender(client),
            ntfy_sender(client),
            stop=stop,
            wait=wait,
        )

        delivery = notifier.send(NoticeKind.ALERT, email=EMAIL, push=PUSH, to=RECIPIENTS)

        assert delivery == Delivery(email=False, push=None)
        assert len(server.connections) == 1
        assert fake.requests == []

    def test_sigterm_during_an_smtp_timeout_returns_promptly(
        self, http: HttpFactory, notify_logs: pytest.LogCaptureFixture
    ) -> None:
        """The in-flight attempt runs to its timeout; no backoff sleep or retry follows."""
        smtp_timeout = 0.4
        fake, client = http()
        stop = threading.Event()
        server = FakeSmtpServer(
            errors=[("connect", TimeoutError("timed out"))] * 3, connect_delay=smtp_timeout
        )
        notifier = Notifier(
            smtp_sender(server, timeout=smtp_timeout),
            ntfy_sender(client),
            ntfy_sender(client),
            stop=stop,  # the real stop_event.wait: the first backoff would be ~2 s
        )

        def on_sigterm(signum: int, frame: FrameType | None) -> None:
            stop.set()

        previous = signal.signal(signal.SIGTERM, on_sigterm)
        timer = threading.Timer(0.1, os.kill, (os.getpid(), signal.SIGTERM))
        try:
            started = time.monotonic()
            timer.start()
            delivery = notifier.send(NoticeKind.ALERT, email=EMAIL, push=PUSH, to=RECIPIENTS)
            elapsed = time.monotonic() - started
        finally:
            timer.cancel()
            signal.signal(signal.SIGTERM, previous)

        assert stop.is_set()
        assert delivery == Delivery(email=False, push=None)
        assert len(server.connections) == 1
        assert fake.requests == []
        assert elapsed < smtp_timeout + 0.5  # well under the 1.6 s minimum first backoff
        assert "stop requested" in notify_logs.text


class TestFactories:
    @pytest.fixture
    def settings(self, make_env_file: EnvFileFactory) -> Settings:
        return load_settings(make_env_file())

    def test_build_notifier_routes_topics_and_redacts(
        self,
        settings: Settings,
        http: HttpFactory,
        notify_logs: pytest.LogCaptureFixture,
    ) -> None:
        fake, client = http(200, 400)
        notifier = build_notifier(settings, client, stop=threading.Event())

        alert = notifier.send(NoticeKind.ALERT, push=PUSH, to=())
        ops = notifier.send(NoticeKind.OUTAGE, push=PUSH, to=())

        assert (alert.push, ops.push) == (True, False)
        assert [body["topic"] for body in fake.bodies()] == [NTFY_TOPIC, NTFY_OPS_TOPIC]
        assert fake.requests[0].headers["Authorization"] == f"Bearer {NTFY_TOKEN}"
        assert_no_secrets(notify_logs.text)

    def test_ops_topic_defaults_to_the_alert_topic(
        self, make_env_file: EnvFileFactory, http: HttpFactory
    ) -> None:
        settings = load_settings(make_env_file(NTFY_OPS_TOPIC=None))
        fake, client = http()

        build_notifier(settings, client, stop=threading.Event()).send(
            NoticeKind.CANARY, push=PUSH, to=()
        )

        assert fake.bodies()[0]["topic"] == NTFY_TOPIC

    def test_build_pinger(
        self, make_env_file: EnvFileFactory, http: HttpFactory, settings: Settings
    ) -> None:
        fake, client = http()

        assert build_pinger(settings, client, stop=threading.Event()).ping() is True
        assert str(fake.requests[0].url) == HC_URL

        unset = load_settings(make_env_file(HEALTHCHECKS_PING_URL=None))
        assert build_pinger(unset, client, stop=threading.Event()).ping() is None
