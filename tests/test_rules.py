from dataclasses import replace
from decimal import Decimal

import pytest

from tests.builders import RULES, load_captured, make_listing
from ticket_poller.models import Baseline, Inventory, Listing, Priority, Reason
from ticket_poller.parsing import parse_inventory
from ticket_poller.rules import (
    candidates,
    classify_priority,
    compute_baseline,
    evaluate,
    pct_below,
    price_snapshot,
    triggers,
)

NO_BASELINE = Baseline(median_cents=None, samples=0, ready=False)


def ready(median_cents: int | str) -> Baseline:
    return Baseline(median_cents=Decimal(median_cents), samples=12, ready=True)


def inventory(*listings: Listing) -> Inventory:
    return Inventory(
        primary=tuple(lst for lst in listings if lst.listing_id is None),
        resale=tuple(lst for lst in listings if lst.listing_id is not None),
        inferred_quantities=frozenset(),
        resale_requested=False,
        warnings=(),
    )


def nk_inventory() -> Inventory:
    names = ["nk_quickpicks_qty2", "nk_quickpicks_qty3", "nk_facets_inventorytypes_offer"]
    return parse_inventory([load_captured(name) for name in names], RULES.wanted_quantities)


# --- candidates ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quantities", "expected"),
    [({1, 3}, True), ({1}, False), ({4}, False), ({2, 4}, True), ({2}, True), (set(), False)],
)
def test_candidates_need_a_wanted_quantity(quantities: set[int], *, expected: bool) -> None:
    primary = make_listing(50_000, quantities, key="P")
    resale = make_listing(50_000, quantities, resale=True, key="R")

    assert candidates(inventory(primary, resale), RULES) == ([primary, resale] if expected else [])


# --- baseline ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("samples", "median"),
    [([1, 2, 3, 4], "2.5"), ([5, 1, 3], "3"), ([7], "7"), ([30_000, 30_001], "30000.5")],
)
def test_baseline_is_the_standard_decimal_median(samples: list[int], median: str) -> None:
    baseline = compute_baseline(samples, min_samples=1)

    assert baseline.median_cents == Decimal(median)
    assert isinstance(baseline.median_cents, Decimal)
    assert baseline.samples == len(samples)


def test_empty_baseline_is_not_ready() -> None:
    assert compute_baseline([], min_samples=0) == NO_BASELINE


@pytest.mark.parametrize(("count", "is_ready"), [(11, False), (12, True)])
def test_baseline_warm_up(count: int, *, is_ready: bool) -> None:
    baseline = compute_baseline([30_000] * count, RULES.baseline_min_samples)
    listing = make_listing(24_000, {2})

    assert baseline.ready is is_ready
    assert (Reason.BELOW_BASELINE in triggers(listing, baseline, RULES)) is is_ready


# --- triggers ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "reasons"),
    [(20_000, {Reason.UNDER_PRICE_CAP}), (20_001, set()), (19_999, {Reason.UNDER_PRICE_CAP})],
)
def test_price_cap_is_inclusive(price: int, reasons: set[Reason]) -> None:
    assert triggers(make_listing(price, {2}), NO_BASELINE, RULES) == reasons


def test_preferred_deal_at_300_00_alerts_as_top() -> None:
    listing = make_listing(30_000, {3})

    [match] = evaluate(inventory(listing), NO_BASELINE, RULES)

    assert match.reasons == {Reason.PREFERRED_DEAL}
    assert match.priority is Priority.TOP
    assert match.within_preferred_price is True


@pytest.mark.parametrize(
    ("price", "quantities"),
    [(30_001, {3}), (25_000, {2}), (25_000, {2, 4})],
    ids=["triple-at-300.01", "pair-at-250", "pair-or-four-at-250"],
)
def test_no_preferred_deal_above_300_or_without_3(price: int, quantities: set[int]) -> None:
    assert evaluate(inventory(make_listing(price, quantities)), NO_BASELINE, RULES) == []


@pytest.mark.parametrize(
    ("median", "price", "below"),
    [
        (30_000, 24_000, True),  # exactly 20 % below
        (30_000, 24_001, False),
        ("30000.5", 24_000, True),  # threshold 24000.4: compared exactly, no rounding
        ("30000.5", 24_001, False),
    ],
)
def test_below_baseline_is_exact(median: int | str, price: int, *, below: bool) -> None:
    reasons = triggers(make_listing(price, {2}), ready(median), RULES)

    assert (Reason.BELOW_BASELINE in reasons) is below


