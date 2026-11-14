"""Delivery: stop-aware retries, SMTP/ntfy/healthchecks senders and the ``Notifier`` facade.

Senders make one attempt and raise on failure; ``Notifier`` adds the per-channel retry
policy (§9.3), never raises, and reports a ``Delivery`` per channel. Every log line goes
through ``_describe``, which names the error without echoing URLs, and through the
sender's secret list, so no password, topic, token or ping URL is ever logged.
"""

import logging
import random
import smtplib
import ssl
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Final, Protocol

import httpx
from pydantic import SecretStr

from ticket_poller.config import Settings
from ticket_poller.logsetup import redact
from ticket_poller.messages import Email, Push
from ticket_poller.models import Channel, NoticeKind
from ticket_poller.schedule import exponential_backoff

log = logging.getLogger(__name__)

SENDER_NAME: Final = "ticket-poller"
HTTP_TIMEOUT_SECONDS: Final = 10.0

type Wait = Callable[[float], bool]
"""Sleep up to N seconds; returns True if stop was requested (``threading.Event.wait``)."""
type AuditHook = Callable[[NoticeKind, Channel, bool, str], None]
"""Called after each channel attempt with (kind, channel, ok, subject)."""


class Stopped(Exception):  # noqa: N818 - a signal, not an error
    """Stop was requested while waiting to retry; no further attempt was made."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int
    first_delay: float = 2.0
    max_delay: float = 30.0


SMTP_RETRY: Final = RetryPolicy(attempts=3)
NTFY_RETRY: Final = RetryPolicy(attempts=3)
HEALTHCHECK_RETRY: Final = RetryPolicy(attempts=2)


def backoff_delay(attempt: int, policy: RetryPolicy, rng: random.Random) -> float:
    """Delay after failed attempt ``attempt`` (1-based): exponential, ±20 % jitter, capped."""
    base = exponential_backoff(attempt, policy.first_delay, policy.max_delay)
    return min(policy.max_delay, base * rng.uniform(0.8, 1.2))


def retry[T](
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    retryable: Callable[[Exception], bool],
    wait: Wait,
    rng: random.Random | None = None,
    on_retry: Callable[[int, Exception, float], None] | None = None,
) -> T:
    """Call ``fn`` up to ``policy.attempts`` times with backoff between attempts.

    Re-raises the last error, or a non-retryable one at once. Backoff sleeps via
    ``wait``; if it reports a stop request, raises ``Stopped`` (chained to the last
    error) and makes no further attempt.
    """
    rng = rng if rng is not None else random.Random()
    for attempt in range(1, policy.attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= policy.attempts or not retryable(exc):
                raise
            delay = backoff_delay(attempt, policy, rng)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            if wait(delay):
                raise Stopped from exc
    raise AssertionError("unreachable: RetryPolicy.attempts must be >= 1")


def _describe(exc: BaseException) -> str:
    """A short, URL-free description of a delivery error."""
    match exc:
        case httpx.HTTPStatusError():
            return f"HTTP {exc.response.status_code}"
        case smtplib.SMTPResponseException():
            return f"{type(exc).__name__} {exc.smtp_code}"
        case httpx.HTTPError() | OSError():
            # httpx transport errors and socket errors carry no URL, but stay terse anyway.
            detail = str(exc).splitlines()[0] if str(exc) else ""
            return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        case ValueError():  # raised locally with secret-free messages (e.g. "no recipients")
            return f"{type(exc).__name__}: {exc}"
        case _:
            return type(exc).__name__


# --- email ------------------------------------------------------------------------------------


class EmailSender(Protocol):
    def send(self, email: Email, *, to: Sequence[str]) -> None:
        """Deliver one message (one attempt); raise on failure."""
        ...


class SmtpFactory(Protocol):
    def __call__(self, host: str, port: int, *, timeout: float) -> smtplib.SMTP: ...


def smtp_retryable(exc: Exception) -> bool:
    """Retry transient SMTP/network failures; never auth errors or permanent (5xx) replies."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return False
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(exc, smtplib.SMTPException):  # recipients refused, STARTTLS unsupported, …
        return False
    return isinstance(exc, OSError)  # timeouts, refused connections, TLS errors


