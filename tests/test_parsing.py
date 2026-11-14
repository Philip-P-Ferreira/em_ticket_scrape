from collections import Counter
from dataclasses import replace

import pytest

from tests.builders import (
    facets,
    load_captured,
    make_group,
    make_offer,
    make_pick,
    make_resale_offer,
    quickpicks,
)
from ticket_poller.models import (
    CapturedResponse,
    Inventory,
    InventoryType,
    Listing,
    ResaleCapture,
)
from ticket_poller.parsing import (
    ParseError,
    SeatGroup,
    merge_seat_groups,
    parse_inventory,
    resale_capture,
    resale_requested,
    seat_label,
)

BOTH = frozenset({2, 3})
TIER_343 = "GJ6DEMJRIYYDAMBQGAYDAN34GMZTSLRVGB6DMMJOHA2XYNBOGAYHYMZTFY4TK7D4PR6A"
TIER_383 = "GJ6DEMJRIYYDAMBQGAYDAN34GM3TSLRVGB6DMOJOGA2XYNBOGAYHYMZXFY4TK7D4PR6A"
TIER_403 = "GJ6DEMJRIYYDAMBQGAYDAN34GM4TSLRVGB6DOMROGY2XYNBOGAYHYMZZFY4TK7D4PR6A"

type Row = tuple[str, str, str, int, int, frozenset[int], int | None, str]


def rows(listings: tuple[Listing, ...]) -> list[Row]:
    return [
        (
            listing.section,
            listing.row,
            listing.seats,
            listing.price_cents,
            listing.fees_cents,
            listing.buyable_quantities,
            listing.max_together,
            listing.offer_id,
        )
        for listing in listings
    ]


def nk(*names: str, wanted: frozenset[int] = BOTH) -> Inventory:
    return parse_inventory([load_captured(f"nk_{name}") for name in names], wanted)


def group(offer: str, places: set[str], qty: int, max_quantity: int | None = None) -> SeatGroup:
    return SeatGroup(
        offer_id=offer,
        section="101",
        row="N",
        places=frozenset(places),
        seats=frozenset(places),
        quantities=frozenset({qty}),
        max_quantity=max_quantity,
    )


# --- NK fixtures (design §4) -------------------------------------------------------------


def test_nk_qty2_parses_six_listings_with_exact_fields() -> None:
    inventory = nk("quickpicks_qty2", wanted=frozenset({2}))

    assert rows(inventory.primary) == [
        ("104", "R", "9\u201310", 34350, 6185, frozenset({2}), 2, TIER_343),
        ("123", "M", "5\u20136", 38350, 6905, frozenset({2}), 4, TIER_383),
        ("101", "N", "12\u201313", 38350, 6905, frozenset({2}), 3, TIER_383),
        ("104", "Q", "13\u201314", 38350, 6905, frozenset({2}), 2, TIER_383),
        ("103", "M", "17\u201318", 38350, 6905, frozenset({2}), 2, TIER_383),
        ("126", "J", "5\u20136", 40350, 7265, frozenset({2}), 2, TIER_403),
    ]
    assert inventory.primary[0].places == {"GEYDIOSSHI4Q", "GEYDIOSSHIYTA"}
    assert inventory.primary[0].all_in_cents == 40535
    assert all(listing.inventory_type is InventoryType.PRIMARY for listing in inventory.primary)
    assert inventory.resale == ()
    assert inventory.inferred_quantities == frozenset()
    assert inventory.warnings == ()


def test_nk_qty3_parses_two_listings() -> None:
    inventory = nk("quickpicks_qty3", wanted=frozenset({3}))

    assert rows(inventory.primary) == [
        ("123", "M", "5\u20137", 38350, 6905, frozenset({3}), 4, TIER_383),
        ("101", "N", "12\u201314", 38350, 6905, frozenset({3}), 3, TIER_383),
    ]


