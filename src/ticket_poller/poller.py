"""Poll orchestration. T5 provides the dry run behind ``poll-once``: no writes, no notifications."""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ticket_poller import messages
from ticket_poller.config import Settings
from ticket_poller.fetcher import Fetcher, write_debug_dump
from ticket_poller.models import (
    Baseline,
    Channel,
    FetchResult,
    Inventory,
    InventoryType,
    Listing,
    Match,
    PollOutcome,
    PriceSnapshot,
    ResaleCapture,
)
from ticket_poller.parsing import ParseError, parse_inventory, resale_capture
from ticket_poller.rules import candidates, compute_baseline, evaluate, price_snapshot
from ticket_poller.status import current_baseline
from ticket_poller.store import Store, key_matches

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreviewMatch:
    match: Match
    pending: frozenset[Channel]
    """Channels this match would be sent on now (new, re-armed or upgraded)."""


@dataclass(frozen=True, slots=True)
class Findings:
    """What an OK/PARTIAL poll found, evaluated against the stored state."""

    inventory: Inventory
    capture: ResaleCapture
    snapshot: PriceSnapshot
    baseline: Baseline
    candidates: int
    matches: tuple[PreviewMatch, ...]


@dataclass(frozen=True, slots=True)
class PollPreview:
    fetch: FetchResult
    outcome: PollOutcome
    detail: str
    findings: Findings | None
    has_state: bool


def preview_poll(
    settings: Settings, fetcher: Fetcher, store: Store | None, started: datetime
) -> PollPreview | None:
    """Run fetch → parse → rules against a read-only store (or none yet); None if stopped."""
    result = fetcher.fetch()
    if result is None:
        return None
    has_state = store is not None
    if result.outcome is not PollOutcome.OK:
        return PollPreview(result, result.outcome, result.detail, None, has_state)

    rules = settings.rule_config()
    try:
        inventory = parse_inventory(result.responses, rules.wanted_quantities)
    except ParseError as exc:
        detail = f"parse: {exc}"
        write_debug_dump(
            settings.debug_dir, detail, result.responses, secrets=settings.secret_values()
        )
        return PollPreview(result, PollOutcome.ERROR, detail, None, has_state)

    capture = resale_capture(inventory, result.resale_eligible)
    baseline = (
        current_baseline(store, settings, started)
        if store is not None
        else compute_baseline([], settings.baseline_min_samples)
    )
    current = candidates(inventory, rules)
    matches = evaluate(inventory, baseline, rules)
    findings = Findings(
        inventory=inventory,
        capture=capture,
        snapshot=price_snapshot(inventory, rules),
        baseline=baseline,
        candidates=len(current),
        matches=_with_pending(store, settings, current, matches, started),
    )
    outcome = PollOutcome.PARTIAL if capture is ResaleCapture.MISSING else PollOutcome.OK
    return PollPreview(result, outcome, result.detail, findings, has_state)


def _with_pending(
    store: Store | None,
    settings: Settings,
    current: Sequence[Listing],
    matches: Sequence[Match],
    now: datetime,
) -> tuple[PreviewMatch, ...]:
    if store is None:
        return tuple(PreviewMatch(match, frozenset(Channel)) for match in matches)
    rearm_after = timedelta(hours=settings.realert_after_absent_hours)
    resolved = store.resolve(current, now, rearm_after)
    keyed = key_matches(matches, resolved)
    pending = {
        channel: {key for key, _ in store.pending(keyed, channel, resolved)} for channel in Channel
    }
    return tuple(
        PreviewMatch(match, frozenset(ch for ch in Channel if key in pending[ch]))
        for key, match in keyed
    )


# --- rendering ------------------------------------------------------------------------------


def render_preview(preview: PollPreview, settings: Settings) -> str:
    fetch = preview.fetch
    head = f"poll {preview.outcome.value} in {fetch.elapsed_s:.1f}s"
    if fetch.browser_recycled:
        head += " (browser recycled)"
    lines = [head]
    if preview.detail:
        lines.append(f"detail: {preview.detail}")
    if (hint := messages.failure_hint(preview.outcome, preview.detail)) is not None:
        lines.append(f"hint: {hint}")
    if preview.findings is not None:
        lines.extend(_findings_lines(preview.findings, preview, settings))
    return "\n".join(lines)


def _findings_lines(found: Findings, preview: PollPreview, settings: Settings) -> list[str]:
    ctx = messages.MessageContext.from_settings(settings)
    inventory, eligible = found.inventory, preview.fetch.resale_eligible
    lines = [
        f"listings: primary={len(inventory.primary)} resale={len(inventory.resale)}"
        f" · resale_capture={found.capture.value}"
        f" (page isResaleEligible={'unknown' if eligible is None else str(eligible).lower()})",
        *(f"  {_describe(listing)}" for listing in _by_price(inventory)),
        "quickpicks: " + _quantity_sources(settings.wanted_quantities, inventory),
        "cheapest primary: " + _cheapest(found.snapshot.primary_by_qty),
        "cheapest resale: " + _cheapest(found.snapshot.resale_by_qty),
        f"baseline: {messages.baseline_text(found.baseline, ctx)}",
        *(f"warning: {warning}" for warning in inventory.warnings),
        f"candidates: {found.candidates} · matches: {len(found.matches)}",
    ]
    if not preview.has_state:
        lines.append("(no state yet: every match counts as new)")
    for number, item in enumerate(found.matches, start=1):
        lines.extend(_match_lines(number, item, found.baseline, ctx))
    return lines


def _quantity_sources(wanted: frozenset[int], inventory: Inventory) -> str:
    def source(qty: int) -> str:
        inferred = qty in inventory.inferred_quantities
        return f"qty {qty} {'inferred from maxQuantity' if inferred else 'captured'}"

    return ", ".join(source(qty) for qty in sorted(wanted))


def _cheapest(by_qty: Mapping[int, int | None]) -> str:
    return " · ".join(
        f"q{qty}={'none' if cents is None else messages.money(cents)}"
        for qty, cents in sorted(by_qty.items())
    )


def _by_price(inventory: Inventory) -> list[Listing]:
    return sorted((*inventory.primary, *inventory.resale), key=lambda item: item.price_cents)


def _describe(listing: Listing) -> str:
    """``PRIMARY Sec 123 Row M Seats 5-7 · $383.50 (≈$452.55 all-in) · qty 2,3``."""
    kind = "RESALE" if listing.inventory_type is InventoryType.RESALE else "PRIMARY"
    seats = f" Seats {listing.seats}" if listing.seats else ""
    quantities = ",".join(str(q) for q in sorted(listing.buyable_quantities)) or "none"
    return (
        f"{kind} Sec {listing.section} Row {listing.row}{seats}"
        f" · {messages.money(listing.price_cents)} (≈{messages.money(listing.all_in_cents)} all-in)"
        f" · qty {quantities}"
    )


def _match_lines(
    number: int, item: PreviewMatch, baseline: Baseline, ctx: messages.MessageContext
) -> list[str]:
    alert = (
        "would alert: " + ", ".join(sorted(ch.value for ch in item.pending))
        if item.pending
        else "already sent"
    )
    label = messages.priority_label(item.match, ctx.rules)
    return [
        f"{number}. [{label}] {_describe(item.match.listing)}",
        f"   {messages.why_line(item.match, baseline, ctx)} · {alert}",
    ]
