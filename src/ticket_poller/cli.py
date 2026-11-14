"""Command-line entry point and composition root for ``ticket-poller``."""

import argparse
import fcntl
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Final

import httpx

from ticket_poller import __version__, dumps, fetcher, messages, notify, poller
from ticket_poller.config import (
    ConfigError,
    Settings,
    check_env_file,
    env_file_path,
    load_settings,
    read_ops_push_config,
    secure_runtime,
)
from ticket_poller.logsetup import configure_logging
from ticket_poller.models import Channel, NoticeKind
from ticket_poller.report import render_report
from ticket_poller.schedule import SystemClock
from ticket_poller.status import canary_report, render_status
from ticket_poller.store import NoStateError, Store, StoreError
from ticket_poller.watchdog import Watchdog

log = logging.getLogger(__name__)

LOCK_HELD_MESSAGE: Final = "another poller is running (systemctl --user stop ticket-poller)"

type Handler = Callable[[argparse.Namespace, Settings], int]


class ExitCode(IntEnum):
    """Process exit codes; systemd restarts only on CRASH and LOCK_HELD."""

    OK = 0  # event over, signal, or command succeeded
    CRASH = 1
    LOCK_HELD = 75  # EX_TEMPFAIL
    CONFIG = 78  # EX_CONFIG: invalid settings, unsafe .env, or an unusable state database


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


def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    """The service loop; exits 0 on the auto-stop time or a signal (§2)."""
    log.info("starting: %s", settings.summary())
    try:
        store = Store.open_writer(settings.db_path)
    except StoreError as exc:
        # A restart would fail the same way, so exit with the code systemd won't restart on.
        log.error("state database unusable, not restarting: %s", exc)
        return ExitCode.CONFIG
    stop = threading.Event()
    watchdog = Watchdog(settings.watchdog_timeout_sec)
    watchdog.start()
    with closing(watchdog), ExitStack() as stack:
        service = _build_poller(stack, settings, store, stop, live=True, watchdog=watchdog)
        reason = service.run()
        watchdog.busy("shutdown")  # the stack closes the browser next
    log.info("exited: %s", reason.value)
    return ExitCode.OK


def _build_poller(
    stack: ExitStack,
    settings: Settings,
    store: Store,
    stop: threading.Event,
    *,
    live: bool,
    watchdog: Watchdog | None = None,
) -> poller.Poller:
    """Wire the poller; ``stack`` closes the store, browser and HTTP client in reverse order.

    Only a live poller records notification attempts (a dry run sends nothing anyway).
    """
    stack.enter_context(store)
    stack.enter_context(_stop_on_signals(stop))
    clock = SystemClock(stop)
    client = stack.enter_context(httpx.Client())
    audit = poller.audit_to(store, clock) if live else None
    return poller.Poller(
        settings,
        stack.enter_context(closing(fetcher.build_fetcher(settings, stop))),
        store,
        notify.build_notifier(settings, client, stop=stop, audit=audit),
        notify.build_pinger(settings, client, stop=stop),
        clock,
        stop,
        watchdog=watchdog,
    )