def test_nk_qty2_and_qty3_merge_into_the_design_table() -> None:
    inventory = nk("quickpicks_qty2", "quickpicks_qty3", "facets_inventorytypes_offer")

    assert rows(inventory.primary) == [
        ("104", "R", "9\u201310", 34350, 6185, frozenset({2}), 2, TIER_343),
        ("123", "M", "5\u20137", 38350, 6905, frozenset({2, 3}), 4, TIER_383),
        ("101", "N", "12\u201314", 38350, 6905, frozenset({2, 3}), 3, TIER_383),
        ("104", "Q", "13\u201314", 38350, 6905, frozenset({2}), 2, TIER_383),
        ("103", "M", "17\u201318", 38350, 6905, frozenset({2}), 2, TIER_383),
        ("126", "J", "5\u20136", 40350, 7265, frozenset({2}), 2, TIER_403),
    ]
    merged = inventory.primary[1]
    assert merged.places == {"GEZDGOSNHI2Q", "GEZDGOSNHI3A", "GEZDGOSNHI3Q"}
    assert merged.identity == f"primary:{TIER_383}:GEZDGOSNHI2Q,GEZDGOSNHI3A,GEZDGOSNHI3Q"
    assert inventory.resale == ()
    assert inventory.inferred_quantities == frozenset()
    assert inventory.resale_requested is False
    assert inventory.warnings == ()


def test_missing_qty3_payload_is_inferred_from_max_quantity_with_a_warning() -> None:
    inventory = nk("quickpicks_qty2")

    assert inventory.inferred_quantities == {3}
    assert inventory.warnings == ("no quickpicks for qty=3; inferred from maxQuantity",)
    buyable = {(lst.section, lst.row): lst.buyable_quantities for lst in inventory.primary}
    assert buyable == {
        ("104", "R"): {2},
        ("123", "M"): {2, 3},
        ("101", "N"): {2, 3},
        ("104", "Q"): {2},
        ("103", "M"): {2},
        ("126", "J"): {2},
    }


def test_inference_requires_the_offer_to_sell_that_quantity() -> None:
    offer = make_offer(sellable=(2, 4))
    pick = make_pick(make_group([1, 2]), max_quantity=4)

    inventory = parse_inventory([quickpicks(2, [pick], [offer])], BOTH)

    assert inventory.primary[0].buyable_quantities == {2}


# --- Bulls resale fixture ------------------------------------------------------------------


def test_bulls_parses_25_resale_listings_with_the_design_distribution() -> None:
    inventory = parse_inventory([load_captured("bulls_resale_facets_trimmed")], BOTH)

    assert inventory.primary == ()
    assert len(inventory.resale) == 25
    distribution = Counter(
        tuple(sorted(listing.buyable_quantities)) for listing in inventory.resale
    )
    assert distribution == {
        (2, 4): 7,
        (2,): 5,
        (2, 4, 6, 8): 5,
        (1, 3): 3,
        (2, 4, 6): 2,
        (1, 2, 3, 5): 1,
        (1,): 1,
        (4,): 1,
    }
    assert inventory.resale[0] == Listing(
        inventory_type=InventoryType.RESALE,
        offer_id="GN6DCMRVGI3DSMJQHE4HYMLBGY3TEMLFGRTDK",
        listing_id="1252691098",
        section="101",
        row="7",
        places=frozenset(),
        seats="11\u201312",
        price_cents=45200,
        fees_cents=12204,
        buyable_quantities=frozenset({2}),
        max_together=None,
    )
    assert inventory.resale[0].identity == "resale:1252691098"
    assert inventory.resale_requested is True
    assert resale_capture(inventory, eligible=True) is ResaleCapture.CAPTURED


# --- merging -------------------------------------------------------------------------------


def test_disjoint_groups_in_the_same_row_at_the_same_price_stay_separate() -> None:
    offer = make_offer()
    pair = quickpicks(2, [make_pick(make_group([9, 10]), max_quantity=3)], [offer])
    triple = quickpicks(3, [make_pick(make_group([20, 21, 22]), max_quantity=3)], [offer])

    inventory = parse_inventory([pair, triple], BOTH)

    assert [(lst.seats, lst.buyable_quantities) for lst in inventory.primary] == [
        ("9\u201310", frozenset({2})),
        ("20\u201322", frozenset({3})),
    ]
    assert {lst.price_cents for lst in inventory.primary} == {38350}
    assert len({lst.identity for lst in inventory.primary}) == 2


def test_same_seats_under_different_offers_stay_separate() -> None:
    offers = [make_offer("A"), make_offer("B", 403.5)]
    pair = quickpicks(2, [make_pick(make_group([5, 6], offers=["A"]))], offers)
    triple = quickpicks(3, [make_pick(make_group([5, 6, 7], offers=["B"]), max_quantity=3)], offers)

    inventory = parse_inventory([pair, triple], BOTH)

    assert [(lst.offer_id, lst.buyable_quantities) for lst in inventory.primary] == [
        ("A", frozenset({2})),
        ("B", frozenset({3})),
    ]


