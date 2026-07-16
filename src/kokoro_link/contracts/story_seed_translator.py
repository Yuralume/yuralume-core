"""Port for optional story-seed one-liner translation.

``cli.import_story_seeds --translate`` can ask an LLM to render each
bundled seed's one-line prompt in the operator's primary language so the
seed management UI reads natively. Adapters must be fail-soft: any
provider, parsing, or length-mismatch issue returns the original texts
so a translation problem never blocks the (idempotent) import.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence


class StorySeedTranslatorPort(ABC):
    @abstractmethod
    async def translate_seed_texts(
        self,
        seed_texts: Sequence[str],
        *,
        target_language: str,
    ) -> list[str]:
        """Translate a batch of one-line seed prompts.

        Returns a list of the same length and order. On any failure the
        implementation returns the original texts unchanged (fail-soft);
        a length mismatch from the model must be treated as failure so a
        seed is never paired with the wrong translation.
        """
