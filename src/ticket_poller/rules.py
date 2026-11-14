"""Pure alert rules: candidates, baseline, triggers, priority and price snapshot (design §6).

Triggers decide *whether* a listing alerts; priority decides *how loudly*. All money
comparisons are exact: integer cents, and a ``Decimal`` median.
"""

import statistics
from collections.abc import Iterable, Sequence
from decimal import Decimal

from ticket_poller.config import RuleConfig
from ticket_poller.models import (
    Baseline,
    Inventory,
    InventoryType,
    Listing,
    Match,
    PriceSnapshot,
    Priority,
    Reason,
)


def is_candidate(listing: Listing, rules: RuleConfig) -> bool:
    """Buyable as exactly one of the wanted quantities."""
    return not listing.buyable_quantities.isdisjoint(rules.wanted_quantities)


def candidates(inventory: Inventory, rules: RuleConfig) -> list[Listing]:
    return [
        listing
        for listing in (*inventory.primary, *inventory.resale)
        if is_candidate(listing, rules)
    ]


def compute_baseline(samples: Sequence[int], min_samples: int) -> Baseline:
    """The standard median of the samples (cents); ready once warm-up is over."""
    if not samples:
        return Baseline(median_cents=None, samples=0, ready=False)
    median = statistics.median(Decimal(sample) for sample in samples)
    return Baseline(median_cents=median, samples=len(samples), ready=len(samples) >= min_samples)


def triggers(listing: Listing, baseline: Baseline, rules: RuleConfig) -> frozenset[Reason]:
    """Every reason the listing alerts; empty if it doesn't (design §6.2 and §6.3)."""
    if not is_candidate(listing, rules):
        return frozenset()
    if listing.inventory_type is InventoryType.RESALE:
        return frozenset({Reason.RESALE_LISTED})
    price = listing.price_cents
    reasons: set[Reason] = set()
    if price <= rules.primary_alert_max_price_cents:
        reasons.add(Reason.UNDER_PRICE_CAP)
    if (
        baseline.ready
        and baseline.median_cents is not None
        and Decimal(price) * 100 <= baseline.median_cents * (100 - rules.primary_drop_percent)
    ):
        reasons.add(Reason.BELOW_BASELINE)
    if _preferred_quantity(listing, rules) and price <= rules.preferred_max_price_cents:
        reasons.add(Reason.PREFERRED_DEAL)
    return frozenset(reasons)


def classify_priority(listing: Listing, rules: RuleConfig) -> Priority:
    if _preferred_quantity(listing, rules):
        if listing.price_cents <= rules.preferred_max_price_cents:
            return Priority.TOP
        return Priority.HIGH
    return Priority.NORMAL


def match_sort_key(match: Match) -> tuple[Priority, bool, int, str]:
    """Most important first: priority, then within the preferred price, cheapest, identity."""
    return (
        match.priority,
        not match.within_preferred_price,
        match.listing.price_cents,
        match.listing.identity,
    )


def evaluate(inventory: Inventory, baseline: Baseline, rules: RuleConfig) -> list[Match]:
    """All triggered candidates as matches, sorted by :func:`match_sort_key`."""
    matches = [
        Match(
            listing=listing,
            reasons=reasons,
            priority=classify_priority(listing, rules),
            within_preferred_price=listing.price_cents <= rules.preferred_max_price_cents,
            pct_below_baseline=pct_below(listing.price_cents, baseline),
        )
        for listing in candidates(inventory, rules)
        if (reasons := triggers(listing, baseline, rules))
    ]
    return sorted(matches, key=match_sort_key)


def pct_below(price_cents: int, baseline: Baseline) -> Decimal | None:
    """``(median - price) / median`` as a fraction, once the baseline is ready."""
    if not baseline.ready or not baseline.median_cents:
        return None
    return (baseline.median_cents - price_cents) / baseline.median_cents


def price_snapshot(inventory: Inventory, rules: RuleConfig) -> PriceSnapshot:
    """Cheapest prices this poll: the baseline sample and per wanted quantity."""
    wanted = sorted(rules.wanted_quantities)
    return PriceSnapshot(
        baseline_sample_cents=_cheapest(
            listing for listing in inventory.primary if is_candidate(listing, rules)
        ),
        primary_by_qty={q: _cheapest_buyable_as(inventory.primary, q) for q in wanted},
        resale_by_qty={q: _cheapest_buyable_as(inventory.resale, q) for q in wanted},
    )


def _preferred_quantity(listing: Listing, rules: RuleConfig) -> bool:
    return not listing.buyable_quantities.isdisjoint(rules.preferred_quantities)


def _cheapest(listings: Iterable[Listing]) -> int | None:
    return min((listing.price_cents for listing in listings), default=None)


def _cheapest_buyable_as(listings: Iterable[Listing], quantity: int) -> int | None:
    return _cheapest(listing for listing in listings if listing.buyable_as(quantity))
