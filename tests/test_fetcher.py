import json
import logging
import re
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast

import pytest
from playwright.sync_api import Response as PlaywrightResponse

from tests.builders import EVENT_API, FIXTURES, load_captured
from tests.fakes import FakeLauncher, FakeRawResponse, FakeTime, PageScript, raw
from tests.sample_env import NTFY_TOPIC, EnvFileFactory
from ticket_poller.config import Settings, load_settings
from ticket_poller.fetcher import (
    PROFILE_IN_USE_MESSAGE,
    BrowserError,
    PageEvidence,
    PlaywrightFetcher,
    classify_failure,
    classify_launch_error,
    has_display,
    is_ismds_url,
    is_offer_facets,
    parse_resale_eligibility,
    read_response,
    recycle_due,
    save_responses,
    write_debug_dump,
)
from ticket_poller.fetcher import _Response as PlaywrightAdapter
from ticket_poller.models import CapturedResponse, FetchResult, PollOutcome
from ticket_poller.parsing import parse_inventory

EVENT_ID = "2000655CAB442FB5"
OFFERADAPTER = f"https://offeradapter.ticketmaster.com/api/ismds/event/{EVENT_ID}"
PAUSED = "Your Browsing Activity Has Been Paused"
# The real Playwright error when a manual Chromium holds the profile (checked on Chromium 14x).
PROFILE_IN_USE_ERROR = (
    "BrowserType.launch_persistent_context: Opening in existing browser session. This usually "
    "means that the profile is already in use by another instance of Chromium.\n"
    "Call log:\n  - <launching> /usr/bin/chromium --user-data-dir=..."
)
ELIGIBLE_HTML = (
    '<script>{"resaleEligibilityJSON":{"isResaleEligible":false,"isPostingAllowed":false}}</script>'
)

OFFER_FACETS = load_captured("nk_facets_inventorytypes_offer")
QP2 = load_captured("nk_quickpicks_qty2")
QP3 = load_captured("nk_quickpicks_qty3")
DYNAMIC_BLOCK = CapturedResponse(
    f"{EVENT_API}/quickpicks?qty=2", 403, {"response": "dynamic_block"}
)


def block_page() -> str:
    return (FIXTURES / "block_page.html").read_text(encoding="utf-8")


def page_title(html: str) -> str:
    found = re.search(r"<title>(.*?)</title>", html)
    assert found is not None
    return found.group(1)


@pytest.fixture
def settings(make_env_file: EnvFileFactory) -> Settings:
    make_env_file()
    return load_settings()


@pytest.fixture
def time() -> FakeTime:
    return FakeTime()


def evidence(
    *responses: CapturedResponse,
    status: int | None = 200,
    title: str = "Noah Kahan Tickets",
    blocklisted: bool = False,
) -> PageEvidence:
    return PageEvidence(status, title, responses, blocklisted)


class TestUrls:
    @pytest.mark.parametrize(
        "url",
        [
            f"{EVENT_API}/quickpicks?qty=2",
            f"{EVENT_API}/facets?by=inventorytypes+offer",
            f"{EVENT_API}/offers?q=x",
            f"{OFFERADAPTER}/facets?q=and(available,%20inventorytypes:%22resale%22)",
        ],
    )
    def test_ismds_calls_on_both_hosts(self, url: str) -> None:
        assert is_ismds_url(url, EVENT_ID)

    @pytest.mark.parametrize(
        "url",
        [
            "https://services.ticketmaster.com/api/ismds/event/OTHER/quickpicks?qty=2",
            f"{EVENT_API}/quickpicksx",
            f"{EVENT_API}/seatmap",
            f"http://services.ticketmaster.com/api/ismds/event/{EVENT_ID}/facets",
            f"https://services.ticketmaster.com.evil.example/api/ismds/event/{EVENT_ID}/facets",
            "https://www.ticketmaster.com/event/2000655CAB442FB5",
        ],
    )
    def test_other_urls_are_ignored(self, url: str) -> None:
        assert not is_ismds_url(url, EVENT_ID)

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("by=inventorytypes+offer&q=available&embed=offer", True),
            ("by=inventorytypes%20offer&embed=area&embed=offer", True),
            ("by=inventorytypes+offer&q=available", False),
            ("by=shape&embed=offer", False),
        ],
    )
    def test_offer_facets(self, query: str, expected: bool) -> None:
        assert is_offer_facets(f"{EVENT_API}/facets?{query}") is expected

    def test_offer_facets_fixture(self) -> None:
        assert is_offer_facets(OFFER_FACETS.url)
        assert not is_offer_facets(QP2.url)


