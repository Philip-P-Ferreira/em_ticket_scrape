import io
import logging
import sys
import threading
import warnings

import pytest

from tests.sample_env import (
    APP_PASSWORD,
    HC_URL,
    HC_UUID,
    NTFY_OPS_TOPIC,
    NTFY_TOKEN,
    NTFY_TOPIC,
    EnvFileFactory,
)
from ticket_poller.config import load_settings
from ticket_poller.logsetup import (
    CONSOLE_FORMAT,
    JOURNAL_FORMAT,
    REDACTED,
    RedactingFormatter,
    configure_logging,
    log_format,
    redact,
)

ALL_FORMS = (
    APP_PASSWORD,
    APP_PASSWORD.replace(" ", ""),
    NTFY_TOPIC,
    NTFY_OPS_TOPIC,
    NTFY_TOKEN,
    HC_URL,
    HC_UUID,
)


@pytest.fixture
def output(make_env_file: EnvFileFactory) -> io.StringIO:
    """Logging configured exactly as the CLI does, writing to a buffer."""
    settings = load_settings(make_env_file())
    stream = io.StringIO()
    configure_logging("DEBUG", settings.secret_values(), stream=stream)
    return stream


def _assert_clean(text: str) -> None:
    for secret in ALL_FORMS:
        assert secret not in text, f"secret leaked: {secret[:3]}…"


def _fail_with_secrets() -> None:
    raise RuntimeError(f"POST https://ntfy.sh/{NTFY_TOPIC} failed; auth={APP_PASSWORD}")


class TestRedact:
    def test_replaces_every_occurrence(self) -> None:
        assert redact("a SECRET b SECRET", ["SECRET"]) == f"a {REDACTED} b {REDACTED}"

    def test_longest_secret_wins(self) -> None:
        assert redact(f"ping {HC_URL}", [HC_UUID, HC_URL]) == f"ping {REDACTED}"

    def test_ignores_empty_secrets(self) -> None:
        assert redact("unchanged", ["", "zzz"]) == "unchanged"


class TestRedactingLogs:
    def test_message_and_args_are_redacted(self, output: io.StringIO) -> None:
        log = logging.getLogger("ticket_poller.test")
        log.info("password %s / %s", APP_PASSWORD, APP_PASSWORD.replace(" ", ""))
        log.warning("topics %s %s token=%s", NTFY_TOPIC, NTFY_OPS_TOPIC, NTFY_TOKEN)
        log.error("ping failed: %s (check %s)", HC_URL, HC_UUID)

        text = output.getvalue()
        _assert_clean(text)
        assert text.count(REDACTED) == 7
        assert "password" in text

    def test_exception_message_and_traceback_are_redacted(self, output: io.StringIO) -> None:
        log = logging.getLogger("ticket_poller.test")
        try:
            _fail_with_secrets()
        except RuntimeError:
            log.exception("send failed for %s", HC_URL)

        text = output.getvalue()
        _assert_clean(text)
        assert "Traceback" in text
        assert "_fail_with_secrets" in text

    def test_chained_exception_is_redacted(self, output: io.StringIO) -> None:
        log = logging.getLogger("ticket_poller.test")
        try:
            try:
                _fail_with_secrets()
            except RuntimeError as exc:
                raise ValueError(f"wrapped {NTFY_TOKEN}") from exc
        except ValueError:
            log.exception("boom")

        _assert_clean(output.getvalue())

    def test_uncaught_exception_hook_is_redacted(self, output: io.StringIO) -> None:
        try:
            _fail_with_secrets()
        except RuntimeError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)

        text = output.getvalue()
        _assert_clean(text)
        assert "unhandled exception" in text

    def test_thread_exception_hook_is_redacted(self, output: io.StringIO) -> None:
        thread = threading.Thread(target=_fail_with_secrets, name="worker")
        thread.start()
        thread.join()

        text = output.getvalue()
        _assert_clean(text)
        assert "unhandled exception in thread worker" in text

    @pytest.mark.filterwarnings("always")
    def test_warnings_are_redacted(self, output: io.StringIO) -> None:
        warnings.warn(f"insecure {NTFY_TOPIC}", UserWarning, stacklevel=1)

        text = output.getvalue()
        _assert_clean(text)
        assert "insecure" in text


class TestConfigureLogging:
    def test_http_client_loggers_are_quiet(self, output: io.StringIO) -> None:
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING

    def test_reconfiguring_replaces_the_handler(self) -> None:
        first = configure_logging(stream=io.StringIO())
        second = configure_logging(stream=io.StringIO())

        handlers = logging.getLogger().handlers
        assert second in handlers
        assert first not in handlers

    def test_level_is_applied(self) -> None:
        configure_logging("WARNING", stream=io.StringIO())

        assert logging.getLogger().level == logging.WARNING

    def test_formatter_redacts(self) -> None:
        formatter = RedactingFormatter("%(message)s", ["hunter22"])
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "pw=hunter22", None, None)

        assert formatter.format(record) == f"pw={REDACTED}"

    @pytest.mark.parametrize(
        ("environ", "expected"),
        [({"JOURNAL_STREAM": "8:12345"}, JOURNAL_FORMAT), ({}, CONSOLE_FORMAT)],
    )
    def test_journald_aware_format(self, environ: dict[str, str], expected: str) -> None:
        assert log_format(environ) == expected
