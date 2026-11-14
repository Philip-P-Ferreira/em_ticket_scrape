"""Checks against the real system Chromium (``pytest -m live``; needs a display, no network)."""

import os
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.sample_env import EnvFileFactory
from ticket_poller.config import Settings, load_settings
from ticket_poller.fetcher import (
    PROFILE_IN_USE,
    BrowserError,
    PlaywrightFetcher,
    RawResponse,
    classify_launch_error,
    launch_chromium,
    read_response,
)
from ticket_poller.models import CapturedResponse, PollOutcome

pytestmark = pytest.mark.live

# Captured at import, before the autouse fixture clears the environment.
DESKTOP_ENV = {
    key: value
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "HOME")
    if (value := os.environ.get(key))
}


@pytest.fixture
def settings(make_env_file: EnvFileFactory, monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key, value in DESKTOP_ENV.items():
        monkeypatch.setenv(key, value)
    make_env_file()
    settings = load_settings()
    settings.browser_profile_dir.mkdir(mode=0o700, parents=True)
    return settings


@pytest.fixture
def manual_chromium(settings: Settings) -> Iterator[subprocess.Popen[bytes]]:
    """A Chromium started by hand on the poller's profile."""
    profile = settings.browser_profile_dir
    args = [f"--user-data-dir={profile}", "--ozone-platform=x11", "--no-first-run", "about:blank"]
    process = subprocess.Popen(
        [str(settings.chromium_path), *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while not (profile / "SingletonLock").is_symlink():
            assert time.monotonic() < deadline, "manual Chromium did not start"
            time.sleep(0.2)
        yield process
    finally:
        process.terminate()
        process.wait(timeout=15)


def test_profile_in_use_is_recognised(
    settings: Settings, manual_chromium: subprocess.Popen[bytes]
) -> None:
    with pytest.raises(BrowserError) as raised:
        launch_chromium(settings, lambda response: None)

    assert classify_launch_error(str(raised.value)) == PROFILE_IN_USE
    assert (settings.browser_profile_dir / "SingletonLock").is_symlink()


def test_fetcher_reports_profile_in_use(
    settings: Settings, manual_chromium: subprocess.Popen[bytes]
) -> None:
    fetcher = PlaywrightFetcher(settings, threading.Event())

    result = fetcher.fetch()

    assert result is not None
    assert (result.outcome, result.detail) == (PollOutcome.ERROR, PROFILE_IN_USE)
    assert manual_chromium.poll() is None


def test_launch_and_close(settings: Settings) -> None:
    tab = launch_chromium(settings, lambda response: None)
    try:
        assert tab.title() == ""
    finally:
        tab.close()

    assert not list(Path(settings.browser_profile_dir).glob("Singleton*"))


QTY_PAGE = (
    "data:text/html,<div style='visibility:hidden'><select id='filter-bar-quantity'>"
    "<option>2</option><option>3</option></select></div>"
    "<script>document.querySelector('select').onchange = e => document.title = e.target.value"
    "</script>"
)


def test_select_quantity_on_a_hidden_select(settings: Settings) -> None:
    tab = launch_chromium(settings, lambda response: None)
    try:
        tab.goto(QTY_PAGE, timeout_s=10)
        tab.select_quantity(3, timeout_s=2)
        title = tab.title()
        tab.goto("data:text/html,<p>no select</p>", timeout_s=10)
        with pytest.raises(BrowserError, match=r"Timeout 1000ms exceeded\. select=\[\]$"):
            tab.select_quantity(3, timeout_s=1)
    finally:
        tab.close()

    assert title == "3"


class _RevalidatingHandler(BaseHTTPRequestHandler):
    """A page whose script fetches JSON that the browser must revalidate (304) on reload."""

    LAST_MODIFIED = "Sat, 14 Nov 2026 04:00:00 GMT"
    PAGE = b"<script>fetch('/api/facets').then(r => r.json())</script>"
    JSON = b'{"facets": []}'

    def do_GET(self) -> None:
        if self.path != "/api/facets":
            self._reply(200, "text/html", self.PAGE)
        elif self.headers.get("If-Modified-Since") == self.LAST_MODIFIED:
            self._reply(304)
        else:
            self._reply(200, "application/json", self.JSON)

    def _reply(self, status: int, content_type: str | None = None, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Last-Modified", self.LAST_MODIFIED)
        if content_type:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def revalidating_site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RevalidatingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_reads_json_revalidated_from_the_browser_cache(
    settings: Settings, revalidating_site: str
) -> None:
    captured: list[CapturedResponse] = []

    def sink(response: RawResponse) -> None:
        if response.url.endswith("/api/facets"):
            captured.append(read_response(response))

    tab = launch_chromium(settings, sink)
    try:
        for _ in range(2):
            tab.goto(revalidating_site, timeout_s=10)
            assert tab.wait_for_response(lambda r: r.url.endswith("/api/facets"), timeout_s=10)
    finally:
        tab.close()

    assert [response.body for response in captured] == [{"facets": []}] * 2
