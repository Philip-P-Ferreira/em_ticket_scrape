import os
import stat
from pathlib import Path

import pytest

from tests.sample_env import APP_PASSWORD, NTFY_TOPIC, EnvFileFactory
from ticket_poller.cli import (
    LOCK_HELD_MESSAGE,
    ExitCode,
    LockHeldError,
    build_parser,
    instance_lock,
    main,
)

COMMANDS = ("run", "poll-once", "test-notify", "canary-now", "status")


class TestParser:
    def test_help_lists_every_command(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exited:
            build_parser().parse_args(["--help"])

        assert exited.value.code == 0
        help_text = capsys.readouterr().out
        for command in COMMANDS:
            assert command in help_text

    def test_command_is_required(self) -> None:
        with pytest.raises(SystemExit) as exited:
            build_parser().parse_args([])

        assert exited.value.code == 2

    def test_subcommand_options(self) -> None:
        parser = build_parser()

        poll = parser.parse_args(["poll-once", "--notify", "--save", "dumps"])
        notify = parser.parse_args(["test-notify", "--no-email", "--no-push"])
        canary = parser.parse_args(["canary-now", "--print"])

        assert (poll.notify, poll.save, poll.needs_lock) == (True, Path("dumps"), True)
        assert (notify.no_email, notify.no_push, notify.needs_lock) == (True, True, False)
        assert canary.print is True
        assert parser.parse_args(["run"]).needs_lock is True
        assert parser.parse_args(["status"]).needs_lock is False


class TestMain:
    def test_unsafe_env_file_exits_78(
        self, make_env_file: EnvFileFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_env_file(mode=0o644)

        assert main(["status"]) == 78
        assert "chmod 600" in capsys.readouterr().out

    def test_invalid_config_exits_78_without_leaking(
        self, make_env_file: EnvFileFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_env_file(PREFERRED_QUANTITIES="4")

        assert main(["status"]) == ExitCode.CONFIG
        out = capsys.readouterr().out
        assert "PREFERRED_QUANTITIES" in out
        assert NTFY_TOPIC not in out
        assert APP_PASSWORD not in out

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-0000 file")
    def test_unreadable_env_file_exits_78(
        self, make_env_file: EnvFileFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_env_file(mode=0o000)

        assert main(["status"]) == ExitCode.CONFIG
        assert "cannot read env file" in capsys.readouterr().out

    def test_state_dir_that_is_a_file_exits_78(
        self, make_env_file: EnvFileFactory, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "a-file").touch()
        make_env_file(STATE_DIR=str(tmp_path / "a-file"))

        assert main(["status"]) == ExitCode.CONFIG
        assert "not a directory" in capsys.readouterr().out

    def test_stub_command_prepares_private_state(
        self, make_env_file: EnvFileFactory, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_env_file()

        assert main(["run"]) == ExitCode.CRASH
        assert "not implemented yet" in capsys.readouterr().err
        for name in ("state", "state/debug", "profile"):
            assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o700

    @pytest.mark.parametrize("command", ["run", "poll-once"])
    def test_lock_held_exits_75(
        self,
        make_env_file: EnvFileFactory,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        command: str,
    ) -> None:
        make_env_file()
        state_dir = tmp_path / "state"
        state_dir.mkdir(mode=0o700)

        with instance_lock(state_dir / "poller.lock"):
            code = main([command])

        assert code == 75
        assert LOCK_HELD_MESSAGE in capsys.readouterr().err

    def test_lock_free_command_runs(
        self, make_env_file: EnvFileFactory, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_env_file()

        assert main(["run"]) == ExitCode.CRASH
        assert "run: not implemented yet" in capsys.readouterr().err


class TestInstanceLock:
    def test_second_holder_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "poller.lock"

        with instance_lock(path), pytest.raises(LockHeldError), instance_lock(path):
            pass

    def test_lock_is_released_on_exit(self, tmp_path: Path) -> None:
        path = tmp_path / "poller.lock"

        with instance_lock(path):
            pass
        with instance_lock(path):
            pass

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
