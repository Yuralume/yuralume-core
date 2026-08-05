"""Translator that already has the answer.

An official card arrives from the Cloud catalog with its prose already
translated into the player's language: a human approved it in the console
long before anybody browsed the gallery. So the import path's translation
step has nothing to ask a model — it has a payload to apply.

This adapter is that step. It satisfies
:class:`CharacterCardTranslatorPort`, so it drops into the place the import
pipeline already had for a translator instead of adding a stage: the only
change on the import side is one optional ``profile_translator`` keyword on
:meth:`CharacterCardImportService.import_card`, which selects this adapter
over the LLM one. Every caller that does not pass it — manual upload,
local pack install — reaches exactly the code it reached before. The payload
is applied through :func:`merge_translated_profile`, the same merge policy
the LLM translator uses: same-length lists or nothing, never blank the
source, never touch structural fields.

**It never calls a model, and it is never installed when there is nothing
to apply.** Whether an approved payload should land at all — empty, or
marked ``stale`` because it was approved against an older version of the
artifact — is decided one layer up, in
:func:`kokoro_link.application.services.official_card_pack_source._profile_translator`,
which simply does not construct this decorator in those cases. Keeping the
judgement there and the merge here is deliberate: this class applies a
payload, and a payload it was handed is a payload somebody decided to
trust.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kokoro_link.application.dto.character_card import CharacterCardProfile
from kokoro_link.contracts.character_card_translator import (
    CharacterCardTranslatorPort,
)
from kokoro_link.infrastructure.character_card.llm_translator import (
    merge_translated_profile,
)


class PreTranslatedCardTranslator(CharacterCardTranslatorPort):
    """Applies an approved translation payload; consults nothing."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = dict(payload or {})

    @property
    def has_payload(self) -> bool:
        return bool(self._payload)

    async def translate_profile(
        self,
        profile: CharacterCardProfile,
        *,
        target_language: str,
    ) -> CharacterCardProfile:
        """Merge the approved payload into ``profile``.

        ``target_language`` is ignored: the payload was translated into one
        specific language by the catalog, and the caller decided that is the
        language this player asked for. Re-deciding here — say, refusing a
        payload whose language tag looks different — would let a tag
        mismatch silently spend the player's credits on an LLM translation
        of text that was already correct.
        """
        return merge_translated_profile(profile, self._payload)


__all__ = ["PreTranslatedCardTranslator"]
