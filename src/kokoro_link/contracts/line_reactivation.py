"""Ports and value objects for the LINE dormant-reactivation campaign (LR).

An operator picks dormant LINE-bound characters in the admin console and
fires one proactive message at each of them. This module owns the
*ledger* half of that: the record of which run selected which characters
and what happened to each one.

Two shapes, deliberately separate:

* :class:`LineReactivationCampaign` — the operator's action (actor, when,
  how many). It carries no character reference at all.
* :class:`LineReactivationCampaignItem` — one character's slot in that
  run. ``outcome is None`` **is** the pending marker; the repository's
  resume query selects exactly on it, so a Core restart mid-run needs
  nothing reconstructed.

The idempotency semantics D5 asks for live in the key shape rather than
in the runner: ``(campaign_id, character_id)`` is the item primary key,
so a resumed POST of the same ``campaign_id`` cannot add a second attempt
row for a character that already has one, regardless of how many runners
race. The console mints ``campaign_id``; Core never generates one.

That key alone fences the *bookkeeping*, not the *send*: two runners in
two API replicas would both evaluate a pending item and only lose the
race afterwards, at ``record_outcome`` — one player, two recall messages.
:meth:`LineReactivationCampaignRepositoryPort.claim_item` is the fence
that sits *before* the send: a runner must win a leased claim on the row
before it may call the dispatcher at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


CAMPAIGN_STATUS_RUNNING = "running"
"""Selected, and at least one item still has no outcome."""

CAMPAIGN_STATUS_COMPLETED = "completed"
"""Every item reached an outcome. Terminal — a completed campaign is
never resumed; a follow-up send is a new campaign (D6)."""

VALID_CAMPAIGN_STATUSES = frozenset(
    {CAMPAIGN_STATUS_RUNNING, CAMPAIGN_STATUS_COMPLETED},
)


class LineReactivationCampaignConflictError(Exception):
    """A ``campaign_id`` was re-used for a different selection (D5).

    Re-POSTing the same id with the same character set is a *resume* and
    must not raise; re-POSTing it with a different set is the one case
    the ledger cannot honestly represent — the stored ``total`` and item
    rows already describe another selection — so it is refused rather
    than merged. The API layer maps this to 409 ``campaign_conflict``.
    """


@dataclass(frozen=True, slots=True)
class LineReactivationCampaign:
    """One admin-triggered reactivation run."""

    campaign_id: str
    actor: str
    status: str
    created_at: datetime
    total: int
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.campaign_id.strip():
            raise ValueError("campaign_id must be non-empty")
        if self.status not in VALID_CAMPAIGN_STATUSES:
            raise ValueError(
                "status must be one of "
                f"{sorted(VALID_CAMPAIGN_STATUSES)}, got {self.status!r}",
            )

    @property
    def running(self) -> bool:
        return self.status == CAMPAIGN_STATUS_RUNNING


@dataclass(frozen=True, slots=True)
class LineReactivationCampaignItem:
    """One character's slot in a campaign.

    ``outcome`` is a ``ProactiveOutcome`` string once attempted (``sent``
    / ``gate_blocked`` / ``quality_withheld`` / …); ``detail`` is
    human-readable amplification for the report and is never parsed.

    ``message_text`` is the body the character actually sent — verbatim,
    never clipped. It is the report's reason for existing: the operator
    fires a small batch, reads what was said, and only then releases the
    rest. Set only when ``outcome == "sent"``; every other row's message
    either does not exist or never reached a player, and showing one
    would misrepresent what is being judged.

    ``claimed_at`` is the send-side lease, not a report field: the instant
    some runner took ownership of this row. It is never surfaced to the
    console — a claimed-but-unstamped row is still "pending" to a reader.
    """

    campaign_id: str
    character_id: str
    outcome: str | None = None
    detail: str | None = None
    message_text: str | None = None
    attempted_at: datetime | None = None
    claimed_at: datetime | None = None

    @property
    def pending(self) -> bool:
        """No outcome yet — the exact set a resume re-runs.

        Deliberately blind to ``claimed_at``: "has this character been
        dealt with" and "is someone dealing with it right now" are
        different questions, and the campaign is only complete when the
        first one is true for every row.
        """
        return self.outcome is None


class LineReactivationCampaignRepositoryPort(Protocol):
    """Durable storage for campaigns and their per-character outcomes."""

    async def get(
        self, campaign_id: str,
    ) -> LineReactivationCampaign | None:
        """Fetch one campaign, or ``None`` when the id is unknown."""

    async def create(
        self,
        campaign: LineReactivationCampaign,
        character_ids: Sequence[str],
    ) -> None:
        """Create the campaign row and one pending item per character.

        One transaction: a campaign whose items failed to land would be
        an empty run that reports ``total`` items and delivers none.
        Raises :class:`LineReactivationCampaignConflictError` when — and
        **only** when — the id already exists; the caller decides
        resume-vs-conflict by reading :meth:`get` first, and this is the
        race backstop for two callers that both read ``None``.

        Any other integrity failure (a character id with no row behind
        it, say) must surface as itself. Reporting it as a conflict would
        send the console chasing a new ``campaign_id`` for a condition no
        new id can fix.
        """

    async def list_items(
        self, campaign_id: str,
    ) -> list[LineReactivationCampaignItem]:
        """Every item of one campaign, in a stable order."""

    async def list_pending_items(
        self, campaign_id: str,
    ) -> list[LineReactivationCampaignItem]:
        """Only the items with no outcome — what a resume must still run."""

    async def claim_item(
        self,
        campaign_id: str,
        character_id: str,
        *,
        now: datetime,
        lease: timedelta,
    ) -> bool:
        """Take a leased, exclusive claim on one pending item.

        One conditional statement the database serialises: stamp
        ``claimed_at = now`` where the row is still un-stamped
        (``outcome IS NULL``) **and** either unclaimed or claimed longer
        than ``lease`` ago. ``True`` means this caller owns the send;
        ``False`` means another replica owns it, or already ran it.

        This is the fence that must be crossed *before* the dispatcher is
        called. ``record_outcome``'s ``WHERE outcome IS NULL`` only fences
        the write, which stops the second runner from lying in the ledger
        but not from putting a second message in front of the player.

        The lease exists because a claim can be orphaned: a replica that
        dies mid-send leaves a row nobody will ever stamp, and a claim
        without an expiry would make that row permanently unrunnable.
        ``lease`` must therefore exceed the worst-case single-message
        generation time — see ``DEFAULT_ITEM_CLAIM_LEASE``.
        """

    async def record_outcome(
        self,
        campaign_id: str,
        character_id: str,
        *,
        outcome: str,
        detail: str | None,
        attempted_at: datetime,
        message_text: str | None = None,
    ) -> bool:
        """Stamp one item's outcome.

        Writes ``outcome`` and ``attempted_at`` together so "attempted"
        and "has an outcome" can never disagree. Returns ``True`` when a
        row was updated; ``False`` means the item was already stamped (a
        racing runner won) or does not exist, and the caller must not
        re-send on the strength of it.

        ``message_text`` is the verbatim sent body and defaults to
        ``None`` — keyword-with-default rather than required so the many
        callers that record a skip or a block (where there is no body at
        all) say nothing rather than passing ``None`` by ritual. The
        caller supplies it only alongside a ``sent`` outcome.
        """

    async def mark_completed(
        self, campaign_id: str, *, completed_at: datetime,
    ) -> bool:
        """Flip a running campaign to ``completed``.

        Guarded on the current status so a second runner finishing the
        same campaign cannot re-stamp ``completed_at``. Returns ``True``
        when this call performed the transition.
        """