def test_merge_is_transitive_and_keeps_first_appearance_order() -> None:
    other = group("X", {"x1", "x2"}, 2)
    left, right = group("O", {"1", "2"}, 2, 2), group("O", {"3", "4"}, 2, 4)
    bridge = group("O", {"2", "3"}, 3)

    merged = merge_seat_groups([other, left, right, bridge])

    assert merged == [
        other,
        SeatGroup("O", "101", "N", frozenset("1234"), frozenset("1234"), frozenset({2, 3}), 4),
    ]


def test_merge_of_nested_groups_unions_quantities_and_places() -> None:
    pair, triple = group("O", {"1", "2"}, 2, 3), group("O", {"1", "2", "3"}, 3, 3)

    assert merge_seat_groups([pair, triple]) == [replace(triple, quantities=frozenset({2, 3}))]


# --- offers, warnings, errors -----------------------------------------------------------


def test_protected_and_offline_offers_are_skipped_silently() -> None:
    offers = [
        make_offer("P", protected=True),
        make_offer("OFF", online=False),
        make_offer("OK", 250.0),
    ]
    picks = [
        make_pick(make_group([1, 2], offers=["P"])),
        make_pick(make_group([3, 4], offers=["OFF"])),
        make_pick(make_group([5, 6], offers=["P", "OK"])),
    ]

    inventory = parse_inventory([quickpicks(2, picks, offers)], frozenset({2}))

    assert [(lst.offer_id, lst.seats) for lst in inventory.primary] == [("OK", "5\u20136")]
    assert inventory.warnings == ()


def test_missing_offer_skips_the_group_with_a_warning() -> None:
    inventory = parse_inventory(
        [quickpicks(2, [make_pick(make_group([1, 2], offers=["GONE"]))])], frozenset({2})
    )

    no_groups = "quickpicks qty=2: no seat group parsed from 1 pick(s)"
    assert inventory.primary == ()
    assert inventory.warnings == ("quickpicks offer missing for 101 N; group skipped", no_groups)
    assert inventory.degraded == (no_groups,)


def test_a_group_with_an_unknown_offer_type_is_skipped_with_a_warning() -> None:
    offers = [make_offer("X", inventory_type="mystery"), make_offer()]
    picks = [make_pick(make_group([1, 2], offers=["X"])), make_pick(make_group([3, 4]))]

    inventory = parse_inventory([quickpicks(2, picks, offers)], frozenset({2}))

    assert [lst.seats for lst in inventory.primary] == ["3\u20134"]
    assert inventory.warnings == (
        "quickpicks group for 101 N skipped: no primary offer (types ['mystery'])",
        "ignored offer with inventoryType 'mystery'",
    )
    assert inventory.degraded == ()


def test_picks_with_only_expected_skips_are_not_degraded() -> None:
    offers = [make_offer("P", protected=True), make_resale_offer("L9")]
    picks = [
        make_pick(make_group([1, 2], offers=["P"])),
        make_pick(make_group([3], offers=["RES-L9"])),
    ]

    inventory = parse_inventory([quickpicks(2, picks, offers)], frozenset({2}))

    assert inventory.primary == ()
    assert (inventory.warnings, inventory.degraded) == ((), ())


def test_picks_with_no_readable_group_are_degraded() -> None:
    picks = [{"row": "N", "offerGroups": [make_group([3, 4])]}]

    inventory = parse_inventory([quickpicks(2, picks, [make_offer()])], frozenset({2}))

    assert inventory.degraded == (
        "malformed quickpicks pick skipped: 'section': expected a non-empty string, got None",
        "quickpicks qty=2: no seat group parsed from 1 pick(s)",
    )
    assert inventory.warnings == inventory.degraded


def test_empty_quickpicks_are_not_degraded() -> None:
    inventory = parse_inventory([quickpicks(2, [], [make_offer()])], frozenset({2}))

    assert (inventory.warnings, inventory.degraded) == ((), ())


def test_offers_embedded_in_another_payload_are_joined() -> None:
    pick = make_pick(make_group([1, 2]))

    inventory = parse_inventory([quickpicks(2, [pick]), facets([make_offer()])], frozenset({2}))

    assert len(inventory.primary) == 1