def test_below_baseline_needs_a_ready_baseline() -> None:
    not_ready = Baseline(median_cents=Decimal(50_000), samples=3, ready=False)

    assert triggers(make_listing(24_000, {2}), not_ready, RULES) == frozenset()


def test_resale_always_matches_regardless_of_price() -> None:
    listing = make_listing(99_999, {2, 4}, resale=True)

    assert triggers(listing, ready(10_000), RULES) == {Reason.RESALE_LISTED}


def test_resale_not_buyable_as_2_or_3_does_not_match() -> None:
    assert triggers(make_listing(10_000, {1, 4}, resale=True), NO_BASELINE, RULES) == frozenset()


def test_reasons_combine() -> None:
    reasons = triggers(make_listing(15_000, {2, 3}), ready(30_000), RULES)

    assert reasons == {Reason.UNDER_PRICE_CAP, Reason.BELOW_BASELINE, Reason.PREFERRED_DEAL}


def test_pct_below_baseline_is_a_fraction_when_ready() -> None:
    assert pct_below(24_000, ready(30_000)) == Decimal("0.2")
    assert pct_below(36_000, ready(30_000)) == Decimal("-0.2")
    assert pct_below(24_000, NO_BASELINE) is None


# --- priority ------------------------------------------------------------------------------


@pytest.mark.parametrize("quantities", [{3}, {2, 3}, {1, 3}])
@pytest.mark.parametrize(("price", "priority"), [(30_000, Priority.TOP), (30_001, Priority.HIGH)])
def test_buyable_as_3_is_top_or_high(quantities: set[int], price: int, priority: Priority) -> None:
    assert classify_priority(make_listing(price, quantities), RULES) is priority


@pytest.mark.parametrize("quantities", [{2}, {2, 4}])
@pytest.mark.parametrize("price", [10_000, 30_000, 50_000])
def test_pair_only_is_normal(quantities: set[int], price: int) -> None:
    assert classify_priority(make_listing(price, quantities), RULES) is Priority.NORMAL


def test_empty_preferred_quantities_make_everything_normal() -> None:
    rules = replace(RULES, preferred_quantities=frozenset())

    assert classify_priority(make_listing(10_000, {3}), rules) is Priority.NORMAL


def test_matches_sort_top_high_preferred_pair_then_pair() -> None:
    pair = make_listing(35_000, {2}, resale=True, key="pair")
    cheap_pair = make_listing(25_000, {2}, resale=True, key="cheap-pair")
    triple = make_listing(40_000, {3}, resale=True, key="triple")
    deal = make_listing(29_000, {2, 3}, resale=True, key="deal")
    deal_twin = make_listing(29_000, {3}, resale=True, key="a-deal")

    matches = evaluate(inventory(pair, cheap_pair, triple, deal, deal_twin), NO_BASELINE, RULES)

    assert [m.listing for m in matches] == [deal_twin, deal, triple, cheap_pair, pair]
    assert [m.priority for m in matches] == [
        Priority.TOP,
        Priority.TOP,
        Priority.HIGH,
        Priority.NORMAL,
        Priority.NORMAL,
    ]
    assert [m.within_preferred_price for m in matches] == [True, True, False, True, False]


# --- NK fixtures end to end ----------------------------------------------------------------


def test_price_snapshot_of_nk_fixtures() -> None:
    snapshot = price_snapshot(nk_inventory(), RULES)

    assert snapshot.baseline_sample_cents == 34_350
    assert snapshot.primary_by_qty == {2: 34_350, 3: 38_350}
    assert snapshot.resale_by_qty == {2: None, 3: None}


def test_price_snapshot_of_empty_inventory() -> None:
    snapshot = price_snapshot(inventory(), RULES)

    assert snapshot.baseline_sample_cents is None
    assert snapshot.primary_by_qty == {2: None, 3: None}


def test_nk_fixtures_without_a_baseline_do_not_alert() -> None:
    assert evaluate(nk_inventory(), NO_BASELINE, RULES) == []


def test_nk_fixtures_against_a_higher_median() -> None:
    matches = evaluate(nk_inventory(), ready(48_000), RULES)  # threshold $384.00

    assert [(m.listing.section, m.listing.row, m.priority) for m in matches] == [
        ("101", "N", Priority.HIGH),
        ("123", "M", Priority.HIGH),
        ("104", "R", Priority.NORMAL),
        ("103", "M", Priority.NORMAL),
        ("104", "Q", Priority.NORMAL),
    ]
    assert all(m.reasons == {Reason.BELOW_BASELINE} for m in matches)
    assert matches[2].pct_below_baseline == Decimal("0.284375")
