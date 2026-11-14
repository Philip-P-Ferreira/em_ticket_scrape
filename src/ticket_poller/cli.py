"""Command-line entry point and composition root for ``ticket-poller``."""

import argparse
import fcntl
import logging
import os
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Final

import httpx

from ticket_poller import __version__, messages, notify
from ticket_poller.config import ConfigError, Settings, load_settings, secure_runtime
from ticket_poller.logsetup import configure_logging
from ticket_poller.models import Channel

log = logging.getLogger(__name__)

LOCK_HELD_MESSAGE: Final = "another poller is running (systemctl --user stop ticket-poller)"

type Handler = Callable[[argparse.Namespace, Settings], int]


class ExitCode(IntEnum):
    """Process exit codes; systemd restarts only on CRASH and LOCK_HELD."""

    OK = 0  # event over, signal, or command succeeded
    CRASH = 1
    LOCK_HELD = 75  # EX_TEMPFAIL
    CONFIG = 78  # EX_CONFIG: invalid settings or unsafe .env


class LockHeldError(Exception):
    """Another process holds the instance lock."""


@contextmanager
def instance_lock(path: Path) -> Iterator[None]:
    """Hold a non-blocking exclusive ``flock`` on ``path`` for the duration of the block."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockHeldError(str(path)) from None
        yield
    finally:
        os.close(fd)  # also releases the lock


def _not_implemented(command: str, task: str) -> int:
    print(f"ticket-poller {command}: not implemented yet ({task})", file=sys.stderr)
    return ExitCode.CRASH


def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    return _not_implemented("run", "T6")


def _cmd_poll_once(args: argparse.Namespace, settings: Settings) -> int:
    return _not_implemented("poll-once", "T5")


def _send_to_admin(
    settings: Settings,
    kind: messages.NoticeKind,
    email: messages.Email | None,
    push: messages.Push | None,
) -> notify.Delivery:
    """Send to ADMIN_EMAIL and the ops topic, then print the per-channel result."""
    with httpx.Client() as client:
        notifier = notify.build_notifier(settings, client, stop=threading.Event())
        delivery = notifier.send(kind, email=email, push=push, to=[settings.admin_email])
    for channel in Channel:
        result = delivery.result(channel)
        print(f"{channel.value}: {'skipped' if result is None else 'ok' if result else 'FAILED'}")
    return delivery


def _cmd_test_notify(args: argparse.Namespace, settings: Settings) -> int:
    ctx = messages.MessageContext.from_settings(settings)
    notice = messages.selftest_notice(
        settings_summary=settings.summary(), ctx=ctx, now=datetime.now(UTC)
    )
    email = None if args.no_email else notice.email
    push = None if args.no_push else notice.push
    delivery = _send_to_admin(settings, notice.kind, email, push)
    return ExitCode.CRASH if delivery.any_failed else ExitCode.OK


def _cmd_canary_now(args: argparse.Namespace, settings: Settings) -> int:
    if not args.print:
        return _not_implemented("canary-now (sending; use --print)", "T3/T6")
    # TODO(T3/T6): build the report from the store's poll statistics.
    print("note: poll statistics are not wired yet (store: T3); report is empty", file=sys.stderr)
    report = messages.CanaryReport(now=datetime.now(UTC))
    email = messages.canary(report, messages.MessageContext.from_settings(settings))
    print(f"Subject: {email.subject}\n\n{email.body}", end="")
    return ExitCode.OK


def _cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    return _not_implemented("status", "T3")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket-poller",
        description="Poll one Ticketmaster event and alert on matching tickets.",
        epilog="Configuration comes from the environment and .env (or $ENV_FILE).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, handler: Handler, help_text: str, *, lock: bool) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text)
        sub.set_defaults(handler=handler, needs_lock=lock)
        return sub

    add("run", _cmd_run, "run the polling service loop", lock=True)

    poll_once = add(
        "poll-once",
        _cmd_poll_once,
        "fetch once and print candidates and matches (no notifications or writes by default)",
        lock=True,
    )
    poll_once.add_argument(
        "--notify", action="store_true", help="behave exactly like one service poll"
    )
    poll_once.add_argument(
        "--save", type=Path, metavar="DIR", help="write the captured response bodies to DIR"
    )

    test_notify = add(
        "test-notify",
        _cmd_test_notify,
        "send a test notification to the admin email and ops topic",
        lock=False,
    )
    test_notify.add_argument("--no-email", action="store_true", help="skip the email channel")
    test_notify.add_argument("--no-push", action="store_true", help="skip the push channel")

    canary_now = add(
        "canary-now", _cmd_canary_now, "render the daily canary and send it to admin", lock=False
    )
    canary_now.add_argument("--print", action="store_true", help="print instead of sending")

    add("status", _cmd_status, "show polling, outage, baseline and delivery state", lock=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()  # bootstrap; no secret is known (or logged) before settings load
    try:
        settings = load_settings()
        configure_logging(settings.log_level, settings.secret_values())
        secure_runtime(settings)
    except ConfigError as exc:
        log.error("%s", exc)
        return ExitCode.CONFIG

    handler: Handler = args.handler
    if not args.needs_lock:
        return handler(args, settings)
    try:
        with instance_lock(settings.lock_path):
            return handler(args, settings)
    except LockHeldError:
        print(LOCK_HELD_MESSAGE, file=sys.stderr)
        return ExitCode.LOCK_HELD