class SmtpEmailSender:
    """Plain-text email over SMTP with STARTTLS (Gmail: smtp.gmail.com:587)."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: SecretStr,
        timeout: float,
        smtp_factory: SmtpFactory = smtplib.SMTP,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._timeout = timeout
        self._smtp_factory = smtp_factory

    def build_message(self, email: Email, to: Sequence[str]) -> EmailMessage:
        message = EmailMessage()
        message["From"] = formataddr((SENDER_NAME, self._user))
        message["To"] = ", ".join(to)
        message["Subject"] = email.subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=self._user.rpartition("@")[2] or None)
        message.set_content(email.body)
        return message

    def send(self, email: Email, *, to: Sequence[str]) -> None:
        if not to:
            raise ValueError("no recipients")
        message = self.build_message(email, to)
        # Gmail shows app passwords in groups of four; the spaces are not part of it.
        password = "".join(self._password.get_secret_value().split())
        # The timeout bounds every socket operation, which bounds shutdown (§8).
        with self._smtp_factory(self._host, self._port, timeout=self._timeout) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self._user, password)
            smtp.send_message(message)


# --- push -------------------------------------------------------------------------------------


class PushSender(Protocol):
    def send(self, push: Push) -> None:
        """Publish one push (one attempt); raise on failure."""
        ...


def http_retryable(exc: Exception) -> bool:
    """Retry network errors, 429 and 5xx; other HTTP errors (4xx) are permanent."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == httpx.codes.TOO_MANY_REQUESTS or status >= 500
    return isinstance(exc, httpx.TransportError)