@contextmanager
def _stop_on_signals(stop: threading.Event) -> Iterator[None]:
    """Turn SIGINT/SIGTERM into ``stop`` so a fetch ends early and the browser closes cleanly."""
    previous = {
        signum: signal.signal(signum, lambda *_: stop.set())
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _open_store_if_any(settings: Settings) -> Store:
    try:
        return Store.open_readonly(settings.db_path)
    except NoStateError:
        return Store.open_empty()


def _cmd_poll_once(args: argparse.Namespace, settings: Settings) -> int:
    """One real fetch. By default a dry run against the read-only store: nothing is sent or
    saved. ``--notify`` uses the writer and behaves exactly like one service poll."""
    try:
        store = Store.open_writer(settings.db_path) if args.notify else _open_store_if_any(settings)
    except StoreError as exc:
        log.error("%s", exc)
        return ExitCode.CRASH

    stop = threading.Event()
    with ExitStack() as stack:
        service = _build_poller(stack, settings, store, stop, live=args.notify)
        report = service.poll_once(dry_run=not args.notify)
    if report is None:
        print("poll-once: stopped")
        return ExitCode.OK

    if args.save is not None:
        try:
            saved = dumps.save_responses(
                args.save, report.fetch.responses, settings.secret_values()
            )
        except (ConfigError, OSError) as exc:
            log.error("cannot save responses: %s", exc)
            return ExitCode.CRASH
        print(f"saved {len(saved)} responses to {args.save}")
    print(render_report(report, settings))
    return ExitCode.OK if report.succeeded else ExitCode.CRASH


def _send_to_admin(
    settings: Settings,
    kind: NoticeKind,
    email: messages.Email | None,
    push: messages.Push | None,
) -> notify.Delivery:
    """Send to ADMIN_EMAIL and the ops topic, then print the per-channel result."""
    with httpx.Client() as client:
        notifier = notify.build_notifier(settings, client, stop=threading.Event())
        delivery = notifier.send(kind, email=email, push=push, to=[settings.admin_email])
    _print_delivery(delivery)
    return delivery


def _print_delivery(delivery: notify.Delivery) -> None:
    for channel in Channel:
        result = delivery.result(channel)
        print(f"{channel.value}: {'skipped' if result is None else 'ok' if result else 'FAILED'}")


def _cmd_test_notify(args: argparse.Namespace, settings: Settings) -> int:
    ctx = messages.MessageContext.from_settings(settings)
    notice = messages.selftest_notice(
        settings_summary=settings.summary(), ctx=ctx, now=datetime.now(UTC)
    )
    email = None if args.no_email else notice.email
    push = None if args.no_push else notice.push
    delivery = _send_to_admin(settings, notice.kind, email, push)
    return ExitCode.CRASH if delivery.any_failed else ExitCode.OK


def _cmd_notify_failure(args: argparse.Namespace) -> int:
    """Ops push only, for systemd ``OnFailure=``. Best effort and tolerant: it reads just the
    ntfy settings (no full validation, no permission refusal, no state database, no email), so
    it still reports a service that exited 78 on bad config or an unsafe ``.env``."""
    path = env_file_path()
    config = read_ops_push_config(path)
    configure_logging(logging.INFO, config.secret_values())
    if config.topic is None:
        log.error("cannot push: neither NTFY_OPS_TOPIC nor NTFY_TOPIC is set")
        return ExitCode.CRASH
    push = messages.service_failed(unit=args.unit, config_problem=_config_problem(path))
    with httpx.Client() as client:
        notifier = notify.build_ops_push_notifier(config, client, stop=threading.Event())
        delivery = notifier.send(NoticeKind.SERVICE_FAILED, push=push, to=())
    _print_delivery(delivery)
    return ExitCode.OK if delivery.ok(Channel.PUSH) else ExitCode.CRASH


def _config_problem(path: Path) -> str | None:
    """Why ``run`` would refuse to start (exit 78), in words safe to push; None if nothing."""
    try:
        check_env_file(path)
    except (ConfigError, OSError) as exc:
        log.warning("%s", exc)
        return f"{path.name} permissions are unsafe (chmod 600 {path.name})"
    try:
        load_settings()
    except ConfigError as exc:
        log.warning("%s", exc)
        return "the configuration is invalid"
    return None


def _cmd_canary_now(args: argparse.Namespace, settings: Settings) -> int:
    """Render the canary from the read-only store; send it to ADMIN_EMAIL unless --print.

    Never sets ``last_canary_date`` (§12): the scheduled canary still goes out.
    """
    try:
        with Store.open_readonly(settings.db_path) as store:
            report = canary_report(store, settings, datetime.now(UTC))
    except NoStateError as exc:
        print(exc)
        return ExitCode.OK
    except StoreError as exc:
        log.error("%s", exc)
        return ExitCode.CRASH
    email = messages.canary(report, messages.MessageContext.from_settings(settings))
    if args.print:
        print(f"Subject: {email.subject}\n\n{email.body}", end="")
        return ExitCode.OK
    delivery = _send_to_admin(settings, NoticeKind.CANARY, email, None)
    return ExitCode.CRASH if delivery.any_failed else ExitCode.OK


def _cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    try:
        with Store.open_readonly(settings.db_path) as store:
            print(render_status(store, settings, datetime.now(UTC)))
    except NoStateError as exc:
        print(exc)
    except StoreError as exc:
        log.error("%s", exc)
        return ExitCode.CRASH
    return ExitCode.OK


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

    # Dispatched before settings load (see main), so it has no Handler.
    failure_help = "push 'service FAILED' to the ops topic (run by ticket-poller-failure.service)"
    notify_failure = commands.add_parser(
        "notify-failure", help=failure_help, description=failure_help
    )
    notify_failure.set_defaults(handler=None, needs_lock=False)
    notify_failure.add_argument("unit", nargs="?", help="the failed systemd unit, for the text")

    add("status", _cmd_status, "show polling, outage, baseline and delivery state", lock=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()  # bootstrap; no secret is known (or logged) before settings load
    if args.command == "notify-failure":
        return _cmd_notify_failure(args)
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