def test_embedded_offers_keyed_by_id_are_accepted() -> None:
    response = quickpicks(2, [make_pick(make_group([1, 2]))])
    assert response.body is not None
    body = {**response.body, "_embedded": {"offer": {"OFFER1": make_offer()}}}

    inventory = parse_inventory([replace(response, body=body)], frozenset({2}))

    assert len(inventory.primary) == 1


def test_truncated_quickpicks_add_a_warning() -> None:
    response = quickpicks(2, [make_pick(make_group([1, 2]))], [make_offer()], total=41)

    inventory = parse_inventory([response], frozenset({2}))

    assert inventory.warnings == ("quickpicks truncated (qty=2: 1 of 41)",)


def test_non_200_and_empty_bodies_are_ignored() -> None:
    blocked = CapturedResponse(
        url=quickpicks(2, []).url, status=403, body={"response": "dynamic_block"}
    )
    empty = CapturedResponse(url=quickpicks(3, []).url, status=200, body=None)

    inventory = parse_inventory([blocked, empty], frozenset({2}))

    assert inventory.primary == ()
    assert inventory.inferred_quantities == {2}


def test_unknown_inventory_type_is_ignored_with_a_warning() -> None:
    inventory = parse_inventory([facets([make_offer(inventory_type="mystery")])], frozenset())

    assert inventory.warnings == ("ignored offer with inventoryType 'mystery'",)


def test_resale_is_deduplicated_by_listing_id_across_bodies() -> None:
    first = make_resale_offer("L1")
    edited = make_resale_offer("L1", list_price=400.0, offerId="RES-L1-v2")

    inventory = parse_inventory([facets([first]), facets([edited])], BOTH)

    assert [(lst.listing_id, lst.price_cents) for lst in inventory.resale] == [("L1", 45200)]


def test_resale_offers_in_quickpicks_are_resale_not_primary() -> None:
    resale = make_resale_offer("L9", sellable=(3,))
    response = quickpicks(3, [make_pick(make_group([1, 2, 3], offers=["RES-L9"]))], [resale])

    inventory = parse_inventory([response], frozenset({3}))

    assert inventory.primary == ()
    assert [lst.identity for lst in inventory.resale] == ["resale:L9"]


def test_resale_facets_without_offers_add_a_warning() -> None:
    response = facets(facet_list=[{"inventoryTypes": ["resale"], "count": 2}])

    inventory = parse_inventory([response], frozenset())

    assert inventory.warnings == ("resale facets seen without resale offers",)


@pytest.mark.parametrize(
    ("bad_pick", "warning"),
    [
        pytest.param(
            {"row": "N", "offerGroups": [make_group([3, 4])]},
            "malformed quickpicks pick skipped: 'section': expected a non-empty string, got None",
            id="pick-without-section",
        ),
        pytest.param(
            make_pick({"offers": ["OFFER1"], "seats": ["3", "4"]}),
            "malformed quickpicks group for 101 N skipped: 'places': expected a list, got NoneType",
            id="group-without-places",
        ),
        pytest.param(
            make_pick({"offers": ["OFFER1"], "places": [1], "seats": []}),
            "malformed quickpicks group for 101 N skipped: 'places': expected a list of strings",
            id="places-not-strings",
        ),
    ],
)
def test_malformed_pick_or_group_is_skipped_with_a_warning(
    bad_pick: dict[str, object], warning: str
) -> None:
    picks = [bad_pick, make_pick(make_group([1, 2]))]

    inventory = parse_inventory([quickpicks(2, picks, [make_offer()])], frozenset({2}))

    assert [lst.seats for lst in inventory.primary] == ["1\u20132"]
    assert inventory.warnings == inventory.degraded == (warning,)


def test_protected_and_offline_resale_offers_are_skipped() -> None:
    offers = [
        make_resale_offer("P", protected=True),
        make_resale_offer("OFF", online=False),
        make_resale_offer("OK"),
    ]

    inventory = parse_inventory([facets(offers)], frozenset())

    assert [lst.identity for lst in inventory.resale] == ["resale:OK"]
    assert inventory.warnings == ()


def test_offer_without_inventory_type_is_primary_without_a_warning() -> None:
    offer = {k: v for k, v in make_offer().items() if k != "inventoryType"}
    pick = make_pick(make_group([1, 2]))

    inventory = parse_inventory([quickpicks(2, [pick], [offer])], frozenset({2}))

    assert len(inventory.primary) == 1
    assert inventory.warnings == ()


