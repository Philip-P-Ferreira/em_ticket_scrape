import contextlib
import dataclasses
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ticket_poller import store as store_module
from ticket_poller.models import (
    Channel,
    InventoryType,
    Listing,
    Match,
    NoticeKind,
    PollOutcome,
    PriceSnapshot,
    Priority,
    Reason,
    ResaleCapture,
)
from ticket_poller.state import PollRecord, RuntimeState
from ticket_poller.store import (
    BUSY_TIMEOUT_MS,
    MIGRATIONS,
    AlertAttempt,
    NoStateError,
    Resolved,
    SchemaVersionError,
    Store,
    StoreError,
    key_matches,
    schema_version,
)

T0 = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
DAY = timedelta(days=1)
REARM = timedelta(hours=24)


def at(hours: float = 0, minutes: float = 0) -> datetime:
    return T0 + timedelta(hours=hours, minutes=minutes)


def primary(*places: str, offer_id: str = "OFFER1", price_cents: int = 34350) -> Listing:
    return Listing(
        inventory_type=InventoryType.PRIMARY,
        offer_id=offer_id,
        listing_id=None,
        section="104",
        row="R",
        places=frozenset(places),
        seats="9-10",
        price_cents=price_cents,
        fees_cents=6185,
        buyable_quantities=frozenset({2}),
        max_together=2,
    )


def resale(listing_id: str) -> Listing:
    return Listing(
        inventory_type=InventoryType.RESALE,
        offer_id="OFFER1",
        listing_id=listing_id,
        section="101",
        row="N",
        places=frozenset({"A"}),
        seats="12-14",
        price_cents=38350,
        fees_cents=0,
        buyable_quantities=frozenset({3}),
        max_together=None,
    )


def match(listing: Listing, priority: Priority = Priority.NORMAL) -> Match:
    return Match(
        listing=listing,
        reasons=frozenset({Reason.UNDER_PRICE_CAP}),
        priority=priority,
        within_preferred_price=False,
        pct_below_baseline=Decimal("12.5"),
    )


def snapshot(
    baseline: int | None = 30000,
    primary_by_qty: dict[int, int | None] | None = None,
    resale_by_qty: dict[int, int | None] | None = None,
) -> PriceSnapshot:
    return PriceSnapshot(
        baseline_sample_cents=baseline,
        primary_by_qty=primary_by_qty or {2: baseline, 3: None},
        resale_by_qty=resale_by_qty or {2: None, 3: None},
    )


def ok_poll(started_at: datetime, **changes: object) -> PollRecord:
    record = PollRecord(
        started_at=started_at,
        outcome=PollOutcome.OK,
        elapsed_s=12.5,
        resale_capture=ResaleCapture.CAPTURED,
        primary_listings=4,
        resale_listings=1,
        snapshot=snapshot(),
        matches=1,
        new_matches=0,
    )
    return dataclasses.replace(record, **changes)  # type: ignore[arg-type]


def see(
    store: Store, listings: Sequence[Listing], now: datetime, rearm: timedelta = REARM
) -> list[Resolved]:
    """One poll's dedup bookkeeping: resolve, then observe."""
    resolved = store.resolve(listings, now, rearm)
    store.observe(resolved, listings, now)
    return resolved


def deliver(
    store: Store, listing: Listing, now: datetime, *channels: Channel, priority: Priority
) -> None:
    keyed = key_matches([match(listing, priority)], see(store, [listing], now))
    for channel in channels:
        store.mark_delivered(keyed, channel, now)


def pending_keys(
    store: Store, matches: Sequence[Match], channel: Channel, now: datetime
) -> list[str]:
    listings = [m.listing for m in matches]
    keyed = key_matches(matches, see(store, listings, now))
    return [key for key, _ in store.pending(keyed, channel)]


def listing_rows(store: Store) -> list[tuple[str, str]]:
    rows = store._conn.execute("SELECT dedup_key, places FROM listings ORDER BY dedup_key")
    return [(key, places) for key, places in rows]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[Store]:
    with Store.open_writer(db_path) as writer:
        yield writer


@pytest.fixture
def reader(store: Store, db_path: Path) -> Iterator[Store]:
    with Store.open_readonly(db_path) as readonly:
        yield readonly


def raw_pragma(path: Path, name: str) -> list[tuple[object, ...]]:
    """Read a pragma with a plain connection, bypassing ``Store``."""
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return conn.execute(f"PRAGMA {name}").fetchall()


def pragma(store: Store, name: str) -> object:
    value: object = store._conn.execute(f"PRAGMA {name}").fetchone()[0]
    return value