class TestPageState:
    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            (ELIGIBLE_HTML, False),
            ('"resaleEligibilityJSON": {"isResaleEligible": true}', True),
            (r"\"resaleEligibilityJSON\":\"{\\\"isResaleEligible\\\":true}\"", True),
            ("<html>no state</html>", None),
        ],
    )
    def test_resale_eligibility(self, html: str, expected: bool | None) -> None:
        assert parse_resale_eligibility(html) is expected

    def test_block_page_has_no_eligibility(self) -> None:
        assert parse_resale_eligibility(block_page()) is None

    @pytest.mark.parametrize(
        ("environ", "expected"),
        [({"DISPLAY": ":0"}, True), ({"WAYLAND_DISPLAY": "wayland-1"}, True), ({}, False)],
    )
    def test_has_display(self, environ: dict[str, str], expected: bool) -> None:
        assert has_display(environ) is expected

    def test_recycle_due(self) -> None:
        assert not recycle_due(6 * 3600 - 1, 6)
        assert recycle_due(6 * 3600, 6)


class TestClassifyLaunchError:
    @pytest.mark.parametrize(
        "message",
        [
            PROFILE_IN_USE_ERROR,
            "Failed to create a ProcessSingleton for your profile directory.",
            "The profile appears to be in use by another Chromium process",
            "cannot create SingletonLock: File exists",
        ],
    )
    def test_profile_in_use(self, message: str) -> None:
        assert classify_launch_error(message) == "profile_in_use"

    def test_other_errors_keep_the_first_line(self) -> None:
        message = "BrowserType.launch_persistent_context: spawn /usr/bin/chromium ENOENT\nlog"

        assert classify_launch_error(message) == (
            "launch: BrowserType.launch_persistent_context: spawn /usr/bin/chromium ENOENT"
        )


class TestClassifyFailure:
    def test_offer_facets_and_quickpicks_are_ok(self) -> None:
        assert classify_failure(evidence(OFFER_FACETS, QP2)) == (PollOutcome.OK, "")

    def test_data_wins_over_block_signals(self) -> None:
        found = evidence(OFFER_FACETS, QP2, DYNAMIC_BLOCK, status=401, blocklisted=True)

        assert classify_failure(found) == (PollOutcome.OK, "")

    def test_missing_quickpicks(self) -> None:
        found = evidence(OFFER_FACETS, DYNAMIC_BLOCK, status=403)

        assert classify_failure(found) == (PollOutcome.ERROR, "missing quickpicks")

    def test_missing_offer_facets(self) -> None:
        assert classify_failure(evidence(QP2)) == (PollOutcome.ERROR, "missing offer facets")

    def test_saved_block_page(self) -> None:
        found = evidence(status=401, title=page_title(block_page()))

        assert classify_failure(found) == (
            PollOutcome.BLOCKED,
            'blocked: document 401, title "Let\'s Get Your Identity Verified"',
        )

    def test_paused_page_with_tm_bl(self) -> None:
        found = evidence(status=403, title=PAUSED, blocklisted=True)

        outcome, detail = classify_failure(found)

        assert outcome is PollOutcome.BLOCKED
        assert detail == f"blocked: document 403, tm-bl, title '{PAUSED}'"

    def test_dynamic_block_bodies(self) -> None:
        assert classify_failure(evidence(DYNAMIC_BLOCK)) == (
            PollOutcome.BLOCKED,
            "blocked: dynamic_block",
        )

    def test_nothing_captured_is_a_timeout(self) -> None:
        assert classify_failure(evidence()) == (PollOutcome.ERROR, "timeout: no ismds responses")

    def test_only_failed_responses(self) -> None:
        failed = CapturedResponse(f"{EVENT_API}/quickpicks?qty=2", 500, None)

        assert classify_failure(evidence(failed)) == (
            PollOutcome.ERROR,
            "timeout: no ismds data (1 failed responses)",
        )


