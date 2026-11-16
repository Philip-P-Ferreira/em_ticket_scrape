"""Text renderings of a poll: the one-line service log and the ``poll-once`` report."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from ticket_poller import messages
from ticket_poller.config import Settings
from ticket_poller.models import Baseline, Channel, Inventory, InventoryType, Listing

if TYPE_CHECKING:
    from ticket_poller.poller import Findings, MatchStatus, PollReport


def poll_summary(report: PollReport, ctx: messages.MessageContext) -> str:
    """The one-line log of a poll (§14), e.g. ``poll ok 3.1s: primary=6 … new=1 top=HIGH``."""
    head = f"poll {report.outcome.value} {report.fetch.elapsed_s:.1f}s"
    findings = report.findings
    if findings is None:
        return f"{head}: {report.detail or 'no detail'}"
    inventory, snapshot = findings.inventory, findings.snapshot
    prices = " ".join(
        f"q{qty}{'~' if qty in inventory.inferred_quantities else ''}="
        f"{'none' if cents is None else messages.money(cents)}"
        for qty, cents in sorted(snapshot.primary_by_qty.items())
    )
    baseline = findings.baseline
    baseline_text = (
        f"{messages.money(baseline.median_cents)}(n={baseline.samples})"
        if baseline.ready and baseline.median_cents is not None
        else f"warming({baseline.samples}/{ctx.rules.baseline_min_samples})"
    )
    new = findings.new_matches
    top = f" top={min(status.match.priority for status in new).name}" if new else ""
    warnings = f" parse_warnings={len(inventory.warnings)}" if inventory.warnings else ""
    return (
        f"{head}: primary={len(inventory.primary)} resale={len(inventory.resale)}"
        f" resale_capture={findings.capture.value} {prices} baseline={baseline_text}"
        f" matches={len(findings.matches)} new={len(new)}{top}{warnings}"
    )


# --- rendering (poll-once) --------------------------------------------------------------------


def render_report(report: PollReport, settings: Settings) -> str:
    fetch = report.fetch
    head = f"poll {report.outcome.value} in {fetch.elapsed_s:.1f}s"
    if fetch.browser_recycled:
        head += " (browser recycled)"
    lines = [head]
    if report.detail:
        lines.append(f"detail: {report.detail}")
    if (hint := messages.failure_hint(report.outcome, report.detail)) is not None:
        lines.append(f"hint: {hint}")
    if report.profile_rotated_to is not None:
        lines.append(f"browser profile rotated: the blocked one is {report.profile_rotated_to}")
    if report.findings is not None:
        lines.extend(_findings_lines(report.findings, report, settings))
    return "\n".join(lines)


def _findings_lines(found: Findings, report: PollReport, settings: Settings) -> list[str]:
    ctx = messages.MessageContext.from_settings(settings)
    inventory, eligible = found.inventory, report.fetch.resale_eligible
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
        f"candidates: {len(found.candidates)} · matches: {len(found.matches)}",
    ]
    if not report.has_state:
        lines.append("(no state yet: every match counts as new)")
    for number, item in enumerate(found.matches, start=1):
        lines.extend(_match_lines(number, item, found.baseline, ctx, dry_run=report.dry_run))
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


def _channels(channels: Iterable[Channel]) -> str:
    return ", ".join(sorted(channel.value for channel in channels))


def _delivery_text(item: MatchStatus, *, dry_run: bool) -> str:
    if not item.pending:
        return "already sent"
    if dry_run:
        return f"would alert: {_channels(item.pending)}"
    parts = []
    if item.sent:
        parts.append(f"sent: {_channels(item.sent)}")
    if unsent := item.pending - item.sent:
        parts.append(f"not sent (retried next poll): {_channels(unsent)}")
    return " · ".join(parts)


def _match_lines(
    number: int,
    item: MatchStatus,
    baseline: Baseline,
    ctx: messages.MessageContext,
    *,
    dry_run: bool,
) -> list[str]:
    label = messages.priority_label(item.match, ctx.rules)
    return [
        f"{number}. [{label}] {_describe(item.match.listing)}",
        f"   {messages.why_line(item.match, baseline, ctx)}"
        f" · {_delivery_text(item, dry_run=dry_run)}",
    ]
