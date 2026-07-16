"""Port for LLM normalization of SillyTavern free-text fields.

SillyTavern cards carry a character as free-text prose (``description`` /
``personality`` / ``scenario`` / ``mes_example``). Core stores a character
as structured fields (``personality: list[str]`` / ``interests`` /
``boundaries`` / ``aspirations`` / ``appearance`` / ``speaking_style``).

Per CLAUDE.md's LLM-first rule (D4) this projection is an LLM semantic
transform, not regex/keyword splitting. This port mirrors the shape of
``CharacterDraftGeneratorPort``: a single ``normalize`` call that turns
the ST prose into the structured Core fields.

Adapters must be **fail-soft**: any provider / parsing / validation
problem returns a best-effort degraded result (raw ``description`` as
``summary``, other fields empty) rather than raising, so a normalization
hiccup never blocks importing a valid card.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SillyTavernNormalizerInput:
    """Free-text evidence extracted from a SillyTavern card.

    ``first_mes`` and ``mes_example`` are **tone evidence only** (D9) —
    the normalizer may read them to infer ``speaking_style`` but must not
    copy them verbatim into any field. ``scenario`` seeds only the
    wizard suggestion (D5), never a relationship."""

    name: str = ""
    description: str = ""
    personality: str = ""
    scenario: str = ""
    mes_example: str = ""
    first_mes: str = ""
    operator_primary_language: str = "zh-TW"


@dataclass(frozen=True, slots=True)
class SillyTavernNormalizedProfile:
    """Structured Core fields derived from the ST prose.

    ``suggested_known_context`` is a neutral rewrite of ``scenario`` for
    the initial-relationship wizard (D5). It is returned alongside — never
    folded into the profile — because it must never auto-apply to a
    relationship; the importer confirms it in the wizard first.
    """

    summary: str = ""
    personality: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    boundaries: list[str] = field(default_factory=list)
    aspirations: list[str] = field(default_factory=list)
    appearance: str = ""
    speaking_style: str = ""
    suggested_known_context: str = ""


class SillyTavernNormalizerPort(Protocol):
    async def normalize(
        self,
        payload: SillyTavernNormalizerInput,
        *,
        operator_id: str | None = None,
    ) -> SillyTavernNormalizedProfile:
        """Turn ST free-text prose into structured Core profile fields.

        Fail-soft: return a degraded profile on any failure, never raise.
        """
