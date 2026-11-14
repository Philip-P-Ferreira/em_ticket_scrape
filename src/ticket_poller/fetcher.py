"""Fetch the event page in a headed, persistent Chromium and capture its ismds JSON (§5).

Only ``launch_chromium``/``PlaywrightTab`` touch Playwright; ``PlaywrightFetcher`` drives the
small ``BrowserTab`` seam, and the ``classify_*``/``parse_*``/``is_*`` helpers are pure.
"""

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, Playwright, sync_playwright
from playwright.sync_api import Response as PlaywrightResponse
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ticket_poller.config import Settings
from ticket_poller.dumps import try_write_dump
from ticket_poller.models import CapturedResponse, FetchResult, PollOutcome
from ticket_poller.parsing import quickpicks_qty

log = logging.getLogger(__name__)

NAVIGATION_TIMEOUT_S: Final = 20.0
QTY_CAPTURE_TIMEOUT_S: Final = 5.0
WAIT_SLICE_S: Final = 1.0
QTY_SELECTOR: Final = "#filter-bar-quantity"

BLOCK_TITLES: Final = ("Browsing Activity Has Been Paused", "Identity Verified")
BLOCKED_DOCUMENT_STATUSES: Final = frozenset({401, 403})
PROFILE_IN_USE: Final = "profile_in_use"
PROFILE_IN_USE_MESSAGE: Final = (
    "Chromium profile is in use by another Chromium (manual session?). "
    "Close it; the poller retries with backoff."
)
_PROFILE_IN_USE_RE: Final = re.compile(
    r"ProcessSingleton|profile appears to be in use|SingletonLock"
    r"|Opening in existing browser session|profile is already in use",
    re.IGNORECASE,
)
_CLOSED_RE: Final = re.compile(r"has been closed|browser closed|crash", re.IGNORECASE)
_ELIGIBILITY_RE: Final = re.compile(
    r"resaleEligibilityJSON.{0,400}?isResaleEligible\W{0,8}(true|false)", re.DOTALL
)


class BrowserError(Exception):
    """A Playwright failure, translated so the fetcher does not depend on Playwright types."""


class LaunchError(Exception):
    """The browser could not be started; the message is the poll's ERROR detail."""


# --- the browser seam ---------------------------------------------------------------------


class RawResponse(Protocol):
    @property
    def url(self) -> str: ...

    @property
    def status(self) -> int: ...

    def header_value(self, name: str) -> str | None: ...

    def body(self) -> bytes:
        """The raw body; raises ``BrowserError`` if it is unavailable."""
        ...


type ResponseSink = Callable[[RawResponse], None]
type ResponsePredicate = Callable[[RawResponse], bool]


class BrowserTab(Protocol):
    """The page operations the fetcher needs. Every method raises ``BrowserError`` on failure."""

    def goto(self, url: str, timeout_s: float) -> RawResponse | None: ...

    def reload(self, timeout_s: float) -> RawResponse | None: ...

    def wait_for_response(self, predicate: ResponsePredicate, timeout_s: float) -> bool:
        """Wait for one matching response; False on timeout."""
        ...

    def select_quantity(self, qty: int, timeout_s: float) -> None: ...

    def content(self) -> str: ...

    def title(self) -> str: ...

    def close(self) -> None: ...


type Launcher = Callable[[Settings, ResponseSink], BrowserTab]


class Fetcher(Protocol):
    def fetch(self) -> FetchResult | None:
        """One page load and capture; None if stop was requested mid-fetch."""
        ...

    def close(self) -> None: ...


# --- pure helpers ---------------------------------------------------------------------------


def is_ismds_url(url: str, event_id: str) -> bool:
    """Whether ``url`` is one of the event's ismds facets/quickpicks/offers calls (either host)."""
    pattern = (
        r"https://(services|offeradapter)\.ticketmaster\.com/api/ismds/event/"
        rf"{re.escape(event_id)}/(facets|quickpicks|offers)\b"
    )
    return re.match(pattern, url) is not None


def is_offer_facets(url: str) -> bool:
    """The ``facets?by=inventorytypes offer&embed=offer`` call: every offer with its prices."""
    parts = urlsplit(url)
    if not parts.path.rstrip("/").endswith("/facets"):
        return False
    params = parse_qs(parts.query)
    by = {" ".join(value.split()) for value in params.get("by", [])}
    return "inventorytypes offer" in by and "offer" in params.get("embed", [])


def has_display(environ: Mapping[str, str]) -> bool:
    return bool(environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))


def recycle_due(age_s: float, recycle_hours: float) -> bool:
    return age_s >= recycle_hours * 3600


def first_line(text: object) -> str:
    lines = str(text).strip().splitlines()
    return lines[0] if lines else ""