def test_resale_facets_without_parsed_offers_leave_capture_missing() -> None:
    response = facets(facet_list=[{"inventoryTypes": ["resale"], "count": 2}])

    inventory = parse_inventory([response], BOTH)

    assert resale_capture(inventory, eligible=True) is ResaleCapture.MISSING


PAIR = make_pick(make_group([1, 2]))


@pytest.mark.parametrize(
    ("picks", "offers"),
    [
        pytest.param("nope", [make_offer()], id="picks-not-a-list"),
        pytest.param([], "nope", id="offers-not-a-list"),
        pytest.param([PAIR], [make_offer(listPrice=None)], id="offer-without-price"),
        pytest.param([PAIR], [make_offer(listPrice=True)], id="boolean-price"),
        pytest.param([PAIR], [make_offer(charges="nope")], id="charges-not-a-list"),
        pytest.param([], [make_offer(offerId=7)], id="offer-id-not-a-string"),
        pytest.param(
            [], [{**make_resale_offer(), "listingId": None}], id="resale-without-listing-id"
        ),
    ],
)
def test_structurally_invalid_payloads_raise_parse_error(picks: object, offers: object) -> None:
    body = {"picks": picks, "_embedded": {"offer": offers}}
    response = CapturedResponse(url=quickpicks(2, []).url, status=200, body=body)

    with pytest.raises(ParseError):
        parse_inventory([response], frozenset({2}))


# --- labels ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seats", "label"),
    [
        (["7", "5", "6"], "5\u20137"),
        (["9", "11"], "9, 11"),
        (["10", "9", "12"], "9, 10, 12"),
        (["4"], "4"),
        (["B", "A"], "A, B"),
        ([], ""),
    ],
)
def test_seat_label(seats: list[str], label: str) -> None:
    assert seat_label(seats) == label


# --- resale requested / capture (design §7) -------------------------------------------


def test_nk_urls_do_not_request_resale() -> None:
    names = ["quickpicks_qty2", "quickpicks_qty3", "facets_inventorytypes_offer"]
    assert resale_requested(load_captured(f"nk_{name}").url for name in names) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://offeradapter.ticketmaster.com/api/ismds/event/E/facets?q=available",
        "https://services.ticketmaster.com/api/ismds/event/E/quickpicks?qty=2&includeResale=true",
        "https://services.ticketmaster.com/api/ismds/event/E/facets?q=and%28available%2C+inventorytypes%3A%22resale%22%29",
        "https://services.ticketmaster.com/api/ismds/event/E/facets?q=inventorytypes%3A%27resale%27",
        "https://services.ticketmaster.com/api/ismds/event/E/facets?q=inventorytypes%3A%28%27resale%27%29",
        "https://services.ticketmaster.com/api/ismds/event/E/facets?q=inventorytypes%3A+%28+%22resale%22%29",
    ],
)
def test_resale_requested(url: str) -> None:
    assert resale_requested([url]) is True


def test_bulls_url_requests_resale() -> None:
    assert resale_requested([load_captured("bulls_resale_facets_trimmed").url]) is True


def inventory_with(*, requested: bool = False, seen: bool = False) -> Inventory:
    resale = parse_inventory([facets([make_resale_offer()])], BOTH).resale if seen else ()
    return Inventory(
        primary=(),
        resale=resale,
        inferred_quantities=frozenset(),
        resale_requested=requested,
        warnings=(),
    )


@pytest.mark.parametrize(
    ("requested", "seen", "eligible", "status"),
    [
        (True, False, True, ResaleCapture.CAPTURED),
        (False, True, True, ResaleCapture.CAPTURED),
        (False, True, False, ResaleCapture.CAPTURED),
        (True, False, None, ResaleCapture.CAPTURED),
        (False, False, False, ResaleCapture.NOT_ELIGIBLE),
        (False, False, None, ResaleCapture.UNKNOWN),
        (False, False, True, ResaleCapture.MISSING),
    ],
)
def test_resale_capture_truth_table(
    *, requested: bool, seen: bool, eligible: bool | None, status: ResaleCapture
) -> None:
    assert resale_capture(inventory_with(requested=requested, seen=seen), eligible) is status
