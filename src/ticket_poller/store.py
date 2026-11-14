"""SQLite persistence: polls, listings, per-channel alert deliveries, notifications, state.

Two ways to open the database (§13):

- ``Store.open_writer`` (``run``, ``poll-once --notify``) must be called while holding the
  instance lock. It enables WAL and runs any pending migrations in ``BEGIN IMMEDIATE``.
- ``Store.open_readonly`` (``status``, ``canary-now``, dry-run ``poll-once``) opens the
  file with ``mode=ro``. It never migrates, and refuses a schema it doesn't match.

Timestamps are stored as aware UTC ISO-8601 text with a fixed width, so they sort
lexicographically. Write methods are atomic on their own and join an enclosing
``transaction()`` when there is one.
"""

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any, Final, NamedTuple, Self

from ticket_poller.models import (
    Channel,
    InventoryType,
    Listing,
    Match,
    PollOutcome,
    PriceSnapshot,
    Priority,
    ResaleCapture,
)

BUSY_TIMEOUT_MS: Final = 5000
ALERT_KIND: Final = "alert"
"""``notifications.kind`` for listing alerts; admin notices use their own kinds."""
SNAPSHOT_QUANTITIES: Final = (2, 3)
"""Quantities with per-quantity cheapest-price columns in ``polls``."""

_SUCCESS: Final = (PollOutcome.OK.value, PollOutcome.PARTIAL.value)

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (  # v1
        """CREATE TABLE polls (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'partial', 'blocked', 'error')),
            detail TEXT NOT NULL DEFAULT '',
            elapsed_s REAL,
            resale_capture TEXT,
            primary_listings INTEGER,
            resale_listings INTEGER,
            baseline_sample_cents INTEGER,
            cheapest_primary_qty2_cents INTEGER,
            cheapest_primary_qty3_cents INTEGER,
            cheapest_resale_qty2_cents INTEGER,
            cheapest_resale_qty3_cents INTEGER,
            cheapest_primary_qty2_label TEXT,
            cheapest_primary_qty3_label TEXT,
            cheapest_resale_qty2_label TEXT,
            cheapest_resale_qty3_label TEXT,
            matches INTEGER,
            new_matches INTEGER,
            browser_recycled INTEGER NOT NULL DEFAULT 0)""",
        "CREATE INDEX polls_started_at ON polls (started_at)",
        """CREATE TABLE listings (
            dedup_key TEXT PRIMARY KEY,
            inventory_type TEXT NOT NULL,
            offer_id TEXT NOT NULL,
            places TEXT NOT NULL DEFAULT '[]',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_price_cents INTEGER NOT NULL,
            summary TEXT NOT NULL)""",
        "CREATE INDEX listings_offer ON listings (offer_id)",
        """CREATE TABLE alert_deliveries (
            dedup_key TEXT NOT NULL REFERENCES listings (dedup_key) ON DELETE CASCADE,
            channel TEXT NOT NULL CHECK (channel IN ('email', 'push')),
            delivered_at TEXT NOT NULL,
            priority INTEGER NOT NULL,
            PRIMARY KEY (dedup_key, channel))""",
        """CREATE TABLE notifications (
            id INTEGER PRIMARY KEY,
            sent_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            channel TEXT NOT NULL,
            ok INTEGER NOT NULL,
            subject TEXT NOT NULL)""",
        "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ),
)
"""Schema migrations; entry ``i`` upgrades ``user_version`` i to i + 1."""


class StoreError(Exception):
    """The state database can't be used."""


class NoStateError(StoreError):
    """The database doesn't exist (or was never initialised) yet."""


class SchemaVersionError(StoreError):
    """The database schema doesn't match this version of the code."""


def schema_version() -> int:
    """The schema version this code reads and writes."""
    return len(MIGRATIONS)


def _ts(instant: datetime) -> str:
    if instant.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return instant.astimezone(UTC).isoformat(timespec="microseconds")


def _from_ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _optional_ts(text: str | None) -> datetime | None:
    return None if text is None else _from_ts(text)


