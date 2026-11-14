import dataclasses
from decimal import Decimal

import pytest

from ticket_poller.models import Baseline, InventoryType, Listing, Match, Priority, Reason


def primary(
    places: frozenset[str] = frozenset({"GEYDIOSSHI4Q", "GEYDIOSSHI4A"}),
    offer_id: str = "OFFER1",
    **changes: object,
) -> Listing:
    listing = Listing(
        inventory_type=InventoryType.PRIMARY,
        offer_id=offer_id,
        listing_id=None,
        section="104",
        row="R",
        places=places,
        seats="9-10",
        price_cents=34350,
        fees_cents=6185,
        buyable_quantities=frozenset({2}),
        max_together=2,
    )
    return dataclasses.replace(listing, **changes)  # type: ignore[arg-type]


def resale(listing_id: str = "L1") -> Listing:
    return Listing(
        inventory_type=InventoryType.RESALE,
        offer_id="RES-OFFER",
        listing_id=listing_id,
        section="101",
        row="N",
        places=frozenset(),
        seats="12-14",
        price_cents=38350,
        fees_cents=0,
        buyable_quantities=frozenset({1, 3}),
        max_together=None,
    )


def test_primary_identity_is_offer_plus_sorted_places() -> None:
    assert primary().identity == "primary:OFFER1:GEYDIOSSHI4A,GEYDIOSSHI4Q"


def test_primary_identity_is_independent_of_place_order() -> None:
    ids = ["C", "A", "B"]
    first = primary(places=frozenset(ids))
    second = primary(places=frozenset(reversed(ids)))
    assert first.identity == second.identity == "primary:OFFER1:A,B,C"


def test_primary_identity_ignores_price_label_and_quantities() -> None:
    changed = primary(price_cents=1, seats="x", buyable_quantities=frozenset({2, 3}))
    assert changed.identity == primary().identity


def test_primary_identity_differs_by_offer_and_places() -> None:
    base = primary()
    assert primary(offer_id="OFFER2").identity != base.identity
    assert primary(places=frozenset({"GEYDIOSSHI4Q"})).identity != base.identity


def test_resale_identity_uses_listing_id() -> None:
    assert resale("abc123").identity == "resale:abc123"


@pytest.mark.parametrize(
    ("inventory_type", "listing_id"),
    [(InventoryType.RESALE, None), (InventoryType.RESALE, ""), (InventoryType.PRIMARY, "L1")],
)
def test_listing_id_must_match_inventory_type(
    inventory_type: InventoryType, listing_id: str | None
) -> None:
    with pytest.raises(ValueError, match="listing_id"):
        primary(inventory_type=inventory_type, listing_id=listing_id)


def test_all_in_and_buyable_as() -> None:
    listing = primary(buyable_quantities=frozenset({2, 3}))
    assert listing.all_in_cents == 40535
    assert listing.buyable_as(3)
    assert not listing.buyable_as(4)


def test_priority_orders_top_first() -> None:
    assert Priority.TOP < Priority.HIGH < Priority.NORMAL
    assert sorted([Priority.NORMAL, Priority.TOP, Priority.HIGH]) == [
        Priority.TOP,
        Priority.HIGH,
        Priority.NORMAL,
    ]
    assert [int(p) for p in Priority] == [1, 2, 3]


def test_enum_values_match_stored_text() -> None:
    assert InventoryType.RESALE.value == "resale"
    assert Reason.PREFERRED_DEAL.value == "preferred_deal"


def test_models_are_immutable_and_hashable() -> None:
    listing = primary()
    with pytest.raises(dataclasses.FrozenInstanceError):
        listing.price_cents = 1  # type: ignore[misc]
    assert hash(listing) == hash(primary())
    assert len({listing, primary()}) == 1

    match = Match(listing, frozenset({Reason.UNDER_PRICE_CAP}), Priority.NORMAL, False, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        match.priority = Priority.TOP  # type: ignore[misc]


def test_models_are_slotted() -> None:
    baseline = Baseline(median_cents=Decimal("34350.5"), samples=12, ready=True)
    assert not hasattr(baseline, "__dict__")
    with pytest.raises((AttributeError, TypeError)):
        baseline.extra = 1  # type: ignore[attr-defined]
