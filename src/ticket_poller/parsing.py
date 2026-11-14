"""Pure parsing of captured ismds JSON into an :class:`Inventory` (design §4 and §7).

Primary listings come only from quickpicks: each ``offerGroups`` entry is a seat group
buyable as exactly the payload's ``qty``. Groups under the same offer whose places
overlap or nest are merged into one canonical listing; disjoint groups stay separate.
Resale listings come from any body's ``_embedded.offer``, de-duplicated by ``listingId``.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import reduce
from typing import Any, Final, Self
from urllib.parse import parse_qs, urlsplit

from ticket_poller.config import dollars_to_cents
from ticket_poller.models import (
    CapturedResponse,
    Inventory,
    InventoryType,
    Listing,
    ResaleCapture,
)

type Json = Mapping[str, Any]

EN_DASH: Final = "\u2013"
_RESALE_QUERY: Final = re.compile(r'inventorytypes\s*:\s*"?resale\b', re.IGNORECASE)


class ParseError(ValueError):
    """A captured ismds body is structurally invalid."""


@dataclass(frozen=True, slots=True)
class SeatGroup:
    """Adjacent seats under one offer, buyable as each of ``quantities``.

    Built from one quickpicks ``offerGroups`` entry (one quantity), or merged from several.
    """

    offer_id: str
    section: str
    row: str
    places: frozenset[str]
    seats: frozenset[str]
    quantities: frozenset[int]
    max_quantity: int | None

    def overlaps(self, other: Self) -> bool:
        """Same offer and at least one shared place (overlapping or nested groups)."""
        return self.offer_id == other.offer_id and not self.places.isdisjoint(other.places)

    def union(self, other: Self) -> Self:
        maxima = [m for m in (self.max_quantity, other.max_quantity) if m is not None]
        return replace(
            self,
            places=self.places | other.places,
            seats=self.seats | other.seats,
            quantities=self.quantities | other.quantities,
            max_quantity=max(maxima, default=None),
        )


def merge_seat_groups(groups: Iterable[SeatGroup]) -> list[SeatGroup]:
    """Merge groups that share an offer and overlap or nest, transitively.

    Disjoint groups stay separate even in the same row at the same price. The result
    keeps the order in which each merged group first appeared.
    """
    merged: list[SeatGroup] = []
    for group in groups:
        hits = [i for i, existing in enumerate(merged) if existing.overlaps(group)]
        if not hits:
            merged.append(group)
            continue
        first, *rest = hits
        merged[first] = reduce(SeatGroup.union, [*(merged[i] for i in rest), group], merged[first])
        for i in reversed(rest):
            del merged[i]
    return merged


def seat_label(seats: Iterable[str]) -> str:
    """``5`` en dash ``7`` for a contiguous numeric run, otherwise e.g. ``"9, 11"``."""
    ordered = sorted(set(seats), key=_seat_sort_key)
    if len(ordered) > 1 and all(seat.isdigit() for seat in ordered):
        numbers = [int(seat) for seat in ordered]
        if numbers[-1] - numbers[0] == len(numbers) - 1:
            return f"{ordered[0]}{EN_DASH}{ordered[-1]}"
    return ", ".join(ordered)


def resale_requested(urls: Iterable[str]) -> bool:
    """Whether any request asked for resale inventory (design §7)."""
    return any(_requests_resale(url) for url in urls)


def resale_capture(inventory: Inventory, eligible: bool | None) -> ResaleCapture:
    """The poll's resale-capture status from the page's eligibility flag (design §7).

    Resale that was requested or seen counts as captured even if the flag says
    "not eligible"; the caller may log that anomaly.
    """
    if inventory.resale_requested or inventory.resale:
        return ResaleCapture.CAPTURED
    if eligible is None:
        return ResaleCapture.UNKNOWN
    return ResaleCapture.MISSING if eligible else ResaleCapture.NOT_ELIGIBLE


def parse_inventory(responses: Iterable[CapturedResponse], wanted: frozenset[int]) -> Inventory:
    """Parse every captured 200 JSON body into primary and resale listings.

    Raises :class:`ParseError` only for structurally invalid payloads; recoverable
    oddities (missing offers, truncation, inferred quantities) become warnings.
    """
    captured = [(r.url, r.body) for r in responses if r.status == 200 and r.body is not None]
    warnings: list[str] = []

    offers: dict[str, Json] = {}
    for _, body in captured:
        for offer in _embedded_offers(body):
            offers.setdefault(_str(offer, "offerId"), offer)

    groups: list[SeatGroup] = []
    quickpick_qtys: set[int] = set()
    for url, body in captured:
        if not _is_quickpicks(url):
            continue
        qty = _qty_param(url)
        if qty is None:
            warnings.append("quickpicks response without a qty parameter ignored")
            continue
        quickpick_qtys.add(qty)
        groups.extend(_quickpick_groups(body, qty, offers, warnings))

    inferred = wanted - quickpick_qtys
    for qty in sorted(inferred):
        warnings.append(f"no quickpicks for qty={qty}; inferred from maxQuantity")
    primary = tuple(
        _primary_listing(_infer_quantities(group, inferred, offers[group.offer_id]), offers)
        for group in merge_seat_groups(groups)
    )

    resale = _resale_listings(offers.values(), warnings)
    if not resale and any(_has_resale_facet(body) for _, body in captured):
        warnings.append("resale facets seen without resale offers")

    return Inventory(
        primary=primary,
        resale=resale,
        inferred_quantities=inferred,
        resale_requested=resale_requested(url for url, _ in captured),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _quickpick_groups(
    body: Json, qty: int, offers: Mapping[str, Json], warnings: list[str]
) -> list[SeatGroup]:
    picks = _list(body, "picks")
    total, offset = body.get("total"), body.get("offset", 0)
    if _is_int(total) and _is_int(offset) and total > offset + len(picks):
        warnings.append(f"quickpicks truncated (qty={qty}: {len(picks)} of {total})")

    groups: list[SeatGroup] = []
    for raw_pick in picks:
        pick = _mapping(raw_pick, "quickpicks pick")
        section, row = _str(pick, "section"), _opt_str(pick, "row")
        max_quantity = pick.get("maxQuantity")
        for raw_group in _list(pick, "offerGroups"):
            offer_group = _mapping(raw_group, "quickpicks offerGroup")
            offer_ids = _str_list(offer_group, "offers")
            known = [offer_id for offer_id in offer_ids if offer_id in offers]
            if not known:
                warnings.append(f"quickpicks offer missing for {section} {row}; group skipped")
                continue
            offer_id = next((o for o in known if _is_buyable_primary(offers[o])), None)
            if offer_id is None:
                continue
            places = frozenset(_str_list(offer_group, "places"))
            if not places:
                warnings.append(f"quickpicks group without places for {section} {row}; skipped")
                continue
            groups.append(
                SeatGroup(
                    offer_id=offer_id,
                    section=section,
                    row=row,
                    places=places,
                    seats=frozenset(_str_list(offer_group, "seats")),
                    quantities=frozenset({qty}),
                    max_quantity=max_quantity if _is_int(max_quantity) else None,
                )
            )
    return groups


def _infer_quantities(group: SeatGroup, missing: frozenset[int], offer: Json) -> SeatGroup:
    """Qty fallback: add each missing quantity the group's maxQuantity and offer allow."""
    if group.max_quantity is None:
        return group
    sellable = set(_int_list(offer, "sellableQuantities"))
    extra = {q for q in missing if q <= group.max_quantity and q in sellable}
    return replace(group, quantities=group.quantities | extra) if extra else group