# --- value types -----------------------------------------------------------------------------

type KeyedMatch = tuple[str, Match]
"""A match paired with the dedup key of its stored listing."""


@dataclass(frozen=True, slots=True)
class Resolved:
    """Where a current listing lives in ``listings`` (§6.6)."""

    identity: str
    """The listing's own canonical identity this poll."""
    key: str
    """The stored ``dedup_key``: an existing row's key, or ``identity`` if it is new."""
    rearmed: bool
    """The row was absent longer than the re-arm window, so its deliveries are reset."""


@dataclass(frozen=True, slots=True)
class PollRecord:
    """One row of ``polls``. Inventory fields are None unless the outcome is OK/PARTIAL."""

    started_at: datetime
    outcome: PollOutcome
    detail: str = ""
    elapsed_s: float | None = None
    resale_capture: ResaleCapture | None = None
    primary_listings: int | None = None
    resale_listings: int | None = None
    snapshot: PriceSnapshot | None = None
    matches: int | None = None
    new_matches: int | None = None
    primary_labels: Mapping[int, str] = field(default_factory=dict)
    """Per quantity, "section/row" of the cheapest primary listing in ``snapshot``."""
    resale_labels: Mapping[int, str] = field(default_factory=dict)
    browser_recycled: bool = False
    """The browser was restarted during this poll."""


@dataclass(frozen=True, slots=True)
class PollStats:
    """Aggregates over polls since ``since`` (canary and status)."""

    since: datetime
    outcomes: Mapping[PollOutcome, int]
    last_success_at: datetime | None
    """The latest OK/PARTIAL poll at any time, not only in the window."""
    resale_captured: int
    """Successful polls in the window whose resale capture was CAPTURED."""
    cheapest_primary: Mapping[int, int | None]
    """Per quantity, from the latest successful poll in the window."""
    cheapest_resale: Mapping[int, int | None]
    cheapest_primary_label: Mapping[int, str | None]
    """Per quantity, the label of the listing behind ``cheapest_primary``."""
    cheapest_resale_label: Mapping[int, str | None]
    low_primary: Mapping[int, int | None]
    """Per quantity, the lowest cheapest price over the window."""
    low_resale: Mapping[int, int | None]
    alerts: int
    """Listings with an alert delivered (any channel) in the window."""
    top_alerts: int
    channel_failures: Mapping[Channel, int]
    """Failed alert sends per channel in the window."""
    browser_recycles: int
    """Polls in the window during which the browser was restarted."""

    @property
    def total(self) -> int:
        return sum(self.outcomes.values())


@dataclass(frozen=True, slots=True)
class AlertAttempt:
    at: datetime
    ok: bool


_DATETIME_FIELDS: Final = frozenset({"failure_started_at"})
_DATE_FIELDS: Final = frozenset({"last_canary_date"})


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """Loop state persisted in ``kv`` so restarts keep backoff, outage and notice flags.

    The ``record_*`` methods are the allowed transitions; each returns a new state.
    """

    failure_started_at: datetime | None = None
    consecutive_failures: int = 0
    consecutive_blocks: int = 0
    outage_announced: bool = False
    recovery_pending: bool = False
    last_outage_duration_s: float | None = None
    last_canary_date: date | None = None
    finished_notice_sent: bool = False
    resale_eligible: bool | None = None
    resale_eligible_notice_sent: bool = False
    resale_missing_streak: int = 0
    resale_missing_notice_sent: bool = False

    def record_failure(self, outcome: PollOutcome, now: datetime) -> Self:
        if outcome not in (PollOutcome.ERROR, PollOutcome.BLOCKED):
            raise ValueError(f"{outcome} is not a failure")
        blocked = outcome is PollOutcome.BLOCKED
        return replace(
            self,
            failure_started_at=self.failure_started_at or now,
            consecutive_failures=self.consecutive_failures + 1,
            consecutive_blocks=self.consecutive_blocks + 1 if blocked else 0,
        )

    def record_success(self, now: datetime) -> Self:
        """End any failure streak; a recovery notice is owed only if the outage was announced."""
        if self.failure_started_at is None:
            return self
        announced = self.outage_announced
        duration = (now - self.failure_started_at).total_seconds()
        return replace(
            self,
            failure_started_at=None,
            consecutive_failures=0,
            consecutive_blocks=0,
            outage_announced=False,
            recovery_pending=self.recovery_pending or announced,
            last_outage_duration_s=duration if announced else self.last_outage_duration_s,
        )

    def record_outage_notice(self, delivered: bool) -> Self:
        """The outage counts as announced only once at least one channel delivered."""
        return replace(self, outage_announced=True) if delivered else self

    def record_recovery_notice(self, delivered: bool) -> Self:
        return replace(self, recovery_pending=False) if delivered else self

    def record_canary(self, local_date: date, email_ok: bool) -> Self:
        """``last_canary_date`` moves only when the canary email was accepted."""
        return replace(self, last_canary_date=local_date) if email_ok else self


