"""In-memory campaign ledger for LINE dormant reactivation (LR T1).

Same semantics as the SA adapter, including the three guards that make
the D5 idempotency story hold: :meth:`create` refuses a duplicate id,
:meth:`claim_item` hands exclusive leased ownership of a pending row to
one caller, and :meth:`record_outcome` only stamps an item that is still
pending.

Single-process by nature, so the claim can never actually contend here —
it is implemented anyway because the *service* tests exercise two service
instances over one repository, which is precisely the two-replica shape
the SQL adapter has to survive.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta

from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.line_reactivation import (
    CAMPAIGN_STATUS_COMPLETED,
    LineReactivationCampaign,
    LineReactivationCampaignConflictError,
    LineReactivationCampaignItem,
    LineReactivationCampaignRepositoryPort,
)


class InMemoryLineReactivationCampaignRepository(
    LineReactivationCampaignRepositoryPort,
):
    def __init__(self) -> None:
        self._campaigns: dict[str, LineReactivationCampaign] = {}
        #: campaign_id -> character_id -> item, insertion ordered.
        self._items: dict[str, dict[str, LineReactivationCampaignItem]] = {}

    async def get(
        self, campaign_id: str,
    ) -> LineReactivationCampaign | None:
        return self._campaigns.get(campaign_id)

    async def create(
        self,
        campaign: LineReactivationCampaign,
        character_ids: Sequence[str],
    ) -> None:
        if campaign.campaign_id in self._campaigns:
            raise LineReactivationCampaignConflictError(
                f"campaign {campaign.campaign_id!r} already exists",
            )
        self._campaigns[campaign.campaign_id] = campaign
        self._items[campaign.campaign_id] = {
            character_id: LineReactivationCampaignItem(
                campaign_id=campaign.campaign_id,
                character_id=character_id,
            )
            for character_id in dict.fromkeys(character_ids)
        }

    async def list_items(
        self, campaign_id: str,
    ) -> list[LineReactivationCampaignItem]:
        return self._sorted(campaign_id)

    async def list_pending_items(
        self, campaign_id: str,
    ) -> list[LineReactivationCampaignItem]:
        return [item for item in self._sorted(campaign_id) if item.pending]

    async def claim_item(
        self,
        campaign_id: str,
        character_id: str,
        *,
        now: datetime,
        lease: timedelta,
    ) -> bool:
        items = self._items.get(campaign_id)
        if items is None:
            return False
        item = items.get(character_id)
        if item is None or not item.pending:
            return False
        moment = ensure_utc(now)
        held = item.claimed_at
        if held is not None and ensure_utc(held) >= moment - lease:
            return False
        items[character_id] = replace(item, claimed_at=moment)
        return True

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
        items = self._items.get(campaign_id)
        if items is None:
            return False
        item = items.get(character_id)
        if item is None or not item.pending:
            return False
        items[character_id] = LineReactivationCampaignItem(
            campaign_id=campaign_id,
            character_id=character_id,
            outcome=outcome,
            detail=detail,
            message_text=message_text,
            attempted_at=ensure_utc(attempted_at),
            claimed_at=item.claimed_at,
        )
        return True

    async def mark_completed(
        self, campaign_id: str, *, completed_at: datetime,
    ) -> bool:
        campaign = self._campaigns.get(campaign_id)
        if campaign is None or not campaign.running:
            return False
        self._campaigns[campaign_id] = LineReactivationCampaign(
            campaign_id=campaign.campaign_id,
            actor=campaign.actor,
            status=CAMPAIGN_STATUS_COMPLETED,
            created_at=campaign.created_at,
            total=campaign.total,
            completed_at=ensure_utc(completed_at),
        )
        return True

    def _sorted(self, campaign_id: str) -> list[LineReactivationCampaignItem]:
        items = self._items.get(campaign_id)
        if not items:
            return []
        return sorted(items.values(), key=lambda item: item.character_id)