class TestReadResponse:
    def test_json_object(self) -> None:
        assert read_response(raw(QP2)) == QP2

    def test_blocked_json_body_is_kept(self) -> None:
        assert read_response(raw(DYNAMIC_BLOCK)) == DYNAMIC_BLOCK

    @pytest.mark.parametrize(
        "response",
        [
            FakeRawResponse(QP2.url, 200, {"a": 1}, {"content-type": "text/html"}),
            FakeRawResponse(QP2.url, 204, None),
            FakeRawResponse(QP2.url, 200, [1, 2]),
        ],
    )
    def test_unusable_bodies_become_none(self, response: FakeRawResponse) -> None:
        assert read_response(response).body is None


class RevalidatedResponse:
    """A Playwright response revalidated from the cache: its raw (304) headers lack content-type."""

    url = QP2.url
    status = 200
    headers: ClassVar = {"content-type": "application/json"}

    def header_value(self, name: str) -> str | None:
        return None

    def body(self) -> bytes:
        return json.dumps(QP2.body).encode()


def test_revalidated_json_falls_back_to_page_visible_headers() -> None:
    response = PlaywrightAdapter(cast(PlaywrightResponse, RevalidatedResponse()))

    assert read_response(response) == QP2


def make_fetcher(
    settings: Settings,
    launcher: FakeLauncher,
    time: FakeTime,
    stop: threading.Event | None = None,
    environ: dict[str, str] | None = None,
) -> PlaywrightFetcher:
    return PlaywrightFetcher(
        settings,
        stop if stop is not None else threading.Event(),
        launcher=launcher,
        monotonic=time,
        environ={"DISPLAY": ":0"} if environ is None else environ,
    )


def event_page(**changes: object) -> PageScript:
    """The event page: offer facets and qty-2 quickpicks on load; qty 3 via the dropdown."""
    script = PageScript(
        on_load=(
            (0.8, FakeRawResponse("https://www.ticketmaster.com/api/other", 200, {})),
            (1.2, raw(OFFER_FACETS)),
            (2.5, raw(QP2)),
        ),
        document=FakeRawResponse("https://www.ticketmaster.com/event/x", 200, None, {}),
        dropdown={3: ((0.4, raw(QP3)),)},
        html=ELIGIBLE_HTML,
    )
    for name, value in changes.items():
        setattr(script, name, value)
    return script


def urls(result: FetchResult | None) -> list[str]:
    assert result is not None
    return [response.url for response in result.responses]


