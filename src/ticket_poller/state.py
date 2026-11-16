"""Poll records, poll aggregates and the service loop's persisted runtime state.

Plain value types shared by ``store`` (which persists them), ``schedule``, ``status`` and
``poller``; none of them touch the database.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Self

from ticket_poller.models import Channel, PollOutcome, PriceSnapshot, ResaleCapture


@dataclass(frozen=True, slots=True)
class PollRecord:
    """One row of ``polls``. Inventory fields are None unless the outcome is OK/PARTIAL."""

    started_at: datetime
    outcome: PollOutcome
    detail: str = ""
    elapsed_s: float | None = None
    resale_capture: ResaleCapture | None = None
    primary_listings: int | None = None
    resale_listings: int | None = None
    snapshot: PriceSnapshot | None = None
    matches: int | None = None
    new_matches: int | None = None
    primary_labels: Mapping[int, str] = field(default_factory=dict)
    """Per quantity, where the cheapest primary listing in ``snapshot`` is (e.g. ``123 M``)."""
    resale_labels: Mapping[int, str] = field(default_factory=dict)
    browser_recycled: bool = False
    """The browser was restarted during this poll."""
    parse_warnings: int | None = None
    """How many parser warnings the poll's inventory had (None if nothing was parsed)."""
    parse_degraded: bool = False
    """The parser dropped data it should have read (malformed or unparsable quickpicks)."""
    profile_rotated: bool = False
    """The browser profile was set aside after this poll saw the block page."""


@dataclass(frozen=True, slots=True)
class PollStats:
    """Aggregates over polls since ``since`` (canary and status)."""

    since: datetime
    outcomes: Mapping[PollOutcome, int]
    last_success_at: datetime | None
    """The latest OK/PARTIAL poll at any time, not only in the window."""
    resale_captured: int
    """Successful polls in the window whose resale capture was CAPTURED."""
    cheapest_primary: Mapping[int, int | None]
    """Per quantity, from the latest successful poll in the window."""
    cheapest_resale: Mapping[int, int | None]
    cheapest_primary_label: Mapping[int, str | None]
    """Per quantity, the label of the listing behind ``cheapest_primary``."""
    cheapest_resale_label: Mapping[int, str | None]
    resale_listings_now: int
    """Resale listings in the latest successful poll in the window (0 if none)."""
    low_primary: Mapping[int, int | None]
    """Per quantity, the lowest cheapest price over the window."""
    low_resale: Mapping[int, int | None]
    alerts: int
    """Listings with an alert delivered (any channel) in the window."""
    top_alerts: int
    channel_failures: Mapping[Channel, int]
    """Failed alert sends per channel in the window."""
    browser_recycles: int
    """Polls in the window during which the browser was restarted."""
    resale_missing: int = 0
    """Successful polls in the window whose resale capture was MISSING."""
    parse_warning_polls: int = 0
    """Polls in the window with at least one parser warning."""
    parse_degraded_polls: int = 0
    """Polls in the window whose parse was degraded."""

    @property
    def total(self) -> int:
        return sum(self.outcomes.values())


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """Loop state persisted in ``kv`` so restarts keep backoff, outage and notice flags.

    The ``record_*`` methods are the allowed transitions; each returns a new state.
    """

    failure_started_at: datetime | None = None
    consecutive_failures: int = 0
    consecutive_blocks: int = 0
    profile_rotated_this_streak: bool = False
    outage_announced: bool = False
    recovery_pending: bool = False
    last_outage_duration_s: float | None = None
    last_canary_date: date | None = None
    finished_notice_sent: bool = False
    resale_eligible: bool | None = None
    resale_eligible_notice_sent: bool = False
    resale_missing_streak: int = 0
    resale_missing_notice_sent: bool = False
    consecutive_email_failures: int = 0
    email_failing_notice_sent: bool = False

    def record_failure(self, outcome: PollOutcome, now: datetime) -> Self:
        if outcome not in (PollOutcome.ERROR, PollOutcome.BLOCKED):
            raise ValueError(f"{outcome} is not a failure")
        blocked = outcome is PollOutcome.BLOCKED
        return replace(
            self,
            failure_started_at=self.failure_started_at or now,
            consecutive_failures=self.consecutive_failures + 1,
            consecutive_blocks=self.consecutive_blocks + 1 if blocked else 0,
        )

    def record_success(self, now: datetime) -> Self:
        """End any failure streak; a recovery notice is owed only if the outage was announced."""
        if self.failure_started_at is None:
            return self
        announced = self.outage_announced
        duration = (now - self.failure_started_at).total_seconds()
        return replace(
            self,
            failure_started_at=None,
            consecutive_failures=0,
            consecutive_blocks=0,
            profile_rotated_this_streak=False,
            outage_announced=False,
            recovery_pending=self.recovery_pending or announced,
            last_outage_duration_s=duration if announced else self.last_outage_duration_s,
        )

    def record_profile_rotation(self) -> Self:
        """At most one rotation per failure streak; ``record_success`` allows the next."""
        if self.failure_started_at is None:
            raise ValueError("a profile rotation needs a failure streak")
        return replace(self, profile_rotated_this_streak=True)

    def record_outage_notice(self, delivered: bool) -> Self:
        """The outage counts as announced only once at least one channel delivered."""
        return replace(self, outage_announced=True) if delivered else self

    def record_recovery_notice(self, delivered: bool) -> Self:
        return replace(self, recovery_pending=False) if delivered else self

    def record_email(self, ok: bool) -> Self:
        """A delivered email ends the "email failing" episode; a failed one extends it."""
        if ok:
            return replace(self, consecutive_email_failures=0, email_failing_notice_sent=False)
        return replace(self, consecutive_email_failures=self.consecutive_email_failures + 1)

    def record_canary(self, local_date: date, email_ok: bool) -> Self:
        """``last_canary_date`` moves only when the canary email was accepted."""
        return replace(self, last_canary_date=local_date) if email_ok else self
