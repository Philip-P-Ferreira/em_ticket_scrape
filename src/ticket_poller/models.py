"""Domain types shared by parsing, rules, store, messages and the poller.

All types are immutable value objects with no I/O. Money is integer cents per ticket,
except the baseline median, which is an exact ``Decimal`` (it may be a half cent).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any, Final

SUPPORTED_QUANTITIES: Final = frozenset({2, 3})
"""Purchase sizes the poller can watch: the price history has columns for these only."""


class InventoryType(StrEnum):
    PRIMARY = "primary"
    RESALE = "resale"


class PollOutcome(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    ERROR = "error"


class ResaleCapture(StrEnum):
    NOT_ELIGIBLE = "not_eligible"
    CAPTURED = "captured"
    MISSING = "missing"
    UNKNOWN = "unknown"


class Reason(StrEnum):
    RESALE_LISTED = "resale_listed"
    UNDER_PRICE_CAP = "under_price_cap"
    BELOW_BASELINE = "below_baseline"
    PREFERRED_DEAL = "preferred_deal"


class Priority(IntEnum):
    """Alert tier; a lower value is more important, so ``sorted()`` puts TOP first."""

    TOP = 1
    HIGH = 2
    NORMAL = 3


class Channel(StrEnum):
    EMAIL = "email"
    PUSH = "push"


class NoticeKind(StrEnum):
    """What a notification is about; used for routing (alert vs ops topic) and auditing."""

    ALERT = "alert"
    CANARY = "canary"
    OUTAGE = "outage"
    RECOVERED = "recovered"
    RESALE_ELIGIBLE = "resale_eligible"
    RESALE_MISSING = "resale_missing"
    FINISHED = "finished"
    EMAIL_FAILING = "email_failing"
    SERVICE_FAILED = "service_failed"
    TEST = "test"


@dataclass(frozen=True, slots=True)
class CapturedResponse:
    url: str
    status: int
    body: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class Listing:
    """One alertable listing: a canonical primary seat group, or one resale listing.

    ``places`` holds base32 place IDs and is empty for resale. ``seats`` is a display
    label for the seat range (e.g. 5 to 7, joined by an en dash). Prices are per ticket:
    ``price_cents`` is TM's listPrice and ``fees_cents`` the sum of its fee charges.
    """

    inventory_type: InventoryType
    offer_id: str
    listing_id: str | None
    section: str
    row: str
    places: frozenset[str]
    seats: str
    price_cents: int
    fees_cents: int
    buyable_quantities: frozenset[int]
    max_together: int | None

    def __post_init__(self) -> None:
        if self.inventory_type is InventoryType.RESALE and not self.listing_id:
            raise ValueError("a resale listing needs a listing_id")
        if self.inventory_type is InventoryType.PRIMARY and self.listing_id is not None:
            raise ValueError("a primary listing has no listing_id")

    @property
    def identity(self) -> str:
        """Canonical dedup key: ``primary:{offer}:{sorted places}`` or ``resale:{listing}``."""
        if self.inventory_type is InventoryType.RESALE:
            return f"resale:{self.listing_id}"
        return f"primary:{self.offer_id}:{','.join(sorted(self.places))}"

    @property
    def all_in_cents(self) -> int:
        """The UI's "all-in before tax" per ticket; for display only."""
        return self.price_cents + self.fees_cents

    def buyable_as(self, quantity: int) -> bool:
        """Whether the listing can be bought as exactly ``quantity`` tickets."""
        return quantity in self.buyable_quantities


@dataclass(frozen=True, slots=True)
class Inventory:
    primary: tuple[Listing, ...]
    resale: tuple[Listing, ...]
    inferred_quantities: frozenset[int]
    """Wanted quantities with no captured quickpicks this poll (the fallback was used)."""
    resale_requested: bool
    """A captured request asked for resale (§7)."""
    warnings: tuple[str, ...]
    degraded: tuple[str, ...] = ()
    """The warnings meaning the parser dropped data it should have read (a format change)."""


@dataclass(frozen=True, slots=True)
class Baseline:
    median_cents: Decimal | None
    samples: int
    ready: bool


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    baseline_sample_cents: int | None
    """Cheapest primary listing buyable in any wanted quantity."""
    primary_by_qty: Mapping[int, int | None]
    resale_by_qty: Mapping[int, int | None]


@dataclass(frozen=True, slots=True)
class Match:
    listing: Listing
    reasons: frozenset[Reason]
    priority: Priority
    within_preferred_price: bool
    pct_below_baseline: Decimal | None


@dataclass(frozen=True, slots=True)
class FetchResult:
    outcome: PollOutcome
    responses: tuple[CapturedResponse, ...]
    detail: str
    resale_eligible: bool | None
    elapsed_s: float
    browser_recycled: bool = False