class TestFetch:
    def test_captures_primary_and_qty3_via_the_dropdown(
        self, settings: Settings, time: FakeTime
    ) -> None:
        launcher = FakeLauncher(event_page(), time)

        result = make_fetcher(settings, launcher, time).fetch()

        assert result is not None
        assert result.outcome is PollOutcome.OK
        assert urls(result) == [OFFER_FACETS.url, QP2.url, QP3.url]
        assert result.resale_eligible is False
        assert result.elapsed_s == pytest.approx(2.5 + 0.4 + settings.capture_settle_seconds)
        assert launcher.tabs[0].calls == ["goto", "select 3"]
        assert not launcher.tabs[0].closed
        inventory = parse_inventory(result.responses, settings.wanted_quantities)
        assert inventory.inferred_quantities == frozenset()

    def test_later_fetches_reload_the_same_browser(
        self, settings: Settings, time: FakeTime
    ) -> None:
        launcher = FakeLauncher(event_page(), time)
        fetcher = make_fetcher(settings, launcher, time)

        fetcher.fetch()
        second = fetcher.fetch()

        assert second is not None
        assert second.outcome is PollOutcome.OK
        assert urls(second) == [OFFER_FACETS.url, QP2.url, QP3.url]
        assert [tab.calls for tab in launcher.tabs] == [["goto", "select 3", "reload", "select 3"]]

    def test_dropdown_failure_falls_back_to_max_quantity(
        self, settings: Settings, time: FakeTime, caplog: pytest.LogCaptureFixture
    ) -> None:
        launcher = FakeLauncher(event_page(dropdown={}), time)

        result = make_fetcher(settings, launcher, time).fetch()

        assert result is not None
        assert result.outcome is PollOutcome.OK
        assert urls(result) == [OFFER_FACETS.url, QP2.url]
        assert "qty 3 dropdown failed (Timeout 5000ms exceeded.); using maxQuantity" in caplog.text
        inventory = parse_inventory(result.responses, settings.wanted_quantities)
        assert inventory.inferred_quantities == frozenset({3})

    def test_dropdown_without_quickpicks_falls_back(
        self, settings: Settings, time: FakeTime, caplog: pytest.LogCaptureFixture
    ) -> None:
        launcher = FakeLauncher(event_page(dropdown={3: ()}), time)

        result = make_fetcher(settings, launcher, time).fetch()

        assert result is not None
        assert result.outcome is PollOutcome.OK
        assert "no quickpicks for qty=3 after selecting it" in caplog.text

    def test_no_dropdown_when_every_quantity_arrived(
        self, settings: Settings, time: FakeTime
    ) -> None:
        on_load = ((1.0, raw(QP2)), (1.2, raw(QP3)), (1.5, raw(OFFER_FACETS)))
        launcher = FakeLauncher(event_page(on_load=on_load), time)

        make_fetcher(settings, launcher, time).fetch()

        assert launcher.tabs[0].calls == ["goto"]

    @pytest.mark.parametrize(
        "waits_before_stop",
        [
            pytest.param(1, id="initial-load"),
            pytest.param(4, id="qty-3"),
            pytest.param(5, id="settle"),
        ],
    )
    def test_stop_mid_fetch_returns_none(
        self, settings: Settings, time: FakeTime, waits_before_stop: int
    ) -> None:
        stop = threading.Event()
        launcher = FakeLauncher(event_page(), time)
        waits = 0

        def stop_after_some_waits() -> None:
            nonlocal waits
            waits += 1
            if waits == waits_before_stop:
                stop.set()

        launcher.on_wait = stop_after_some_waits

        assert make_fetcher(settings, launcher, time, stop).fetch() is None
        assert waits == waits_before_stop
        assert not launcher.tabs[0].closed

    def test_blocked_page(self, settings: Settings, time: FakeTime) -> None:
        document = FakeRawResponse(
            "https://www.ticketmaster.com/event/x", 403, None, {"tm-bl": "1"}
        )
        script = event_page(on_load=(), document=document, html=block_page(), title=PAUSED)
        launcher = FakeLauncher(script, time)

        result = make_fetcher(settings, launcher, time).fetch()

        assert result is not None
        assert result.outcome is PollOutcome.BLOCKED
        assert result.detail == f"blocked: document 403, tm-bl, title '{PAUSED}'"
        assert result.resale_eligible is None
        assert time.now == pytest.approx(settings.capture_timeout_seconds)
        assert launcher.tabs[0].calls == ["goto"]
        assert launcher.tabs[0].closed
        (dump,) = settings.debug_dir.iterdir()
        assert "-blocked-document-403-tm-bl-title-your" in dump.name

    def test_dynamic_block_responses(self, settings: Settings, time: FakeTime) -> None:
        blocked = raw(DYNAMIC_BLOCK, **{"tm-bl": "1"})
        launcher = FakeLauncher(event_page(on_load=((1.0, blocked),)), time)

        result = make_fetcher(settings, launcher, time).fetch()

        assert result is not None
        assert (result.outcome, result.detail) == (
            PollOutcome.BLOCKED,
            "blocked: tm-bl, dynamic_block",
        )
        assert result.responses == (DYNAMIC_BLOCK,)

    def test_missing_quickpicks_writes_a_debug_dump(
        self, settings: Settings, time: FakeTime
    ) -> None:
        launcher = FakeLauncher(event_page(on_load=((1.0, raw(OFFER_FACETS)),)), time)

        result = make_fetcher(settings, launcher, time).fetch()

        assert result is not None
        assert (result.outcome, result.detail) == (PollOutcome.ERROR, "missing quickpicks")
        assert [p.name[-24:] for p in settings.debug_dir.iterdir()] == ["-missing-quickpicks.json"]

    def test_navigation_error(self, settings: Settings, time: FakeTime) -> None:
        script = event_page(navigation_error="Page.goto: net::ERR_NAME_NOT_RESOLVED\nlog")
        launcher = FakeLauncher(script, time)

        result = make_fetcher(settings, launcher, time).fetch()

        assert result is not None
        assert result.detail == "navigation: Page.goto: net::ERR_NAME_NOT_RESOLVED"
        assert launcher.tabs[0].closed

    def test_browser_dying_during_shutdown_is_a_stop(
        self, settings: Settings, time: FakeTime
    ) -> None:
        stop = threading.Event()
        launcher = FakeLauncher(event_page(), time)

        def shutdown() -> None:
            stop.set()
            raise BrowserError("Target page, context or browser has been closed")

        launcher.on_wait = shutdown

        assert make_fetcher(settings, launcher, time, stop).fetch() is None

    def test_navigation_failing_during_shutdown_is_a_stop(
        self, settings: Settings, time: FakeTime
    ) -> None:
        stop = threading.Event()
        stop.set()
        launcher = FakeLauncher(event_page(navigation_error="Page.goto: net::ERR_ABORTED"), time)

        assert make_fetcher(settings, launcher, time, stop).fetch() is None

    def test_responses_queued_between_polls_are_dropped(
        self, settings: Settings, time: FakeTime
    ) -> None:
        late = FakeRawResponse(f"{EVENT_API}/offers?q=late", 200, {})
        script = event_page()
        script.on_load = (*script.on_load, (30.0, late))
        fetcher = make_fetcher(settings, FakeLauncher(script, time), time)

        fetcher.fetch()
        time.now += 60
        second = fetcher.fetch()

        assert urls(second) == [OFFER_FACETS.url, QP2.url, QP3.url]

    def test_closed_window_is_a_browser_error(self, settings: Settings, time: FakeTime) -> None:
        closed = "Page.reload: Target page, context or browser has been closed"
        launcher = FakeLauncher(event_page(navigation_error=closed), time)

        result = make_fetcher(settings, launcher, time).fetch()

        assert result is not None
        assert (result.outcome, result.detail) == (PollOutcome.ERROR, f"browser: {closed}")

    def test_failed_fetch_relaunches_next_time(self, settings: Settings, time: FakeTime) -> None:
        launcher = FakeLauncher(event_page(on_load=()), time)
        fetcher = make_fetcher(settings, launcher, time)

        fetcher.fetch()
        launcher.script = event_page()
        result = fetcher.fetch()

        assert result is not None
        assert result.outcome is PollOutcome.OK
        assert [tab.calls[0] for tab in launcher.tabs] == ["goto", "goto"]

    def test_profile_in_use(
        self, settings: Settings, time: FakeTime, caplog: pytest.LogCaptureFixture
    ) -> None:
        launcher = FakeLauncher(event_page(), time, error=PROFILE_IN_USE_ERROR)

        result = make_fetcher(settings, launcher, time).fetch()

        assert result is not None
        assert (result.outcome, result.detail) == (PollOutcome.ERROR, "profile_in_use")
        assert PROFILE_IN_USE_MESSAGE in caplog.text

    def test_other_launch_error(self, settings: Settings, time: FakeTime) -> None:
        launcher = FakeLauncher(event_page(), time, error="spawn /usr/bin/chromium ENOENT")

        result = make_fetcher(settings, launcher, time).fetch()

        assert result is not None
        assert result.detail == "launch: spawn /usr/bin/chromium ENOENT"

    def test_no_display(self, settings: Settings, time: FakeTime) -> None:
        launcher = FakeLauncher(event_page(), time)

        result = make_fetcher(settings, launcher, time, environ={}).fetch()

        assert result is not None
        assert result.detail.startswith("no_display")
        assert launcher.tabs == []

    def test_recycles_the_browser_after_six_hours(
        self, settings: Settings, time: FakeTime, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)
        launcher = FakeLauncher(event_page(), time)
        fetcher = make_fetcher(settings, launcher, time)
        fetcher.fetch()

        time.now = 5.9 * 3600
        kept = fetcher.fetch()
        time.now = 6.0 * 3600
        recycled = fetcher.fetch()

        assert kept is not None
        assert not kept.browser_recycled
        assert recycled is not None
        assert recycled.browser_recycled
        assert recycled.outcome is PollOutcome.OK
        assert [tab.closed for tab in launcher.tabs] == [True, False]
        assert launcher.tabs[1].calls == ["goto", "select 3"]
        assert "browser recycled (age 6.0h)" in caplog.text

    def test_close_is_idempotent(self, settings: Settings, time: FakeTime) -> None:
        launcher = FakeLauncher(event_page(), time)
        fetcher = make_fetcher(settings, launcher, time)
        fetcher.fetch()

        fetcher.close()
        fetcher.close()

        assert launcher.tabs[0].closed