class NtfyPushSender:
    """ntfy JSON publishing: the secret topic travels in the request body, never the URL."""

    def __init__(
        self,
        server: str,
        topic: SecretStr,
        token: SecretStr | None,
        client: httpx.Client,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._url = server.rstrip("/") + "/"
        self._topic = topic
        self._token = token
        self._client = client
        self._timeout = timeout

    def payload(self, push: Push) -> dict[str, object]:
        body: dict[str, object] = {
            "topic": self._topic.get_secret_value(),
            "title": push.title,
            "message": push.message,
            "priority": push.priority,
        }
        if push.tags:
            body["tags"] = list(push.tags)
        if push.click:
            body["click"] = push.click
        return body

    def send(self, push: Push) -> None:
        headers = {}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        response = self._client.post(
            self._url, json=self.payload(push), headers=headers, timeout=self._timeout
        )
        response.raise_for_status()


# --- healthchecks -----------------------------------------------------------------------------


class HealthcheckPinger:
    """Pings healthchecks.io after each successful poll; a no-op if no URL is configured."""

    def __init__(
        self,
        url: SecretStr | None,
        client: httpx.Client,
        *,
        wait: Wait,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        secrets: Iterable[str] = (),
        rng: random.Random | None = None,
    ) -> None:
        self._url = url
        self._client = client
        self._wait = wait
        self._timeout = timeout
        self._secrets = tuple(secrets)
        self._rng = rng

    def ping(self) -> bool | None:
        """True if pinged, False if it failed (logged), None if no URL is set. Never raises."""
        if self._url is None:
            return None
        url = self._url.get_secret_value()

        def attempt() -> None:
            self._client.get(url, timeout=self._timeout).raise_for_status()

        try:
            retry(
                attempt,
                policy=HEALTHCHECK_RETRY,
                retryable=http_retryable,
                wait=self._wait,
                rng=self._rng,
            )
        except Exception as exc:
            log.warning("healthchecks ping failed: %s", redact(_describe(exc), self._secrets))
            return False
        return True


# --- facade -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Delivery:
    """Per-channel result: True delivered, False failed, None not attempted."""

    email: bool | None = None
    push: bool | None = None
    email_auth_failed: bool = False
    """The SMTP server rejected the login: retrying will not help."""

    def result(self, channel: Channel) -> bool | None:
        return self.email if channel is Channel.EMAIL else self.push

    def ok(self, channel: Channel) -> bool:
        return self.result(channel) is True

    @property
    def any_ok(self) -> bool:
        return self.email is True or self.push is True

    @property
    def any_failed(self) -> bool:
        return self.email is False or self.push is False


class Notifier:
    """Sends a notification on each requested channel. Never raises.

    Alerts go to the alert topic and every other kind to the ops topic. No new channel
    attempt starts once ``stop`` is set; an attempt already in flight is bounded by the
    sender's timeout.
    """

    def __init__(
        self,
        email_sender: EmailSender,
        alert_push: PushSender,
        ops_push: PushSender,
        *,
        stop: threading.Event,
        wait: Wait | None = None,
        secrets: Iterable[str] = (),
        audit: AuditHook | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._email_sender = email_sender
        self._alert_push = alert_push
        self._ops_push = ops_push
        self._stop = stop
        self._wait = wait if wait is not None else stop.wait
        self._secrets = tuple(secrets)
        self._audit = audit
        self._rng = rng

    def send(
        self,
        kind: NoticeKind,
        *,
        to: Sequence[str],
        email: Email | None = None,
        push: Push | None = None,
    ) -> Delivery:
        email_ok = push_ok = None
        email_errors: list[Exception] = []
        if email is not None and not to:
            log.error("%s email not sent: no recipients", kind.value)
            email_ok = False
            self._record(kind, Channel.EMAIL, email_ok, email.subject)
        elif email is not None:
            email_ok = self._attempt(
                kind,
                Channel.EMAIL,
                email.subject,
                lambda: self._email_sender.send(email, to=to),
                SMTP_RETRY,
                smtp_retryable,
                email_errors,
            )
        if push is not None:
            sender = self._alert_push if kind is NoticeKind.ALERT else self._ops_push
            push_ok = self._attempt(
                kind,
                Channel.PUSH,
                push.title,
                lambda: sender.send(push),
                NTFY_RETRY,
                http_retryable,
            )
        auth_failed = any(isinstance(e, smtplib.SMTPAuthenticationError) for e in email_errors)
        return Delivery(email=email_ok, push=push_ok, email_auth_failed=auth_failed)

    def _attempt(
        self,
        kind: NoticeKind,
        channel: Channel,
        subject: str,
        send: Callable[[], None],
        policy: RetryPolicy,
        retryable: Callable[[Exception], bool],
        errors: list[Exception] | None = None,
    ) -> bool | None:
        """Send with retries; the final exception of a failed attempt goes to ``errors``."""
        if self._stop.is_set():
            log.info("%s %s not sent: stopping", kind.value, channel.value)
            return None

        def on_retry(attempt: int, exc: Exception, delay: float) -> None:
            log.warning(
                "%s %s attempt %d failed (%s); retrying in %.0fs",
                kind.value,
                channel.value,
                attempt,
                self._safe(exc),
                delay,
            )

        try:
            retry(
                send,
                policy=policy,
                retryable=retryable,
                wait=self._wait,
                rng=self._rng,
                on_retry=on_retry,
            )
        except Stopped as exc:
            log.warning(
                "%s %s not delivered: stop requested after %s",
                kind.value,
                channel.value,
                self._safe(exc.__cause__ or exc),
            )
            ok = False
        except Exception as exc:
            log.error("%s %s failed: %s", kind.value, channel.value, self._safe(exc))
            if isinstance(exc, smtplib.SMTPAuthenticationError):
                log.error("SMTP login was rejected: check SENDER_EMAIL and SENDER_APP_PASSWORD")
            if errors is not None:
                errors.append(exc)
            ok = False
        else:
            log.info("%s %s sent: %s", kind.value, channel.value, subject)
            ok = True
        self._record(kind, channel, ok, subject)
        return ok

    def _record(self, kind: NoticeKind, channel: Channel, ok: bool, subject: str) -> None:
        if self._audit is None:
            return
        try:
            self._audit(kind, channel, ok, subject)
        except Exception as exc:
            log.error(
                "could not record the %s %s attempt: %s",
                kind.value,
                channel.value,
                self._safe(exc),
            )

    def _safe(self, exc: BaseException) -> str:
        return redact(_describe(exc), self._secrets)


def build_notifier(
    settings: Settings,
    client: httpx.Client,
    *,
    stop: threading.Event,
    audit: AuditHook | None = None,
) -> Notifier:
    """The production ``Notifier``: Gmail SMTP plus the alert and ops ntfy topics."""
    email = SmtpEmailSender(
        settings.smtp_host,
        settings.smtp_port,
        settings.sender_email,
        settings.sender_app_password,
        settings.smtp_timeout_seconds,
    )
    alert_push = NtfyPushSender(
        settings.ntfy_server, settings.ntfy_topic, settings.ntfy_token, client
    )
    ops_push = NtfyPushSender(settings.ntfy_server, settings.ops_topic, settings.ntfy_token, client)
    return Notifier(
        email, alert_push, ops_push, stop=stop, secrets=settings.secret_values(), audit=audit
    )


def build_pinger(
    settings: Settings, client: httpx.Client, *, stop: threading.Event
) -> HealthcheckPinger:
    """The healthchecks.io pinger (a no-op when ``HEALTHCHECKS_PING_URL`` is unset)."""
    return HealthcheckPinger(
        settings.healthchecks_ping_url,
        client,
        wait=stop.wait,
        secrets=settings.secret_values(),
    )