def classify_launch_error(message: str) -> str:
    """``profile_in_use`` if another Chromium holds the profile, else ``launch: <first line>``."""
    if _PROFILE_IN_USE_RE.search(message):
        return PROFILE_IN_USE
    return f"launch: {first_line(message)}"


def parse_resale_eligibility(html: str) -> bool | None:
    """``resaleEligibilityJSON.isResaleEligible`` from the page state; None if absent."""
    found = _ELIGIBILITY_RE.search(html)
    return None if found is None else found.group(1) == "true"


@dataclass(frozen=True, slots=True)
class PageEvidence:
    """What one page load produced, for ``classify_failure``."""

    document_status: int | None
    title: str
    responses: tuple[CapturedResponse, ...]
    blocklisted: bool
    """A ``tm-bl: 1`` header on the document or an ismds response."""


def has_offer_facets(responses: Iterable[CapturedResponse]) -> bool:
    return any(_has_data(r) and is_offer_facets(r.url) for r in responses)


def captured_quantities(responses: Iterable[CapturedResponse]) -> frozenset[int]:
    return frozenset(
        qty for r in responses if _has_data(r) and (qty := quickpicks_qty(r.url)) is not None
    )


def block_signals(evidence: PageEvidence) -> list[str]:
    """Every sign that Ticketmaster served a block or challenge instead of the event."""
    signals: list[str] = []
    if evidence.document_status in BLOCKED_DOCUMENT_STATUSES:
        signals.append(f"document {evidence.document_status}")
    if evidence.blocklisted:
        signals.append("tm-bl")
    if any(marker in evidence.title for marker in BLOCK_TITLES):
        signals.append(f"title {evidence.title!r}")
    if any(_is_dynamic_block(r.body) for r in evidence.responses):
        signals.append("dynamic_block")
    return signals


def classify_failure(evidence: PageEvidence) -> tuple[PollOutcome, str]:
    """OK with offer facets and quickpicks; otherwise BLOCKED or ERROR (data present wins)."""
    responses = evidence.responses
    has_offers, has_quickpicks = has_offer_facets(responses), bool(captured_quantities(responses))
    if has_offers and has_quickpicks:
        return PollOutcome.OK, ""
    if any(_has_data(r) for r in responses):
        return PollOutcome.ERROR, "missing offer facets" if has_quickpicks else "missing quickpicks"
    if signals := block_signals(evidence):
        return PollOutcome.BLOCKED, "blocked: " + ", ".join(signals)
    if not responses:
        return PollOutcome.ERROR, "timeout: no ismds responses"
    return PollOutcome.ERROR, f"timeout: no ismds data ({len(responses)} failed responses)"


def _has_data(response: CapturedResponse) -> bool:
    return response.status == 200 and response.body is not None


def _is_dynamic_block(body: Mapping[str, Any] | None) -> bool:
    return body is not None and body.get("response") == "dynamic_block"


def read_response(raw: RawResponse) -> CapturedResponse:
    """The response with its JSON object body (None if it is not JSON or unreadable)."""
    return CapturedResponse(url=raw.url, status=raw.status, body=_json_body(raw))


def _json_body(raw: RawResponse) -> Mapping[str, Any] | None:
    try:
        if "json" not in (raw.header_value("content-type") or ""):
            return None
        data = json.loads(raw.body())
    except (BrowserError, ValueError) as exc:
        path = urlsplit(raw.url).path
        log.debug("unreadable body (%d %s): %s", raw.status, path, first_line(exc))
        return None
    return data if isinstance(data, dict) else None


# --- the fetcher ----------------------------------------------------------------------------