def _encode_state_value(value: object) -> str:
    if isinstance(value, datetime):
        value = _ts(value)
    elif isinstance(value, date):
        value = value.isoformat()
    return json.dumps(value)


def _decode_state_value(name: str, text: str) -> Any:
    value = json.loads(text)
    if value is None:
        return None
    if name in _DATETIME_FIELDS:
        return _from_ts(value)
    if name in _DATE_FIELDS:
        return date.fromisoformat(value)
    return value


class _StoredListing(NamedTuple):
    key: str
    places: frozenset[str]
    last_seen_at: datetime


# --- the store -------------------------------------------------------------------------------


class Store:
    """The state database. Use as a context manager, or call ``close()``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        self._conn = conn

    @classmethod
    def open_writer(cls, path: Path) -> Self:
        """Open read-write and migrate. Call only while holding the instance lock."""
        return cls(_connect(path, path, _prepare_writer))

    @classmethod
    def open_readonly(cls, path: Path) -> Self:
        """Open with ``mode=ro``; never migrates. Raises ``NoStateError``/``SchemaVersionError``."""
        if not path.exists():
            raise NoStateError(f"no state yet: {path} does not exist")
        target = f"{path.resolve().as_uri()}?mode=ro"
        return cls(_connect(path, target, partial(_prepare_reader, path=path), uri=True))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the block in one ``BEGIN IMMEDIATE`` transaction; nested calls join it."""
        if self._conn.in_transaction:
            yield
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # --- listings and dedup (§6.6) -----------------------------------------------------------

    def resolve(
        self, listings: Sequence[Listing], now: datetime, rearm_after: timedelta
    ) -> list[Resolved]:
        """Map each current listing to its stored row (read-only).

        A listing adopts the row with its exact identity, else the most recently seen
        primary row with the same offer and overlapping places. Each row is adopted by at
        most one listing per poll.

        A row is re-armed once it has been gone for ``rearm_after``: measured from the first
        OK/PARTIAL poll after it was last seen, so BLOCKED/ERROR outages don't count as
        absence. ``rearm_after`` of zero disables re-arming.
        """
        found: dict[str, _StoredListing] = {}
        for listing in listings:
            row = self._listing_by_key(listing.identity)
            if row is not None:
                found[listing.identity] = row
        claimed = {row.key for row in found.values()}
        for listing in listings:
            if listing.identity in found or listing.inventory_type is not InventoryType.PRIMARY:
                continue
            row = self._overlapping_listing(listing, exclude=claimed)
            if row is not None:
                found[listing.identity] = row
                claimed.add(row.key)

        return [
            self._resolved(listing.identity, found.get(listing.identity), now, rearm_after)
            for listing in listings
        ]

    def observe(
        self, resolved: Sequence[Resolved], listings: Sequence[Listing], now: datetime
    ) -> None:
        """Upsert every current candidate; re-armed rows lose their deliveries.

        ``now`` must not be earlier than this poll's ``started_at``, or the poll would
        later count as one the listings were missing from.
        """
        with self.transaction():
            for res, listing in zip(resolved, listings, strict=True):
                if res.identity != listing.identity:
                    raise ValueError("resolved and listings are not aligned")
                self._upsert_listing(res.key, listing, now)
                if res.rearmed:
                    self._conn.execute(
                        "DELETE FROM alert_deliveries WHERE dedup_key = ?", (res.key,)
                    )

    def pending(
        self, matches: Iterable[KeyedMatch], channel: Channel, resolved: Iterable[Resolved] = ()
    ) -> list[KeyedMatch]:
        """Matches not yet delivered on ``channel``, or now at a better priority (upgrade).

        Keys re-armed in ``resolved`` are pending even before ``observe`` resets their
        deliveries, so a dry run labels them correctly.
        """
        rearmed = {res.key for res in resolved if res.rearmed}
        return [
            (key, match)
            for key, match in matches
            if key in rearmed or self._needs_delivery(key, match.priority, channel)
        ]

    def mark_delivered(
        self, matches: Iterable[KeyedMatch], channel: Channel, now: datetime
    ) -> None:
        with self.transaction():
            self._conn.executemany(
                """INSERT INTO alert_deliveries (dedup_key, channel, delivered_at, priority)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (dedup_key, channel) DO UPDATE SET
                     delivered_at = excluded.delivered_at,
                     priority = MIN(priority, excluded.priority)""",
                [(key, channel.value, _ts(now), match.priority.value) for key, match in matches],
            )

    def _resolved(
        self, identity: str, row: _StoredListing | None, now: datetime, rearm_after: timedelta
    ) -> Resolved:
        if row is None:
            return Resolved(identity=identity, key=identity, rearmed=False)
        gone_at = self._gone_at(row) if rearm_after else None
        rearmed = gone_at is not None and gone_at <= now - rearm_after
        return Resolved(identity=identity, key=row.key, rearmed=rearmed)

    def _gone_at(self, row: _StoredListing) -> datetime | None:
        """The first successful poll after ``row`` was last seen, i.e. one without it."""
        found = self._conn.execute(
            "SELECT MIN(started_at) FROM polls WHERE outcome IN (?, ?) AND started_at > ?",
            (*_SUCCESS, _ts(row.last_seen_at)),
        ).fetchone()
        return _optional_ts(found[0])

    def _listing_by_key(self, key: str) -> _StoredListing | None:
        row = self._conn.execute(
            "SELECT dedup_key, places, last_seen_at FROM listings WHERE dedup_key = ?", (key,)
        ).fetchone()
        return None if row is None else _stored_listing(row)

    def _overlapping_listing(self, listing: Listing, exclude: set[str]) -> _StoredListing | None:
        rows = self._conn.execute(
            """SELECT dedup_key, places, last_seen_at FROM listings
               WHERE offer_id = ? AND inventory_type = ?
               ORDER BY last_seen_at DESC, dedup_key""",
            (listing.offer_id, InventoryType.PRIMARY.value),
        )
        for stored in map(_stored_listing, rows):
            if stored.key not in exclude and stored.places & listing.places:
                return stored
        return None

    def _upsert_listing(self, key: str, listing: Listing, now: datetime) -> None:
        self._conn.execute(
            """INSERT INTO listings (dedup_key, inventory_type, offer_id, places,
                                     first_seen_at, last_seen_at, last_price_cents, summary)
               VALUES (:key, :type, :offer, :places, :now, :now, :price, :summary)
               ON CONFLICT (dedup_key) DO UPDATE SET
                 places = excluded.places,
                 last_seen_at = excluded.last_seen_at,
                 last_price_cents = excluded.last_price_cents,
                 summary = excluded.summary""",
            {
                "key": key,
                "type": listing.inventory_type.value,
                "offer": listing.offer_id,
                "places": json.dumps(sorted(listing.places)),
                "now": _ts(now),
                "price": listing.price_cents,
                "summary": _summary(listing),
            },
        )

    def _needs_delivery(self, key: str, priority: Priority, channel: Channel) -> bool:
        row = self._conn.execute(
            "SELECT priority FROM alert_deliveries WHERE dedup_key = ? AND channel = ?",
            (key, channel.value),
        ).fetchone()
        return row is None or priority < row[0]

    # --- polls ---------------------------------------------------------------------------

    def record_poll(self, record: PollRecord) -> int:
        capture = record.resale_capture
        values: dict[str, object] = {
            "started_at": _ts(record.started_at),
            "outcome": record.outcome.value,
            "detail": record.detail,
            "elapsed_s": record.elapsed_s,
            "resale_capture": None if capture is None else capture.value,
            "primary_listings": record.primary_listings,
            "resale_listings": record.resale_listings,
            "matches": record.matches,
            "new_matches": record.new_matches,
            "browser_recycled": record.browser_recycled,
            **_snapshot_columns(record.snapshot),
            **_label_columns(InventoryType.PRIMARY, record.primary_labels),
            **_label_columns(InventoryType.RESALE, record.resale_labels),
        }
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        with self.transaction():
            cursor = self._conn.execute(
                f"INSERT INTO polls ({columns}) VALUES ({placeholders})", values
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def recent_polls(self, limit: int) -> list[PollRecord]:
        """The latest ``limit`` polls, newest first."""
        rows = self._conn.execute(
            "SELECT * FROM polls ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [_poll_record(row) for row in rows]

    def baseline_samples(self, since: datetime, until: datetime) -> list[int]:
        """Baseline samples of OK/PARTIAL polls with ``since <= started_at < until``."""
        rows = self._conn.execute(
            """SELECT baseline_sample_cents FROM polls
               WHERE outcome IN (?, ?) AND started_at >= ? AND started_at < ?
                 AND baseline_sample_cents IS NOT NULL
               ORDER BY started_at""",
            (*_SUCCESS, _ts(since), _ts(until)),
        )
        return [cents for (cents,) in rows]

    def stats_since(self, since: datetime) -> PollStats:
        start = _ts(since)
        latest = self._latest_success(start)
        return PollStats(
            since=since,
            outcomes=self._outcome_counts(start),
            last_success_at=self._last_success_at(),
            resale_captured=self._scalar(
                """SELECT COUNT(*) FROM polls
                   WHERE started_at >= ? AND outcome IN (?, ?) AND resale_capture = ?""",
                (start, *_SUCCESS, ResaleCapture.CAPTURED.value),
            ),
            cheapest_primary=_by_qty(latest, _price_column, InventoryType.PRIMARY),
            cheapest_resale=_by_qty(latest, _price_column, InventoryType.RESALE),
            cheapest_primary_label=_by_qty(latest, _label_column, InventoryType.PRIMARY),
            cheapest_resale_label=_by_qty(latest, _label_column, InventoryType.RESALE),
            low_primary=self._lowest_cheapest(InventoryType.PRIMARY, start),
            low_resale=self._lowest_cheapest(InventoryType.RESALE, start),
            alerts=self._scalar(
                "SELECT COUNT(DISTINCT dedup_key) FROM alert_deliveries WHERE delivered_at >= ?",
                (start,),
            ),
            top_alerts=self._scalar(
                """SELECT COUNT(DISTINCT dedup_key) FROM alert_deliveries
                   WHERE delivered_at >= ? AND priority = ?""",
                (start, Priority.TOP.value),
            ),
            channel_failures=self._alert_failures(start),
            browser_recycles=self._scalar(
                "SELECT COUNT(*) FROM polls WHERE started_at >= ? AND browser_recycled", (start,)
            ),
        )

    def _outcome_counts(self, start: str) -> dict[PollOutcome, int]:
        rows = self._conn.execute(
            "SELECT outcome, COUNT(*) FROM polls WHERE started_at >= ? GROUP BY outcome",
            (start,),
        )
        counts = dict.fromkeys(PollOutcome, 0)
        counts.update({PollOutcome(outcome): count for outcome, count in rows})
        return counts

    def _last_success_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT MAX(started_at) FROM polls WHERE outcome IN (?, ?)", _SUCCESS
        ).fetchone()
        return _optional_ts(row[0])

    def _latest_success(self, start: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            """SELECT * FROM polls WHERE started_at >= ? AND outcome IN (?, ?)
               ORDER BY started_at DESC, id DESC LIMIT 1""",
            (start, *_SUCCESS),
        ).fetchone()
        return row

    def _lowest_cheapest(self, kind: InventoryType, start: str) -> dict[int, int | None]:
        columns = ", ".join(f"MIN({_price_column(kind, qty)})" for qty in SNAPSHOT_QUANTITIES)
        row = self._conn.execute(
            f"SELECT {columns} FROM polls WHERE started_at >= ? AND outcome IN (?, ?)",
            (start, *_SUCCESS),
        ).fetchone()
        return dict(zip(SNAPSHOT_QUANTITIES, row, strict=True))

    def _alert_failures(self, start: str) -> dict[Channel, int]:
        rows = self._conn.execute(
            """SELECT channel, COUNT(*) FROM notifications
               WHERE sent_at >= ? AND kind = ? AND NOT ok GROUP BY channel""",
            (start, ALERT_KIND),
        )
        failures = dict.fromkeys(Channel, 0)
        failures.update({Channel(channel): count for channel, count in rows})
        return failures

    def _scalar(self, sql: str, params: Sequence[object]) -> int:
        value: int = self._conn.execute(sql, params).fetchone()[0]
        return value

    # --- notifications audit -------------------------------------------------------------

    def log_notification(
        self, kind: str, channel: Channel, ok: bool, subject: str, now: datetime
    ) -> None:
        """Record one channel attempt (alerts use ``ALERT_KIND``)."""
        with self.transaction():
            self._conn.execute(
                """INSERT INTO notifications (sent_at, kind, channel, ok, subject)
                   VALUES (?, ?, ?, ?, ?)""",
                (_ts(now), kind, channel.value, ok, subject),
            )

    def last_alert_attempts(self) -> dict[Channel, AlertAttempt]:
        """The most recent alert attempt per channel; a failed one is retried next poll."""
        rows = self._conn.execute(
            """SELECT channel, sent_at, ok FROM notifications
               WHERE id IN (SELECT MAX(id) FROM notifications WHERE kind = ? GROUP BY channel)""",
            (ALERT_KIND,),
        )
        return {
            Channel(channel): AlertAttempt(_from_ts(sent_at), bool(ok))
            for channel, sent_at, ok in rows
        }

    # --- runtime state -------------------------------------------------------------------

    def get_state(self) -> RuntimeState:
        names = {f.name for f in fields(RuntimeState)}
        stored = {
            key: _decode_state_value(key, value)
            for key, value in self._conn.execute("SELECT key, value FROM kv")
            if key in names
        }
        return RuntimeState(**stored)

    def save_state(self, state: RuntimeState) -> None:
        with self.transaction():
            self._conn.executemany(
                "INSERT INTO kv (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                [(f.name, _encode_state_value(getattr(state, f.name))) for f in fields(state)],
            )


def key_matches(matches: Iterable[Match], resolved: Iterable[Resolved]) -> list[KeyedMatch]:
    """Pair each match with the stored key its listing resolved to."""
    keys = {res.identity: res.key for res in resolved}
    return [(keys[match.listing.identity], match) for match in matches]


# --- helpers -----------------------------------------------------------------------------------


def _connect(
    path: Path,
    target: str | Path,
    prepare: Callable[[sqlite3.Connection], None],
    *,
    uri: bool = False,
) -> sqlite3.Connection:
    """Connect and run ``prepare``; SQLite failures (e.g. a corrupt file) become StoreError."""
    try:
        conn = sqlite3.connect(target, uri=uri, autocommit=True)
    except sqlite3.DatabaseError as exc:
        raise StoreError(f"cannot open {path}: {exc}") from exc
    try:
        prepare(conn)
    except BaseException as exc:
        conn.close()
        if isinstance(exc, sqlite3.DatabaseError):
            raise StoreError(f"cannot open {path}: {exc}") from exc
        raise
    return conn


def _prepare_writer(conn: sqlite3.Connection) -> None:
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)


