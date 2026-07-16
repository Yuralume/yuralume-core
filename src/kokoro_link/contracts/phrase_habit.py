"""Port for the phrase-habit extractor (HUMANIZATION_ROADMAP §3.3).

Reads a window of the character's own recent assistant lines and asks
the LLM to name verbal habits (口頭禪 / 結尾語助詞 / 慣用句式) the
character keeps reusing. Output is at most a handful of short strings.
"""

from __future__ import annotations

from typing import Protocol


class PhraseHabitExtractorPort(Protocol):
    async def extract(
        self, *, character_name: str, recent_lines: list[str],
    ) -> list[str]:
        """Return ≤5 short Chinese-phrase descriptions of habitual usage.

        Empty list when the provider is unavailable, the lines are too
        few to reason about, or the LLM produced nothing usable. The
        caller treats empty as "no update this pass" — repository rows
        from prior passes still survive via TTL / observed_count
        reinforcement.
        """


class NullPhraseHabitExtractor(PhraseHabitExtractorPort):
    """Pass-through when the feature is intentionally disabled."""

    async def extract(
        self, *, character_name: str, recent_lines: list[str],
    ) -> list[str]:
        return []