def _primary_listing(group: SeatGroup, offers: Mapping[str, Json]) -> Listing:
    offer = offers[group.offer_id]
    return Listing(
        inventory_type=InventoryType.PRIMARY,
        offer_id=group.offer_id,
        listing_id=None,
        section=group.section,
        row=group.row,
        places=group.places,
        seats=seat_label(group.seats),
        price_cents=_list_price_cents(offer),
        fees_cents=_fees_cents(offer),
        buyable_quantities=group.quantities,
        max_together=group.max_quantity,
    )


def _resale_listings(offers: Iterable[Json], warnings: list[str]) -> tuple[Listing, ...]:
    listings: dict[str, Listing] = {}
    for offer in offers:
        inventory_type = offer.get("inventoryType")
        if inventory_type == InventoryType.RESALE:
            if _is_buyable(offer):
                listing = _resale_listing(offer)
                listings.setdefault(_str(offer, "listingId"), listing)
        elif inventory_type != InventoryType.PRIMARY:
            warnings.append(f"ignored offer with inventoryType {inventory_type!r}")
    return tuple(listings.values())


def _resale_listing(offer: Json) -> Listing:
    seat_from, seat_to = _opt_str(offer, "seatFrom"), _opt_str(offer, "seatTo")
    return Listing(
        inventory_type=InventoryType.RESALE,
        offer_id=_str(offer, "offerId"),
        listing_id=_str(offer, "listingId"),
        section=_str(offer, "section"),
        row=_opt_str(offer, "row"),
        places=frozenset(),
        seats=seat_from if seat_from == seat_to else f"{seat_from}{EN_DASH}{seat_to}",
        price_cents=_list_price_cents(offer),
        fees_cents=_fees_cents(offer),
        buyable_quantities=frozenset(_int_list(offer, "sellableQuantities")),
        max_together=None,
    )