class TestOpening:
    def test_writer_creates_the_latest_schema(self, store: Store) -> None:
        tables = {row[0] for row in store._conn.execute("SELECT name FROM sqlite_master")}

        assert pragma(store, "user_version") == schema_version() == 3
        assert {"polls", "listings", "alert_deliveries", "notifications", "kv"} <= tables
        assert {"polls_started_at", "listings_offer"} <= tables

    def test_writer_pragmas(self, store: Store) -> None:
        assert pragma(store, "journal_mode") == "wal"
        assert pragma(store, "busy_timeout") == BUSY_TIMEOUT_MS
        assert pragma(store, "foreign_keys") == 1

    def test_reader_has_busy_timeout(self, reader: Store) -> None:
        assert pragma(reader, "busy_timeout") == BUSY_TIMEOUT_MS

    def test_reopening_writer_is_idempotent(self, db_path: Path) -> None:
        with Store.open_writer(db_path) as first:
            first.record_poll(ok_poll(T0))

        with Store.open_writer(db_path) as second:
            assert pragma(second, "user_version") == schema_version()
            assert len(second.recent_polls(10)) == 1

    def test_readonly_on_missing_file_does_not_create_it(self, db_path: Path) -> None:
        with pytest.raises(NoStateError, match="no state yet"):
            Store.open_readonly(db_path)

        assert not db_path.exists()

    def test_readonly_never_initialises_an_empty_file(self, db_path: Path) -> None:
        raw_pragma(db_path, "user_version")  # creates an empty file

        with pytest.raises(NoStateError, match="not been initialised"):
            Store.open_readonly(db_path)

        assert raw_pragma(db_path, "user_version") == [(0,)]

    def test_readonly_cannot_write(self, reader: Store) -> None:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.record_poll(ok_poll(T0))
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.save_state(RuntimeState(consecutive_failures=1))

    def test_newer_schema_is_refused_by_both(self, store: Store, db_path: Path) -> None:
        store._conn.execute("PRAGMA user_version = 99")

        with pytest.raises(SchemaVersionError, match="newer"):
            Store.open_writer(db_path)
        with pytest.raises(SchemaVersionError, match="newer"):
            Store.open_readonly(db_path)


@pytest.mark.parametrize("opener", [Store.open_writer, Store.open_readonly])
def test_corrupt_file_is_a_store_error(db_path: Path, opener: Callable[[Path], Store]) -> None:
    db_path.write_bytes(b"this is not a sqlite database" * 100)

    with pytest.raises(StoreError, match=f"cannot open {db_path}"):
        opener(db_path)


