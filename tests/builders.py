"""Fixture loading and small builders for synthetic ismds payloads and listings."""

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ticket_poller.config import RuleConfig
from ticket_poller.models import CapturedResponse, InventoryType, Listing

FIXTURES = Path(__file__).parent / "fixtures"
EVENT_API = "https://services.ticketmaster.com/api/ismds/event/2000655CAB442FB5"

type Body = dict[str, Any]

RULES = RuleConfig(
    wanted_quantities=frozenset({2, 3}),
    preferred_quantities=frozenset({3}),
    primary_alert_max_price_cents=20_000,
    primary_drop_percent=20,
    preferred_max_price_cents=30_000,
    baseline_min_samples=12,
)


def load_captured(name: str) -> CapturedResponse:
    """A fixture from ``tests/fixtures/{name}.json`` as a 200 response from its ``_source_url``."""
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return CapturedResponse(url=data["_source_url"], status=200, body=data["response"])


def make_offer(
    offer_id: str = "OFFER1",
    list_price: float = 383.5,
    fee: float = 69.05,
    *,
    inventory_type: str = "primary",
    sellable: Sequence[int] = (1, 2, 3, 4, 5, 6),
    **extra: Any,
) -> Body:
    return {
        "offerId": offer_id,
        "inventoryType": inventory_type,
        "online": True,
        "protected": False,
        "listPrice": list_price,
        "charges": [
            {"reason": "service", "type": "fee", "amount": fee},
            {"reason": "face_value_tax", "type": "tax", "amount": 37.95},
            {"reason": "order_processing", "type": "fee", "amount": 0.0},
        ],
        "sellableQuantities": list(sellable),
        **extra,
    }


def make_resale_offer(
    listing_id: str = "L1",
    sellable: Sequence[int] = (2,),
    list_price: float = 452.0,
    **extra: Any,
) -> Body:
    return make_offer(
        f"RES-{listing_id}",
        list_price,
        122.04,
        inventory_type="resale",
        sellable=sellable,
        listingId=listing_id,
        section="101",
        row="7",
        seatFrom="11",
        seatTo="12",
        **extra,
    )


def make_group(seats: Iterable[int], offers: Sequence[str] = ("OFFER1",), row: str = "N") -> Body:
    """One ``offerGroups`` entry; place IDs are readable stand-ins like ``101:N:12``."""
    labels = [str(seat) for seat in seats]
    return {
        "offers": list(offers),
        "places": [f"101:{row}:{seat}" for seat in labels],
        "seats": labels,
    }


def make_pick(*groups: Body, section: str = "101", row: str = "N", max_quantity: int = 2) -> Body:
    return {
        "type": "seat",
        "section": section,
        "row": row,
        "maxQuantity": max_quantity,
        "offerGroups": list(groups),
    }


def quickpicks(
    qty: int,
    picks: Sequence[Body],
    offers: Sequence[Body] = (),
    *,
    total: int | None = None,
    offset: int = 0,
) -> CapturedResponse:
    body: Body = {
        "offset": offset,
        "total": len(picks) if total is None else total,
        "picks": list(picks),
        "_embedded": {"offer": list(offers)},
    }
    url = f"{EVENT_API}/quickpicks?qty={qty}&includeResale=false&limit=40&offset={offset}"
    return CapturedResponse(url=url, status=200, body=body)


def facets(
    offers: Sequence[Body] = (), facet_list: Sequence[Mapping[str, Any]] = ()
) -> CapturedResponse:
    body: Body = {"facets": list(facet_list), "_embedded": {"offer": list(offers)}}
    url = f"{EVENT_API}/facets?by=inventorytypes+offer&q=available&embed=offer"
    return CapturedResponse(url=url, status=200, body=body)


def make_listing(
    price_cents: int,
    quantities: Iterable[int],
    *,
    resale: bool = False,
    key: str = "A",
) -> Listing:
    return Listing(
        inventory_type=InventoryType.RESALE if resale else InventoryType.PRIMARY,
        offer_id=f"OFFER-{key}",
        listing_id=key if resale else None,
        section="101",
        row="N",
        places=frozenset() if resale else frozenset({f"place-{key}"}),
        seats="1\u20132",
        price_cents=price_cents,
        fees_cents=0,
        buyable_quantities=frozenset(quantities),
        max_together=None,
    )
