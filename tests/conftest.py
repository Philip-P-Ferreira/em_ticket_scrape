"""Shared fixtures: an isolated environment and a factory for private ``.env`` files."""

import logging
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.sample_env import BASE_ENV, EnvFileFactory
from ticket_poller.config import Settings


def _env_keys() -> set[str]:
    keys = {name.upper() for name in Settings.model_fields}
    return keys | {"ENV_FILE", "JOURNAL_STREAM"}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run each test in an empty temp dir with no config from the real environment."""
    for key in _env_keys():
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def restore_process_state() -> Iterator[None]:
    """Undo global side effects of configure_logging() and secure_runtime()."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    quiet_levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    excepthook, thread_hook = sys.excepthook, threading.excepthook
    umask = os.umask(0o022)
    os.umask(umask)
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    root.setLevel(level)
    for name, quiet_level in quiet_levels.items():
        logging.getLogger(name).setLevel(quiet_level)
    logging.captureWarnings(False)
    sys.excepthook, threading.excepthook = excepthook, thread_hook
    os.umask(umask)


@pytest.fixture
def make_env_file(tmp_path: Path) -> EnvFileFactory:
    """Write a 0600 ``.env`` in the temp dir: BASE_ENV plus private state dirs plus overrides.

    Pass ``KEY=None`` to omit a key.
    """

    def make(mode: int = 0o600, **overrides: str | None) -> Path:
        values: dict[str, str | None] = {
            **BASE_ENV,
            "STATE_DIR": str(tmp_path / "state"),
            "BROWSER_PROFILE_DIR": str(tmp_path / "profile"),
            **overrides,
        }
        path = tmp_path / ".env"
        lines = [f"{key}={value}" for key, value in values.items() if value is not None]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(mode)
        return path

    return make