class TestMigrations:
    NEXT = ("ALTER TABLE polls ADD COLUMN extra INTEGER",)

    def test_only_the_writer_migrates(
        self, store: Store, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.close()
        current = schema_version()
        monkeypatch.setattr(store_module, "MIGRATIONS", (*MIGRATIONS, self.NEXT))

        with pytest.raises(SchemaVersionError, match="older"):
            Store.open_readonly(db_path)
        assert raw_pragma(db_path, "user_version") == [(current,)]

        with Store.open_writer(db_path) as writer:
            columns = {row[1] for row in writer._conn.execute("PRAGMA table_info(polls)")}
            assert pragma(writer, "user_version") == current + 1
            assert "extra" in columns
        with Store.open_readonly(db_path) as readonly:
            assert pragma(readonly, "user_version") == current + 1

    def test_v1_database_gains_parser_health_columns(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        for statement in MIGRATIONS[0]:
            conn.execute(statement)
        conn.execute("INSERT INTO polls (started_at, outcome) VALUES (?, 'ok')", (T0.isoformat(),))
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        with Store.open_writer(db_path) as writer:
            [old] = writer.recent_polls(1)
            assert pragma(writer, "user_version") == schema_version()

        assert (old.parse_warnings, old.parse_degraded) == (None, False)

    def test_v2_database_gains_the_profile_rotated_column(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        for statement in (*MIGRATIONS[0], *MIGRATIONS[1]):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO polls (started_at, outcome) VALUES (?, 'blocked')", (T0.isoformat(),)
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()

        with Store.open_writer(db_path) as writer:
            [old] = writer.recent_polls(1)
            writer.record_poll(PollRecord(at(0, 5), PollOutcome.BLOCKED, profile_rotated=True))
            [new, _] = writer.recent_polls(2)
            assert pragma(writer, "user_version") == schema_version()

        assert (old.profile_rotated, new.profile_rotated) == (False, True)

    def test_failed_migration_rolls_back(
        self, store: Store, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.close()
        current = schema_version()
        broken = ("ALTER TABLE polls ADD COLUMN extra INTEGER", "NOT VALID SQL")
        monkeypatch.setattr(store_module, "MIGRATIONS", (*MIGRATIONS, broken))

        with pytest.raises(StoreError, match="cannot open"):
            Store.open_writer(db_path)

        assert raw_pragma(db_path, "user_version") == [(current,)]
        assert "extra" not in {row[1] for row in raw_pragma(db_path, "table_info(polls)")}


class TestTransactions:
    def test_rollback_on_error(self, store: Store) -> None:
        def write_then_fail() -> None:
            with store.transaction():
                store.record_poll(ok_poll(T0))
                store.save_state(RuntimeState(consecutive_failures=3))
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            write_then_fail()

        assert store.recent_polls(10) == []
        assert store.get_state() == RuntimeState()

    def test_nested_writes_commit_together(self, store: Store, reader: Store) -> None:
        with store.transaction():
            store.record_poll(ok_poll(T0))
            store.save_state(RuntimeState(consecutive_failures=3))
            assert reader.recent_polls(10) == []

        assert len(reader.recent_polls(10)) == 1
        assert reader.get_state().consecutive_failures == 3


class TestResolve:
    def test_new_listing_keys_by_its_identity(self, store: Store) -> None:
        listing = primary("A", "B")

        [res] = store.resolve([listing], T0, REARM)

        assert res == Resolved(identity=listing.identity, key=listing.identity, rearmed=False)

    def test_resolve_does_not_write(self, store: Store) -> None:
        store.resolve([primary("A", "B")], T0, REARM)

        assert listing_rows(store) == []

    def test_same_listing_keeps_its_key(self, store: Store) -> None:
        see(store, [primary("A", "B")], T0)

        [res] = see(store, [primary("A", "B")], at(minutes=5))

        assert res.key == primary("A", "B").identity
        assert len(listing_rows(store)) == 1

    def test_overlapping_places_adopt_the_stored_key(self, store: Store) -> None:
        see(store, [primary("A", "B")], T0)

        [res] = see(store, [primary("B", "C")], at(minutes=5))

        assert res.identity == primary("B", "C").identity
        assert res.key == primary("A", "B").identity
        assert listing_rows(store) == [(res.key, '["B", "C"]')]

    def test_disjoint_places_are_a_new_listing(self, store: Store) -> None:
        see(store, [primary("A", "B")], T0)

        [res] = see(store, [primary("C", "D")], at(minutes=5))

        assert res.key == primary("C", "D").identity
        assert len(listing_rows(store)) == 2

    def test_different_offer_is_a_new_listing(self, store: Store) -> None:
        see(store, [primary("A", "B")], T0)

        [res] = see(store, [primary("A", "B", offer_id="OFFER2")], at(minutes=5))

        assert res.key == primary("A", "B", offer_id="OFFER2").identity

    def test_resale_never_matches_by_overlap(self, store: Store) -> None:
        see(store, [resale("L1")], T0)

        same, other = see(store, [resale("L1"), resale("L2")], at(minutes=5))

        assert (same.key, other.key) == ("resale:L1", "resale:L2")

    def test_most_recently_seen_overlap_wins(self, store: Store) -> None:
        see(store, [primary("A", "B")], T0)
        see(store, [primary("C", "D")], at(hours=1))

        [res] = see(store, [primary("B", "C")], at(hours=2))

        assert res.key == primary("C", "D").identity

    def test_exact_identity_beats_a_more_recent_overlap(self, store: Store) -> None:
        see(store, [primary("A", "B")], T0)
        see(store, [primary("B", "C")], at(hours=1))

        [res] = see(store, [primary("A", "B")], at(hours=2))

        assert res.key == primary("A", "B").identity

    def test_a_row_is_adopted_by_one_listing_per_poll(self, store: Store) -> None:
        see(store, [primary("A", "B", "C", "D")], T0)

        left, right = see(store, [primary("A", "B"), primary("C", "D")], at(minutes=5))

        assert left.key == primary("A", "B", "C", "D").identity
        assert right.key == primary("C", "D").identity
        assert len(listing_rows(store)) == 2

    def test_an_exact_match_is_not_also_adopted_by_overlap(self, store: Store) -> None:
        see(store, [primary("A", "B")], T0)

        exact, other = see(store, [primary("A", "B"), primary("B", "C")], at(minutes=5))

        assert exact.key == primary("A", "B").identity
        assert other.key == primary("B", "C").identity

    def test_observe_requires_aligned_inputs(self, store: Store) -> None:
        resolved = store.resolve([primary("A")], T0, REARM)

        with pytest.raises(ValueError, match="aligned"):
            store.observe(resolved, [primary("B")], T0)

    def test_key_matches_uses_the_resolved_key(self, store: Store) -> None:
        see(store, [primary("A", "B")], T0)
        moved = match(primary("B", "C"))

        keyed = key_matches([moved], store.resolve([moved.listing], at(minutes=5), REARM))

        assert keyed == [(primary("A", "B").identity, moved)]


class TestDeliveries:
    def test_undelivered_match_is_pending_on_every_channel(self, store: Store) -> None:
        listing = primary("A", "B")

        for channel in Channel:
            assert pending_keys(store, [match(listing)], channel, T0) == [listing.identity]

    def test_pending_is_tracked_per_channel(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, Channel.EMAIL, priority=Priority.NORMAL)

        later = at(minutes=5)
        assert pending_keys(store, [match(listing)], Channel.EMAIL, later) == []
        assert pending_keys(store, [match(listing)], Channel.PUSH, later) == [listing.identity]

    def test_delivery_follows_an_overlapping_listing(self, store: Store) -> None:
        deliver(store, primary("A", "B"), T0, *Channel, priority=Priority.NORMAL)

        assert pending_keys(store, [match(primary("B", "C"))], Channel.PUSH, at(minutes=5)) == []

    @pytest.mark.parametrize(
        ("delivered", "now", "pending"),
        [
            (Priority.NORMAL, Priority.HIGH, True),
            (Priority.NORMAL, Priority.TOP, True),
            (Priority.HIGH, Priority.TOP, True),
            (Priority.HIGH, Priority.HIGH, False),
            (Priority.TOP, Priority.HIGH, False),
            (Priority.HIGH, Priority.NORMAL, False),
        ],
    )
    def test_upgrade_realerts_once(
        self, store: Store, delivered: Priority, now: Priority, pending: bool
    ) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, Channel.PUSH, priority=delivered)

        keys = pending_keys(store, [match(listing, now)], Channel.PUSH, at(minutes=5))

        assert keys == ([listing.identity] if pending else [])

    def test_upgrade_is_not_resent_after_delivery(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, Channel.PUSH, priority=Priority.NORMAL)
        deliver(store, listing, at(minutes=5), Channel.PUSH, priority=Priority.HIGH)

        keys = pending_keys(store, [match(listing, Priority.HIGH)], Channel.PUSH, at(minutes=10))

        assert keys == []

    def test_a_lower_priority_delivery_keeps_the_best(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, Channel.PUSH, priority=Priority.HIGH)
        deliver(store, listing, at(minutes=5), Channel.PUSH, priority=Priority.NORMAL)

        keys = pending_keys(store, [match(listing, Priority.HIGH)], Channel.PUSH, at(minutes=10))

        assert keys == []

    def test_delivered_keys_are_per_channel(self, store: Store) -> None:
        sent, unsent = primary("A", "B"), primary("C", "D")
        deliver(store, sent, T0, Channel.EMAIL, priority=Priority.NORMAL)
        keys = [sent.identity, unsent.identity]

        assert store.delivered_keys(keys, Channel.EMAIL) == {sent.identity}
        assert store.delivered_keys(keys, Channel.PUSH) == set()


def poll(store: Store, when: datetime, outcome: PollOutcome = PollOutcome.OK) -> None:
    """Record a poll that did not see any of the test listings."""
    store.record_poll(PollRecord(started_at=when, outcome=outcome))


class TestRearm:
    def test_listing_gone_for_24h_alerts_again(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, *Channel, priority=Priority.NORMAL)
        poll(store, at(minutes=5))

        [res] = store.resolve([listing], at(hours=25), REARM)
        assert res.rearmed is True

        for channel in Channel:
            assert pending_keys(store, [match(listing)], channel, at(hours=25)) == [res.key]

    def test_absence_counts_from_the_first_poll_without_it(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, *Channel, priority=Priority.NORMAL)
        poll(store, at(minutes=5))

        [early] = store.resolve([listing], at(hours=24), REARM)
        [due] = store.resolve([listing], at(hours=24, minutes=5), REARM)

        assert (early.rearmed, due.rearmed) == (False, True)

    def test_short_absence_does_not_rearm(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, *Channel, priority=Priority.NORMAL)
        poll(store, at(minutes=5))

        [res] = store.resolve([listing], at(hours=2), REARM)

        assert res.rearmed is False
        assert pending_keys(store, [match(listing)], Channel.EMAIL, at(hours=2)) == []

    def test_an_outage_is_not_absence(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, *Channel, priority=Priority.NORMAL)
        for hour in range(31):
            outcome = PollOutcome.BLOCKED if hour % 2 else PollOutcome.ERROR
            poll(store, at(hours=hour, minutes=5), outcome)

        [res] = store.resolve([listing], at(hours=31), REARM)

        assert res.rearmed is False
        assert pending_keys(store, [match(listing)], Channel.PUSH, at(hours=31)) == []

    def test_absence_after_an_outage_counts_from_recovery(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, *Channel, priority=Priority.NORMAL)
        poll(store, at(hours=1), PollOutcome.BLOCKED)
        poll(store, at(hours=30))

        [before] = store.resolve([listing], at(hours=40), REARM)
        [after] = store.resolve([listing], at(hours=54), REARM)

        assert (before.rearmed, after.rearmed) == (False, True)

    def test_continuous_presence_never_rearms(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, *Channel, priority=Priority.NORMAL)
        for hour in range(0, 41, 5):
            poll(store, at(hours=hour))
            see(store, [listing], at(hours=hour))

        assert pending_keys(store, [match(listing)], Channel.EMAIL, at(hours=45)) == []

    def test_rearm_follows_the_adopted_row(self, store: Store) -> None:
        deliver(store, primary("A", "B"), T0, Channel.EMAIL, priority=Priority.NORMAL)
        poll(store, at(minutes=5))

        keys = pending_keys(store, [match(primary("B", "C"))], Channel.EMAIL, at(hours=30))

        assert keys == [primary("A", "B").identity]

    def test_zero_disables_rearming(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, *Channel, priority=Priority.NORMAL)
        poll(store, at(minutes=5))

        [res] = see(store, [listing], at(hours=100), rearm=timedelta(0))

        assert res.rearmed is False
        assert store.pending(key_matches([match(listing)], [res]), Channel.EMAIL) == []

    def test_resale_absence_counts_only_polls_that_captured_resale(self, store: Store) -> None:
        listing = resale("L1")
        deliver(store, listing, T0, *Channel, priority=Priority.NORMAL)
        for hour in range(1, 31):
            missing = ResaleCapture.MISSING if hour % 2 else ResaleCapture.NOT_ELIGIBLE
            store.record_poll(
                PollRecord(at(hours=hour), PollOutcome.PARTIAL, resale_capture=missing)
            )

        [during] = store.resolve([listing], at(hours=31), REARM)
        store.record_poll(ok_poll(at(hours=32)))
        [early] = store.resolve([listing], at(hours=55), REARM)
        [due] = store.resolve([listing], at(hours=56), REARM)

        assert (during.rearmed, early.rearmed, due.rearmed) == (False, False, True)

    def test_primary_absence_counts_partial_polls(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, *Channel, priority=Priority.NORMAL)
        store.record_poll(
            PollRecord(at(hours=1), PollOutcome.PARTIAL, resale_capture=ResaleCapture.MISSING)
        )

        [res] = store.resolve([listing], at(hours=25), REARM)

        assert res.rearmed is True

    def test_a_degraded_parse_is_not_absence(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, *Channel, priority=Priority.NORMAL)
        store.record_poll(ok_poll(at(hours=1), outcome=PollOutcome.PARTIAL, parse_degraded=True))
        poll(store, at(hours=10))

        [early] = store.resolve([listing], at(hours=33), REARM)
        [due] = store.resolve([listing], at(hours=34), REARM)

        assert (early.rearmed, due.rearmed) == (False, True)

    def test_pending_honours_rearm_without_observe(self, store: Store) -> None:
        listing = primary("A", "B")
        deliver(store, listing, T0, *Channel, priority=Priority.NORMAL)
        poll(store, at(minutes=5))

        resolved = store.resolve([listing], at(hours=25), REARM)
        keyed = key_matches([match(listing)], resolved)

        assert store.pending(keyed, Channel.EMAIL) == []
        assert store.pending(keyed, Channel.EMAIL, resolved) == keyed


class TestPolls:
    def test_round_trip(self, store: Store) -> None:
        good = ok_poll(
            T0,
            snapshot=snapshot(30000, {2: 30000, 3: 41000}, {2: None, 3: 38350}),
            primary_labels={2: "Sec 104 Row R", 3: "Sec 105 Row A"},
            resale_labels={3: "Sec 101 Row N"},
            browser_recycled=True,
            parse_warnings=2,
            parse_degraded=True,
        )
        failed = PollRecord(
            started_at=at(minutes=5), outcome=PollOutcome.BLOCKED, detail="HTTP 403"
        )
        store.record_poll(good)
        store.record_poll(failed)

        assert store.recent_polls(10) == [failed, good]

    def test_recent_polls_is_newest_first_and_limited(self, store: Store) -> None:
        for minutes in (0, 10, 5):
            store.record_poll(ok_poll(at(minutes=minutes)))

        polls = store.recent_polls(2)

        assert [poll.started_at for poll in polls] == [at(minutes=10), at(minutes=5)]

    def test_timestamps_come_back_as_aware_utc(self, store: Store) -> None:
        chicago = T0.astimezone(ZoneInfo("America/Chicago"))
        store.record_poll(ok_poll(chicago))

        [poll] = store.recent_polls(1)

        assert poll.started_at == T0
        assert poll.started_at.utcoffset() == timedelta(0)

    def test_baseline_samples_window(self, store: Store) -> None:
        since, until = T0, at(hours=24)
        store.record_poll(ok_poll(since - timedelta(seconds=1), snapshot=snapshot(1)))
        store.record_poll(ok_poll(since, snapshot=snapshot(100)))
        store.record_poll(ok_poll(at(hours=1), outcome=PollOutcome.PARTIAL, snapshot=snapshot(200)))
        store.record_poll(ok_poll(at(hours=2), outcome=PollOutcome.ERROR, snapshot=snapshot(3)))
        store.record_poll(ok_poll(at(hours=3), snapshot=snapshot(None)))
        store.record_poll(ok_poll(at(hours=4), snapshot=None))
        store.record_poll(ok_poll(until, snapshot=snapshot(4)))

        assert store.baseline_samples(since, until) == [100, 200]


class TestStats:
    def test_empty_database(self, store: Store) -> None:
        stats = store.stats_since(T0)

        assert stats.total == 0
        assert set(stats.outcomes) == set(PollOutcome)
        assert stats.last_success_at is None
        assert stats.cheapest_primary == stats.low_resale == {2: None, 3: None}
        assert stats.cheapest_primary_label == stats.cheapest_resale_label == {2: None, 3: None}
        assert stats.resale_listings_now == 0
        assert stats.browser_recycles == 0
        assert (stats.alerts, stats.top_alerts, stats.resale_captured) == (0, 0, 0)
        assert stats.channel_failures == {Channel.EMAIL: 0, Channel.PUSH: 0}

    def test_polls_and_prices(self, store: Store) -> None:
        since = T0
        store.record_poll(
            ok_poll(
                since - DAY,
                snapshot=snapshot(1, {2: 1, 3: 1}, {2: 1, 3: 1}),
                browser_recycled=True,
            )
        )
        store.record_poll(
            ok_poll(
                at(hours=1),
                snapshot=snapshot(500, {2: 500, 3: None}, {2: 800, 3: None}),
                primary_labels={2: "Sec 101 Row A"},
                resale_labels={2: "Sec 102 Row B"},
                browser_recycled=True,
            )
        )
        store.record_poll(
            ok_poll(
                at(hours=2),
                outcome=PollOutcome.PARTIAL,
                resale_capture=ResaleCapture.MISSING,
                resale_listings=3,
                snapshot=snapshot(600, {2: 600, 3: 700}, {2: None, 3: None}),
                primary_labels={2: "Sec 104 Row R", 3: "Sec 105 Row A"},
            )
        )
        for minutes, outcome in [(180, PollOutcome.BLOCKED), (190, PollOutcome.ERROR)]:
            store.record_poll(
                PollRecord(started_at=at(minutes=minutes), outcome=outcome, browser_recycled=True)
            )

        stats = store.stats_since(since)

        assert stats.outcomes == {
            PollOutcome.OK: 1,
            PollOutcome.PARTIAL: 1,
            PollOutcome.BLOCKED: 1,
            PollOutcome.ERROR: 1,
        }
        assert stats.total == 4
        assert stats.last_success_at == at(hours=2)
        assert stats.resale_captured == 1
        assert stats.cheapest_primary == {2: 600, 3: 700}
        assert stats.cheapest_resale == {2: None, 3: None}
        assert stats.cheapest_primary_label == {2: "Sec 104 Row R", 3: "Sec 105 Row A"}
        assert stats.cheapest_resale_label == {2: None, 3: None}
        assert stats.resale_listings_now == 3  # the latest success, not the poll before it
        assert stats.browser_recycles == 3
        assert stats.low_primary == {2: 500, 3: 700}
        assert stats.low_resale == {2: 800, 3: None}

    def test_parser_health_and_missing_resale(self, store: Store) -> None:
        store.record_poll(ok_poll(T0 - DAY, parse_warnings=3, parse_degraded=True))
        store.record_poll(ok_poll(at(hours=1), parse_warnings=0))
        store.record_poll(ok_poll(at(hours=2), parse_warnings=1))
        store.record_poll(
            ok_poll(
                at(hours=3),
                outcome=PollOutcome.PARTIAL,
                resale_capture=ResaleCapture.MISSING,
                parse_warnings=2,
                parse_degraded=True,
            )
        )
        store.record_poll(PollRecord(at(hours=4), PollOutcome.ERROR))

        stats = store.stats_since(T0)

        assert (stats.parse_warning_polls, stats.parse_degraded_polls) == (2, 1)
        assert stats.resale_missing == 1

    def test_last_success_looks_beyond_the_window(self, store: Store) -> None:
        store.record_poll(ok_poll(T0 - DAY))

        assert store.stats_since(T0).last_success_at == T0 - DAY

    def test_alerts_and_channel_failures(self, store: Store) -> None:
        deliver(store, primary("A"), T0 - DAY, Channel.EMAIL, priority=Priority.TOP)
        deliver(store, primary("B"), at(hours=1), *Channel, priority=Priority.TOP)
        deliver(store, primary("C"), at(hours=2), Channel.EMAIL, priority=Priority.NORMAL)
        failures = [
            (NoticeKind.ALERT, Channel.PUSH, False, at(hours=1)),
            (NoticeKind.ALERT, Channel.PUSH, False, at(hours=2)),
            (NoticeKind.ALERT, Channel.EMAIL, True, at(hours=2)),
            (NoticeKind.ALERT, Channel.EMAIL, False, T0 - DAY),
            (NoticeKind.OUTAGE, Channel.EMAIL, False, at(hours=3)),
        ]
        for kind, channel, ok, when in failures:
            store.log_notification(kind, channel, ok, "subject", when)

        stats = store.stats_since(T0)

        assert (stats.alerts, stats.top_alerts) == (2, 1)
        assert stats.channel_failures == {Channel.EMAIL: 0, Channel.PUSH: 2}


class TestNotifications:
    def test_last_alert_attempt_per_channel(self, store: Store) -> None:
        store.log_notification(NoticeKind.ALERT, Channel.EMAIL, True, "a", T0)
        store.log_notification(NoticeKind.ALERT, Channel.EMAIL, False, "b", at(minutes=5))
        store.log_notification(NoticeKind.ALERT, Channel.PUSH, True, "c", at(minutes=5))
        store.log_notification(NoticeKind.CANARY, Channel.PUSH, False, "d", at(minutes=10))

        assert store.last_alert_attempts() == {
            Channel.EMAIL: AlertAttempt(at(minutes=5), ok=False),
            Channel.PUSH: AlertAttempt(at(minutes=5), ok=True),
        }

    def test_no_attempts_yet(self, store: Store) -> None:
        assert store.last_alert_attempts() == {}


class TestRuntimeStatePersistence:
    def test_fresh_database_has_default_state(self, store: Store) -> None:
        assert store.get_state() == RuntimeState()

    def test_round_trip(self, store: Store, reader: Store) -> None:
        state = RuntimeState(
            failure_started_at=T0,
            consecutive_failures=4,
            consecutive_blocks=2,
            profile_rotated_this_streak=True,
            outage_announced=True,
            recovery_pending=True,
            last_outage_duration_s=4321.5,
            last_canary_date=date(2026, 11, 14),
            finished_notice_sent=True,
            resale_eligible=False,
            resale_eligible_notice_sent=True,
            resale_missing_streak=3,
            resale_missing_notice_sent=True,
        )

        store.save_state(state)

        assert store.get_state() == state
        assert reader.get_state() == state

    def test_saving_again_overwrites(self, store: Store) -> None:
        store.save_state(RuntimeState(failure_started_at=T0, consecutive_failures=2))
        store.save_state(RuntimeState())

        assert store.get_state() == RuntimeState()

    def test_unknown_keys_are_ignored(self, store: Store) -> None:
        store._conn.execute("INSERT INTO kv (key, value) VALUES ('retired_field', '1')")

        assert store.get_state() == RuntimeState()


class TestRuntimeStateTransitions:
    def test_failures_accumulate_from_the_first(self) -> None:
        state = (
            RuntimeState()
            .record_failure(PollOutcome.ERROR, T0)
            .record_failure(PollOutcome.BLOCKED, at(minutes=5))
            .record_failure(PollOutcome.BLOCKED, at(minutes=20))
        )

        assert state.failure_started_at == T0
        assert (state.consecutive_failures, state.consecutive_blocks) == (3, 2)

    def test_an_error_resets_the_block_streak(self) -> None:
        state = (
            RuntimeState()
            .record_failure(PollOutcome.BLOCKED, T0)
            .record_failure(PollOutcome.ERROR, at(minutes=15))
        )

        assert (state.consecutive_failures, state.consecutive_blocks) == (2, 0)

    @pytest.mark.parametrize("outcome", [PollOutcome.OK, PollOutcome.PARTIAL])
    def test_success_is_not_a_failure(self, outcome: PollOutcome) -> None:
        with pytest.raises(ValueError, match="not a failure"):
            RuntimeState().record_failure(outcome, T0)

    def test_success_when_healthy_changes_nothing(self) -> None:
        assert RuntimeState().record_success(T0) == RuntimeState()

    def test_one_profile_rotation_per_failure_streak(self) -> None:
        rotated = RuntimeState().record_failure(PollOutcome.BLOCKED, T0).record_profile_rotation()
        blocked_again = rotated.record_failure(PollOutcome.ERROR, at(minutes=5))

        assert rotated.profile_rotated_this_streak
        assert blocked_again.profile_rotated_this_streak
        assert not blocked_again.record_success(at(minutes=10)).profile_rotated_this_streak

    def test_a_profile_rotation_needs_a_failure_streak(self) -> None:
        with pytest.raises(ValueError, match="failure streak"):
            RuntimeState().record_profile_rotation()

    def test_unannounced_outage_recovers_silently(self) -> None:
        state = RuntimeState().record_failure(PollOutcome.ERROR, T0).record_success(at(0, 30))

        assert state == RuntimeState()

    def test_announced_outage_owes_a_recovery_notice(self) -> None:
        state = (
            RuntimeState()
            .record_failure(PollOutcome.BLOCKED, T0)
            .record_outage_notice(delivered=True)
            .record_success(at(hours=2))
        )

        assert state.failure_started_at is None
        assert (state.consecutive_failures, state.outage_announced) == (0, False)
        assert state.recovery_pending is True
        assert state.last_outage_duration_s == 7200

    def test_outage_is_announced_only_when_delivered(self) -> None:
        failing = RuntimeState().record_failure(PollOutcome.ERROR, T0)

        assert failing.record_outage_notice(delivered=False).outage_announced is False
        assert failing.record_outage_notice(delivered=True).outage_announced is True

    def test_recovery_notice_clears_only_when_delivered(self) -> None:
        owed = RuntimeState(recovery_pending=True)

        assert owed.record_recovery_notice(delivered=False).recovery_pending is True
        assert owed.record_recovery_notice(delivered=True).recovery_pending is False

    def test_canary_date_moves_only_after_email_success(self) -> None:
        today = date(2026, 11, 14)

        assert RuntimeState().record_canary(today, email_ok=False).last_canary_date is None
        assert RuntimeState().record_canary(today, email_ok=True).last_canary_date == today

    def test_email_failing_episode_ends_on_a_delivered_email(self) -> None:
        failing = RuntimeState(consecutive_email_failures=2, email_failing_notice_sent=True)

        assert failing.record_email(ok=False).consecutive_email_failures == 3
        assert failing.record_email(ok=False).email_failing_notice_sent is True
        assert failing.record_email(ok=True) == RuntimeState()


class TestConcurrency:
    def test_reader_works_while_writer_commits(
        self, store: Store, reader: Store, db_path: Path
    ) -> None:
        store.close()
        done = threading.Event()
        errors: list[BaseException] = []

        def write() -> None:
            try:
                with Store.open_writer(db_path) as writer:
                    for n in range(200):
                        with writer.transaction():
                            writer.record_poll(ok_poll(at(minutes=n)))
                            writer.save_state(RuntimeState(consecutive_failures=n))
            except BaseException as exc:  # surfaced to the main thread below
                errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=write)
        thread.start()
        reads = 0
        while not done.is_set() or reads == 0:
            reader.stats_since(T0)
            reader.recent_polls(5)
            reader.get_state()
            reads += 1
        thread.join()

        assert errors == []
        assert reader.stats_since(T0).total == 200
        assert reader.get_state().consecutive_failures == 199

    def test_reader_is_not_blocked_by_an_open_write_transaction(
        self, store: Store, reader: Store
    ) -> None:
        store.record_poll(ok_poll(T0))

        with store.transaction():
            store.record_poll(ok_poll(at(minutes=5)))
            assert len(reader.recent_polls(10)) == 1

        assert len(reader.recent_polls(10)) == 2


def test_open_empty_is_a_migrated_store_without_history() -> None:
    with Store.open_empty() as store:
        assert not store.persistent
        assert store.get_state() == RuntimeState()
        assert store.recent_polls(1) == []
