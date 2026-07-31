"""Character draft application service.

Thin wrapper around the generator port — keeps the API layer free of
domain knowledge about image handling and response shaping.

It is also one of the four AP2 action-priced entry points: a draft is a single
player-visible action ("suggest me a character"), so under
``billing_shape=action_fixed`` it costs one fixed price regardless of how the
generator splits the work internally.
"""

from __future__ import annotations

from kokoro_link.application.dto.character_draft import CharacterDraftResponse
from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
    NullActionBillingService,
)
from kokoro_link.contracts.character_draft import (
    CharacterDraftGeneratorPort,
    ImageInput,
)
from kokoro_link.contracts.cloud_action_billing import ACTION_CHARACTER_DRAFT


class CharacterDraftService:
    def __init__(
        self,
        generator: CharacterDraftGeneratorPort,
        *,
        action_billing: (
            CloudActionBillingService | NullActionBillingService | None
        ) = None,
    ) -> None:
        self._generator = generator
        self._action_billing = action_billing or NullActionBillingService()

    async def generate(
        self,
        *,
        prompt: str | None,
        image: ImageInput | None,
        operator_primary_language: str = "zh-TW",
        operator_id: str | None = None,
        quoted_price_cr: float | None = None,
    ) -> CharacterDraftResponse:
        """Draft one character, charged as a single ``character_draft`` action.

        R9: ``quoted_price_cr`` is the price the player's own client had on
        screen when they pressed the button. It binds the charge to what was
        displayed instead of to Core's process-local cache, which can hold a
        number this player never saw (a second replica, or a cache refreshed
        between the quote and the send). ``None`` — an older client, self-host,
        or nothing unambiguous to quote — falls back to the cache, which is the
        pre-R9 behaviour.
        """
        async with self._action_billing.action(
            ACTION_CHARACTER_DRAFT,
            operator_id=operator_id or "",
            quoted_price_cr=quoted_price_cr,
        ):
            draft = await self._generator.generate(
                prompt=prompt,
                image=image,
                operator_primary_language=operator_primary_language,
                operator_id=operator_id,
            )
        return CharacterDraftResponse.from_domain(draft)