def _prepare_reader(conn: sqlite3.Connection, path: Path) -> None:
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    _check_readable_version(conn, path)


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = _user_version(conn)
        if version > schema_version():
            raise SchemaVersionError(
                f"state schema v{version} is newer than this code (v{schema_version()})"
            )
        for target, statements in enumerate(MIGRATIONS[version:], start=version + 1):
            for statement in statements:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {int(target)}")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _check_readable_version(conn: sqlite3.Connection, path: Path) -> None:
    version = _user_version(conn)
    if version == 0:
        raise NoStateError(f"no state yet: {path} has not been initialised")
    if version != schema_version():
        relation = "newer" if version > schema_version() else "older"
        raise SchemaVersionError(
            f"state schema v{version} is {relation} than this code (v{schema_version()}); "
            "start the service once to migrate, or upgrade ticket-poller"
        )


def _user_version(conn: sqlite3.Connection) -> int:
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    return version


def _stored_listing(row: sqlite3.Row) -> _StoredListing:
    key, places, last_seen_at = row
    return _StoredListing(key, frozenset(json.loads(places)), _from_ts(last_seen_at))


def _summary(listing: Listing) -> str:
    return f"{listing.inventory_type.value} {listing.section} {listing.row} {listing.seats}"


def _price_column(kind: InventoryType, qty: int) -> str:
    return f"cheapest_{kind.value}_qty{qty}_cents"