class PlaywrightFetcher:
    """Keeps one headed Chromium (persistent profile) and reloads the event page per fetch."""

    def __init__(
        self,
        settings: Settings,
        stop: threading.Event,
        *,
        launcher: Launcher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._stop = stop
        self._launcher = launcher if launcher is not None else launch_chromium
        self._monotonic = monotonic
        self._environ = os.environ if environ is None else environ
        self._tab: BrowserTab | None = None
        self._launched_at = 0.0
        self._responses: list[CapturedResponse] = []
        self._blocklisted = False

    def fetch(self) -> FetchResult | None:
        started = self._monotonic()
        try:
            recycled = self._recycle_if_due()
            fresh = self._tab is None
            tab = self._ensure_tab()
            result = self._load(tab, fresh=fresh, started=started, recycled=recycled)
        except (LaunchError, BrowserError) as exc:
            if self._stop.is_set():
                return None  # the browser went away during shutdown: stopped, not an error
            detail = str(exc) if isinstance(exc, LaunchError) else f"browser: {first_line(exc)}"
            result = self._failure(PollOutcome.ERROR, detail, started)
        if result is not None and result.outcome is not PollOutcome.OK:
            self.close()
        return result

    def close(self) -> None:
        tab, self._tab = self._tab, None
        if tab is None:
            return
        try:
            tab.close()
        except BrowserError as exc:
            log.warning("closing the browser failed: %s", first_line(exc))

    # --- browser lifecycle

    def _recycle_if_due(self) -> bool:
        age_s = self._monotonic() - self._launched_at
        if self._tab is None or not recycle_due(age_s, self._settings.browser_recycle_hours):
            return False
        self.close()
        log.info("browser recycled (age %.1fh)", age_s / 3600)
        return True

    def _ensure_tab(self) -> BrowserTab:
        if self._tab is not None:
            return self._tab
        if not has_display(self._environ):
            raise LaunchError("no_display: neither DISPLAY nor WAYLAND_DISPLAY is set")
        try:
            self._tab = self._launcher(self._settings, self._on_response)
        except BrowserError as exc:
            detail = classify_launch_error(str(exc))
            if detail == PROFILE_IN_USE:
                log.warning("%s", PROFILE_IN_USE_MESSAGE)
            raise LaunchError(detail) from exc
        self._launched_at = self._monotonic()
        return self._tab

    def _on_response(self, response: RawResponse) -> None:
        # Read bodies on arrival, while Chromium still holds them.
        if self._is_capture(response):
            self._responses.append(read_response(response))
            self._blocklisted |= _blocklisted(response)

    def _is_capture(self, response: RawResponse) -> bool:
        return is_ismds_url(response.url, self._settings.event_id)

    # --- one page load

    def _load(
        self, tab: BrowserTab, *, fresh: bool, started: float, recycled: bool
    ) -> FetchResult | None:
        tab.title()  # dispatches responses queued since the last poll, so the clear drops them
        self._responses.clear()
        self._blocklisted = False
        try:
            document = self._navigate(tab, fresh=fresh)
        except BrowserError as exc:
            if self._stop.is_set():
                return None
            if _CLOSED_RE.search(str(exc)):
                raise
            return self._failure(PollOutcome.ERROR, f"navigation: {first_line(exc)}", started)

        settings = self._settings
        captured = self._wait_until(tab, self._has_primary_data, settings.capture_timeout_seconds)
        if captured is None:
            return None
        if captured:
            for qty in sorted(settings.wanted_quantities - self._quantities()):
                if self._capture_quantity(tab, qty) is None:
                    return None
            if self._wait_until(tab, lambda: False, settings.capture_settle_seconds) is None:
                return None
            if unwanted := self._quantities() - settings.wanted_quantities:
                log.info("page also fetched quickpicks for qty=%s (ignored)", _qty_list(unwanted))

        responses = tuple(self._responses)
        html, title = tab.content(), tab.title()
        evidence = PageEvidence(
            document_status=None if document is None else document.status,
            title=title,
            responses=responses,
            blocklisted=self._blocklisted or _blocklisted(document),
        )
        outcome, detail = classify_failure(evidence)
        if outcome is not PollOutcome.OK:
            try_write_dump(self._settings, detail, responses, html=html, title=title)
        return FetchResult(
            outcome=outcome,
            responses=responses,
            detail=detail,
            resale_eligible=parse_resale_eligibility(html),
            elapsed_s=self._monotonic() - started,
            browser_recycled=recycled,
        )

    def _navigate(self, tab: BrowserTab, *, fresh: bool) -> RawResponse | None:
        if fresh:
            return tab.goto(self._settings.event_url, NAVIGATION_TIMEOUT_S)
        return tab.reload(NAVIGATION_TIMEOUT_S)

    def _wait_until(
        self, tab: BrowserTab, done: Callable[[], bool], timeout_s: float
    ) -> bool | None:
        """Pump page events in ≤1 s slices until ``done()``; False on timeout, None on stop."""
        deadline = self._monotonic() + timeout_s
        while True:
            if self._stop.is_set():
                return None
            if done():
                return True
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            tab.wait_for_response(self._is_capture, min(WAIT_SLICE_S, remaining))

    def _capture_quantity(self, tab: BrowserTab, qty: int) -> bool | None:
        """Pick ``qty`` in the page's quantity dropdown so it fetches quickpicks for it."""
        selected_at = self._monotonic()
        try:
            tab.select_quantity(qty, QTY_CAPTURE_TIMEOUT_S)
        except BrowserError as exc:
            log.warning("qty %d dropdown failed (%s); using maxQuantity", qty, first_line(exc))
            return False
        arrived = self._wait_until(tab, lambda: qty in self._quantities(), QTY_CAPTURE_TIMEOUT_S)
        if arrived is False:
            log.warning("no quickpicks for qty=%d after selecting it; using maxQuantity", qty)
        elif arrived:
            log.debug("qty %d quickpicks in %.2fs", qty, self._monotonic() - selected_at)
        return arrived

    def _has_primary_data(self) -> bool:
        return has_offer_facets(self._responses) and bool(self._quantities())

    def _quantities(self) -> frozenset[int]:
        return captured_quantities(self._responses)

    def _failure(self, outcome: PollOutcome, detail: str, started: float) -> FetchResult:
        return FetchResult(
            outcome=outcome,
            responses=(),
            detail=detail,
            resale_eligible=None,
            elapsed_s=self._monotonic() - started,
        )


def _qty_list(quantities: Iterable[int]) -> str:
    return ",".join(map(str, sorted(quantities)))


def _blocklisted(response: RawResponse | None) -> bool:
    if response is None:
        return False
    try:
        return response.header_value("tm-bl") == "1"
    except BrowserError:
        return False


def build_fetcher(settings: Settings, stop: threading.Event) -> Fetcher:
    """The production fetcher (tests replace this factory)."""
    return PlaywrightFetcher(settings, stop)


# --- Playwright ------------------------------------------------------------------------------


@contextmanager
def _translated() -> Iterator[None]:
    try:
        yield
    except PlaywrightError as exc:
        raise BrowserError(str(exc)) from exc


class _Response:
    """A Playwright response behind the ``RawResponse`` protocol."""

    def __init__(self, response: PlaywrightResponse) -> None:
        self._response = response

    @property
    def url(self) -> str:
        return self._response.url

    @property
    def status(self) -> int:
        return self._response.status

    def header_value(self, name: str) -> str | None:
        # A revalidated (304) response's raw headers lack content-type; the page-visible ones don't.
        with _translated():
            return self._response.header_value(name) or self._response.headers.get(name.lower())

    def body(self) -> bytes:
        with _translated():
            return self._response.body()


def _wrap(response: PlaywrightResponse | None) -> RawResponse | None:
    return None if response is None else _Response(response)


class PlaywrightTab:
    """The real ``BrowserTab``: one page of a persistent Chromium context."""

    def __init__(self, playwright: Playwright, page: Page) -> None:
        self._playwright = playwright
        self._page = page

    def goto(self, url: str, timeout_s: float) -> RawResponse | None:
        with _translated():
            return _wrap(
                self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            )

    def reload(self, timeout_s: float) -> RawResponse | None:
        with _translated():
            return _wrap(self._page.reload(wait_until="domcontentloaded", timeout=timeout_s * 1000))

    def wait_for_response(self, predicate: ResponsePredicate, timeout_s: float) -> bool:
        try:
            with _translated():
                self._page.wait_for_event(
                    "response",
                    predicate=lambda response: predicate(_Response(response)),
                    timeout=timeout_s * 1000,
                )
        except BrowserError as exc:
            if isinstance(exc.__cause__, PlaywrightTimeoutError):
                return False
            raise
        return True

    def select_quantity(self, qty: int, timeout_s: float) -> None:
        # force: skip the visibility wait (an overlay may cover the filter bar); selectOption
        # dispatches the same input/change events either way.
        select = self._page.locator(QTY_SELECTOR)
        try:
            with _translated():
                select.select_option(str(qty), force=True, timeout=timeout_s * 1000)
        except BrowserError as exc:
            raise BrowserError(f"{first_line(exc)} select={_select_state(select)}") from exc

    def content(self) -> str:
        with _translated():
            return self._page.content()

    def title(self) -> str:
        with _translated():
            return self._page.title()

    def close(self) -> None:
        try:
            with _translated():
                self._page.context.close()
        finally:
            self._playwright.stop()


_SELECT_STATE_JS: Final = """els => els.map(el => ({
    visible: el.getClientRects().length > 0,
    disabled: el.disabled,
    value: el.value,
    options: Array.from(el.options, option => option.value),
}))"""


def _select_state(select: Locator) -> str:
    """The quantity select's state, for the dropdown-failure warning."""
    try:
        with _translated():
            return json.dumps(select.evaluate_all(_SELECT_STATE_JS))
    except BrowserError as exc:
        return f"unavailable ({first_line(exc)})"


def launch_chromium(settings: Settings, sink: ResponseSink) -> BrowserTab:
    """Start the system Chromium, headed, on the persistent profile (never touching its locks)."""
    playwright = sync_playwright().start()
    try:
        with _translated():
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=settings.browser_profile_dir,
                executable_path=settings.chromium_path,
                headless=False,
                viewport={"width": 1400, "height": 900},
                locale="en-US",
                timezone_id="America/Chicago",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    *settings.browser_extra_args,
                ],
                ignore_default_args=["--enable-automation"],
                handle_sigint=False,
            )
            page = context.pages[0] if context.pages else context.new_page()
    except BaseException:
        playwright.stop()
        raise
    page.on("response", lambda response: sink(_Response(response)))
    return PlaywrightTab(playwright, page)
