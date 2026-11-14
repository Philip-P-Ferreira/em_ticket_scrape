"""Logging to stdout (journald-aware) with secret redaction on every formatted record."""

import logging
import os
import sys
import threading
from collections.abc import Iterable, Mapping
from types import TracebackType
from typing import Final, TextIO

REDACTED: Final = "***"
JOURNAL_FORMAT: Final = "%(levelname)s %(name)s: %(message)s"
CONSOLE_FORMAT: Final = "%(asctime)s " + JOURNAL_FORMAT
QUIET_LOGGERS: Final = ("httpx", "httpcore")

log = logging.getLogger("ticket_poller")


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every occurrence of each secret in ``text`` with ``***``.

    Longer secrets are replaced first, so a URL is masked whole before its path segment.
    """
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


class RedactingFormatter(logging.Formatter):
    """Formats a record normally, then redacts secrets from the complete output.

    Redacting the fully formatted text also covers arguments, exception messages and
    tracebacks (``exc_text``) and stack info.
    """

    def __init__(self, fmt: str, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt)
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


def log_format(environ: Mapping[str, str] | None = None) -> str:
    """Use the bare format under journald (it adds timestamps), else prefix a timestamp."""
    env = os.environ if environ is None else environ
    return JOURNAL_FORMAT if env.get("JOURNAL_STREAM") else CONSOLE_FORMAT


def configure_logging(
    level: str | int = logging.INFO,
    secrets: Iterable[str] = (),
    *,
    stream: TextIO | None = None,
) -> logging.Handler:
    """Install the single redacting stdout handler on the root logger.

    Safe to call again (e.g. once before settings load, then with the secrets): the
    previously installed handler is replaced. Also routes ``warnings`` and uncaught
    exceptions (main thread and other threads) through the same handler.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler.formatter, RedactingFormatter):
            root.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(RedactingFormatter(log_format(), secrets))
    root.addHandler(handler)
    root.setLevel(level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.captureWarnings(True)
    sys.excepthook = _log_uncaught
    threading.excepthook = _log_uncaught_in_thread
    return handler


def _log_uncaught(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    log.critical("unhandled exception", exc_info=(exc_type, exc, tb))


def _log_uncaught_in_thread(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is None or issubclass(args.exc_type, SystemExit):
        return
    name = args.thread.name if args.thread is not None else "?"
    log.critical(
        "unhandled exception in thread %s",
        name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