def _is_buyable(offer: Json) -> bool:
    return offer.get("protected") is not True and offer.get("online") is not False


def _is_buyable_primary(offer: Json) -> bool:
    is_primary = offer.get("inventoryType", InventoryType.PRIMARY) == InventoryType.PRIMARY
    return is_primary and _is_buyable(offer)


def _has_resale_facet(body: Json) -> bool:
    facets = body.get("facets")
    if not isinstance(facets, list):
        return False
    return any(
        isinstance(facet, Mapping) and InventoryType.RESALE in (facet.get("inventoryTypes") or ())
        for facet in facets
    )


def _requests_resale(url: str) -> bool:
    parts = urlsplit(url)
    if (parts.hostname or "").startswith("offeradapter."):
        return True
    params = parse_qs(parts.query)
    if any(value.lower() == "true" for value in params.get("includeResale", [])):
        return True
    return any(_RESALE_QUERY.search(query) for query in params.get("q", []))


def _is_quickpicks(url: str) -> bool:
    return urlsplit(url).path.rstrip("/").endswith("/quickpicks")


def _qty_param(url: str) -> int | None:
    values = parse_qs(urlsplit(url).query).get("qty", [])
    return int(values[0]) if len(values) == 1 and values[0].isdigit() else None


def _seat_sort_key(seat: str) -> tuple[int, int, str]:
    return (0, int(seat), "") if seat.isdigit() else (1, 0, seat)


# --- money -------------------------------------------------------------------------------


def _cents(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ParseError(f"{what}: expected a number, got {type(value).__name__}")
    try:
        return dollars_to_cents(Decimal(str(value)))
    except ArithmeticError as exc:
        raise ParseError(f"{what}: invalid amount {value!r}") from exc


def _list_price_cents(offer: Json) -> int:
    return _cents(offer.get("listPrice"), "offer.listPrice")


def _fees_cents(offer: Json) -> int:
    charges = offer.get("charges", [])
    if not isinstance(charges, list):
        raise ParseError("offer.charges: expected a list")
    return sum(
        _cents(charge.get("amount"), "offer.charges[].amount")
        for charge in (_mapping(c, "offer.charges[]") for c in charges)
        if charge.get("type") == "fee"
    )


# --- structural accessors ----------------------------------------------------------------


def _embedded_offers(body: Json) -> list[Json]:
    embedded = body.get("_embedded")
    if embedded is None:
        return []
    raw = _mapping(embedded, "_embedded").get("offer")
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    if not isinstance(raw, list):
        raise ParseError("_embedded.offer: expected a list or an object")
    return [_mapping(offer, "_embedded.offer[]") for offer in raw]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _mapping(value: object, what: str) -> Json:
    if not isinstance(value, Mapping):
        raise ParseError(f"{what}: expected an object, got {type(value).__name__}")
    return value


def _list(obj: Json, key: str) -> Sequence[Any]:
    value = obj.get(key)
    if not isinstance(value, list):
        raise ParseError(f"{key!r}: expected a list, got {type(value).__name__}")
    return value


def _str(obj: Json, key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ParseError(f"{key!r}: expected a non-empty string, got {value!r}")
    return value


def _opt_str(obj: Json, key: str) -> str:
    value = obj.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ParseError(f"{key!r}: expected a string, got {type(value).__name__}")
    return value


def _str_list(obj: Json, key: str) -> list[str]:
    values = _list(obj, key)
    if not all(isinstance(value, str) for value in values):
        raise ParseError(f"{key!r}: expected a list of strings")
    return list(values)


def _int_list(obj: Json, key: str) -> list[int]:
    values = obj.get(key, [])
    if not isinstance(values, list) or not all(_is_int(value) for value in values):
        raise ParseError(f"{key!r}: expected a list of integers")
    return list(values)
