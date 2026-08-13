"""Cloud->Core per-card exclusive-freeze channel (D7 / EC10-A / EC10-B).

EC10-A (Cloud) owns the admin switch on one official card's contract-
termination freeze and enqueues a durable-outbox message keyed by
``card_id``; this service is the Core-side consumer, freezing / thawing
every character installed from that card (``origin_official_card_id``)
across every tenant — a card's IP-partner contract ending silences every
character built from it site-wide, which a tenant-scoped freeze cannot
express.

Reuses the same ``frozen`` / ``frozen_reason`` freeze chain as ``idle`` /
``manual`` (CHARACTER_FREEZE_PLAN), not the orthogonal ``subscription_locked``
projection the tenant subscription-freeze channel writes — the two
mechanisms are independent columns and never race each other.
``FREEZE_REASON_EXCLUSIVE_CONTRACT_END`` is deliberately excluded from
``CHAT_THAWABLE_FREEZE_REASONS`` (see ``domain.entities.character``) so a
user's foreground chat turn can never silently undo a card-level freeze;
only a matching message from this same channel clears it. Beyond not
thawing, the chat entry point *refuses* turns on such a character
(``ChatCharacterContractEndedError``) — D7's pinned semantics are "資料
保留、不可互動", so a withdrawn character has to actually stop answering,
not merely stop its background schedule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import (
    FREEZE_REASON_EXCLUSIVE_CONTRACT_END,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExclusiveCardFreezeResult:
    """Outcome of one per-card freeze / unfreeze batch.

    ``characters`` is how many rows carry this card's provenance;
    ``frozen`` / ``unfrozen`` count characters actually flipped this run
    (a row already in the desired state is skipped, not recounted, so a
    durable-outbox retry is cheap and does not reset ``frozen_at``);
    ``failures`` counts per-character repository errors that were skipped
    (the batch does not abort on them)."""

    characters: int = 0
    frozen: int = 0
    unfrozen: int = 0
    failures: int = 0


class ExclusiveCardFreezeService:
    """Freeze / thaw every character installed from one official card."""

    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        clock: ClockPort,
    ) -> None:
        self._character_repository = character_repository
        self._clock = clock

    async def freeze_card(self, card_id: str) -> ExclusiveCardFreezeResult:
        """Freeze every character with ``origin_official_card_id == card_id``.

        Idempotent, and a no-op on any row that is *already frozen* — for
        this reason or any other. Overwriting a foreign ``frozen_reason``
        (``manual``, legacy ``subscription_lapse`` …) bought nothing, since
        the character is equally silent under either provenance, but it
        destroyed the record of who froze it: :meth:`unfreeze_card` clears
        every row carrying *this* reason, so a relabelled row would have
        its admin sanction lifted as a side effect of the contract being
        renewed. Provenance therefore stays with whoever got there first.

        Accepted residual: the mirror case is not defended. If an admin
        manually unfreezes a row while a card freeze is in force, that row
        escapes the card freeze until the next ``freeze_card`` call — the
        admin console's unfreeze is unconditional by design and this
        service has no standing hook to re-assert itself. The escape is
        loud (an admin took an explicit action) where the overwrite was
        silent, which is the trade being made."""
        now = self._resolve_now()
        characters = await self._characters_for(card_id)
        frozen = 0
        failures = 0
        for character in characters:
            if character.frozen:
                continue
            try:
                did_freeze = await self._character_repository.set_frozen(
                    character.id,
                    frozen=True,
                    now=now,
                    reason=FREEZE_REASON_EXCLUSIVE_CONTRACT_END,
                )
            except Exception:
                failures += 1
                _LOGGER.exception(
                    "exclusive card freeze: freeze failed character=%s "
                    "card=%s",
                    character.id, card_id,
                )
                continue
            if did_freeze:
                frozen += 1
        _LOGGER.info(
            "exclusive card freeze: card=%s characters=%d frozen=%d "
            "failures=%d",
            card_id, len(characters), frozen, failures,
        )
        return ExclusiveCardFreezeResult(
            characters=len(characters), frozen=frozen, failures=failures,
        )

    async def unfreeze_card(self, card_id: str) -> ExclusiveCardFreezeResult:
        """Unfreeze every character this channel itself froze.

        Only rows whose ``frozen_reason`` is exactly
        ``exclusive_contract_end`` are touched — a character frozen for an
        unrelated reason (admin ``manual``, legacy ``subscription_lapse``)
        is left exactly as it was, so this channel can never undo a freeze
        it did not set."""
        characters = await self._characters_for(card_id)
        unfrozen = 0
        failures = 0
        for character in characters:
            if character.frozen_reason != FREEZE_REASON_EXCLUSIVE_CONTRACT_END:
                continue
            try:
                did_unfreeze = await self._character_repository.set_frozen(
                    character.id, frozen=False, now=self._resolve_now(),
                )
            except Exception:
                failures += 1
                _LOGGER.exception(
                    "exclusive card freeze: unfreeze failed character=%s "
                    "card=%s",
                    character.id, card_id,
                )
                continue
            if did_unfreeze:
                unfrozen += 1
        _LOGGER.info(
            "exclusive card freeze: card=%s characters=%d unfrozen=%d "
            "failures=%d",
            card_id, len(characters), unfrozen, failures,
        )
        return ExclusiveCardFreezeResult(
            characters=len(characters), unfrozen=unfrozen, failures=failures,
        )

    async def _characters_for(self, card_id: str):
        return await self._character_repository.list_by_origin_official_card_id(
            card_id,
        )

    def _resolve_now(self) -> datetime:
        return ensure_utc(self._clock.now())
