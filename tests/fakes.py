"""Test doubles for the browser seam (``BrowserTab``/``Launcher``) and the ``Fetcher``."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from ticket_poller.config import Settings
from ticket_poller.fetcher import BrowserError, RawResponse, ResponsePredicate, ResponseSink
from ticket_poller.models import CapturedResponse, FetchResult

JSON_HEADERS: Mapping[str, str] = {"content-type": "application/json;charset=utf-8"}


class FakeTime:
    """A monotonic clock that only moves when the fake page waits."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakeRawResponse:
    url: str
    status: int = 200
    payload: object = None
    """JSON-encoded by ``body()``; None means the body is unavailable."""
    headers: Mapping[str, str] = field(default_factory=lambda: dict(JSON_HEADERS))

    def header_value(self, name: str) -> str | None:
        return self.headers.get(name)

    def body(self) -> bytes:
        if self.payload is None:
            raise BrowserError("Protocol error: No resource with given identifier found")
        return json.dumps(self.payload).encode()


def raw(response: CapturedResponse, **headers: str) -> FakeRawResponse:
    return FakeRawResponse(
        response.url, response.status, response.body, {**JSON_HEADERS, **headers}
    )


type Script = Sequence[tuple[float, FakeRawResponse]]
"""Responses the page receives, each ``seconds`` after the triggering action."""


@dataclass
class PageScript:
    """How the fake event page behaves on every load."""

    on_load: Script = ()
    document: RawResponse | None = None
    dropdown: Mapping[int, Script] = field(default_factory=dict)
    html: str = "<html></html>"
    title: str = "Noah Kahan Tickets"
    navigation_error: str | None = None


class FakeTab:
    def __init__(self, script: PageScript, time: FakeTime, sink: ResponseSink) -> None:
        self.script, self.time, self.sink = script, time, sink
        self.calls: list[str] = []
        self.closed = False
        self.on_wait: Callable[[], None] | None = None
        self._scheduled: list[tuple[float, FakeRawResponse]] = []

    def goto(self, url: str, timeout_s: float) -> RawResponse | None:
        self.calls.append("goto")
        return self._load()

    def reload(self, timeout_s: float) -> RawResponse | None:
        self.calls.append("reload")
        return self._load()

    def _load(self) -> RawResponse | None:
        self._deliver_arrived()
        self._scheduled.clear()  # navigating cancels the old page's pending requests
        if self.script.navigation_error is not None:
            raise BrowserError(self.script.navigation_error)
        self._schedule(self.script.on_load)
        return self.script.document

    def _deliver_arrived(self) -> None:
        """Like Playwright: responses that arrived while idle reach the sink on the next call."""
        while self._scheduled and self._scheduled[0][0] <= self.time.now:
            self.sink(self._scheduled.pop(0)[1])

    def _schedule(self, script: Script) -> None:
        self._scheduled.extend((self.time.now + delay, replace(r)) for delay, r in script)
        self._scheduled.sort(key=lambda item: item[0])

    def wait_for_response(self, predicate: ResponsePredicate, timeout_s: float) -> bool:
        if self.on_wait is not None:
            self.on_wait()
        deadline = self.time.now + timeout_s
        while self._scheduled and self._scheduled[0][0] <= deadline:
            at, response = self._scheduled.pop(0)
            self.time.now = max(self.time.now, at)
            self.sink(response)
            if predicate(response):
                return True
        self.time.now = deadline
        return False

    def select_quantity(self, qty: int, timeout_s: float) -> None:
        self.calls.append(f"select {qty}")
        if qty not in self.script.dropdown:
            self.time.now += timeout_s
            raise BrowserError(f"Timeout {timeout_s * 1000:.0f}ms exceeded.\nCall log: ...")
        self._schedule(self.script.dropdown[qty])

    def content(self) -> str:
        self._deliver_arrived()
        return self.script.html

    def title(self) -> str:
        self._deliver_arrived()
        return self.script.title

    def close(self) -> None:
        self.closed = True


class FakeLauncher:
    """Launches a fresh ``FakeTab`` per call, or fails with ``error``."""

    def __init__(self, script: PageScript, time: FakeTime, error: str | None = None) -> None:
        self.script, self.time, self.error = script, time, error
        self.tabs: list[FakeTab] = []
        self.on_wait: Callable[[], None] | None = None

    def __call__(self, settings: Settings, sink: ResponseSink) -> FakeTab:
        if self.error is not None:
            raise BrowserError(self.error)
        tab = FakeTab(self.script, self.time, sink)
        tab.on_wait = self.on_wait
        self.tabs.append(tab)
        return tab


class FakeFetcher:
    """Returns ``result`` from ``fetch()`` and records ``close()``."""

    def __init__(self, result: FetchResult | None) -> None:
        self.result = result
        self.fetches = 0
        self.closed = False

    def fetch(self) -> FetchResult | None:
        self.fetches += 1
        return self.result

    def close(self) -> None:
        self.closed = True