def _label_column(kind: InventoryType, qty: int) -> str:
    return f"cheapest_{kind.value}_qty{qty}_label"


def _label_columns(kind: InventoryType, labels: Mapping[int, str]) -> dict[str, str | None]:
    return {_label_column(kind, qty): labels.get(qty) for qty in SNAPSHOT_QUANTITIES}


def _snapshot_columns(snapshot: PriceSnapshot | None) -> dict[str, int | None]:
    if snapshot is None:
        snapshot = PriceSnapshot(baseline_sample_cents=None, primary_by_qty={}, resale_by_qty={})
    columns = {"baseline_sample_cents": snapshot.baseline_sample_cents}
    for qty in SNAPSHOT_QUANTITIES:
        columns[_price_column(InventoryType.PRIMARY, qty)] = snapshot.primary_by_qty.get(qty)
        columns[_price_column(InventoryType.RESALE, qty)] = snapshot.resale_by_qty.get(qty)
    return columns


def _poll_record(data: sqlite3.Row) -> PollRecord:
    outcome = PollOutcome(data["outcome"])
    snapshot = None
    if outcome.value in _SUCCESS:
        snapshot = PriceSnapshot(
            baseline_sample_cents=data["baseline_sample_cents"],
            primary_by_qty=_by_qty(data, _price_column, InventoryType.PRIMARY),
            resale_by_qty=_by_qty(data, _price_column, InventoryType.RESALE),
        )
    capture = data["resale_capture"]
    return PollRecord(
        started_at=_from_ts(data["started_at"]),
        outcome=outcome,
        detail=data["detail"],
        elapsed_s=data["elapsed_s"],
        resale_capture=None if capture is None else ResaleCapture(capture),
        primary_listings=data["primary_listings"],
        resale_listings=data["resale_listings"],
        snapshot=snapshot,
        matches=data["matches"],
        new_matches=data["new_matches"],
        primary_labels=_labels(data, InventoryType.PRIMARY),
        resale_labels=_labels(data, InventoryType.RESALE),
        browser_recycled=bool(data["browser_recycled"]),
    )


def _by_qty(
    data: sqlite3.Row | None, column: Callable[[InventoryType, int], str], kind: InventoryType
) -> dict[int, Any]:
    """Per-quantity values of ``column``; all None when there is no row."""
    return {qty: None if data is None else data[column(kind, qty)] for qty in SNAPSHOT_QUANTITIES}


def _labels(data: sqlite3.Row, kind: InventoryType) -> dict[int, str]:
    labels = _by_qty(data, _label_column, kind)
    return {qty: label for qty, label in labels.items() if label is not None}