class TestDumps:
    def test_debug_dump_is_private_redacted_and_capped(self, settings: Settings) -> None:
        secret_url = f"{QP2.url}&apikey=PUBLICKEY&apisecret=SECRETVALUE&topic={NTFY_TOPIC}"
        response = CapturedResponse(secret_url, 200, {"big": "x" * 600_000})
        html = f"<p>{NTFY_TOPIC}</p>" + "y" * 300_000

        path = write_debug_dump(
            settings.debug_dir, "parse: bad", [response], html=html, secrets=[NTFY_TOPIC]
        )

        text = path.read_text(encoding="utf-8")
        for secret in ("PUBLICKEY", "SECRETVALUE", NTFY_TOPIC):
            assert secret not in text
        assert "apikey=***&apisecret=***" in text
        assert "body over 512,000 characters" in text
        assert "[truncated" in text
        assert len(text) < 300_000
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(settings.debug_dir.stat().st_mode) == 0o700

    def test_redaction_keeps_the_json_valid(self, tmp_path: Path) -> None:
        quoted = CapturedResponse(QP2.url, 200, {"link": 'https://x/?apikey=PUBLICKEY"&q=1'})

        dump = write_debug_dump(tmp_path / "debug", "blocked", [quoted])
        (saved,) = save_responses(tmp_path / "saved", [quoted])

        assert json.loads(dump.read_text())["responses"][0]["response"] == {
            "link": 'https://x/?apikey=***"&q=1'
        }
        assert json.loads(saved.read_text())["response"] == {"link": 'https://x/?apikey=***"&q=1'}

    def test_keeps_the_newest_twenty(self, tmp_path: Path) -> None:
        directory = tmp_path / "debug"
        start = datetime(2026, 11, 14, tzinfo=UTC)
        for minute in range(23):
            write_debug_dump(directory, "timeout", [], now=start + timedelta(minutes=minute))

        names = sorted(p.name for p in directory.iterdir())
        assert len(names) == 20
        assert names[0].startswith("20261114T000300")

    def test_saved_responses_load_as_fixtures(self, tmp_path: Path) -> None:
        keyed = CapturedResponse(f"{QP2.url}&apikey=PUBLICKEY", 200, QP2.body)

        paths = save_responses(tmp_path / "dump", [OFFER_FACETS, keyed, DYNAMIC_BLOCK])

        assert [p.name for p in paths] == [
            "01-facets-200.json",
            "02-quickpicks-200.json",
            "03-quickpicks-403.json",
        ]
        text = paths[1].read_text(encoding="utf-8")
        assert "PUBLICKEY" not in text
        assert json.loads(text).keys() == {"_source_url", "status", "response"}
        assert stat.S_IMODE((tmp_path / "dump").stat().st_mode) == 0o700

    def test_saving_into_an_existing_directory_keeps_its_mode(self, tmp_path: Path) -> None:
        directory = tmp_path / "shared"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)

        (path,) = save_responses(directory, [QP2])

        assert stat.S_IMODE(directory.stat().st_mode) == 0o755
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
