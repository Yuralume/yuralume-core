"""Memory consolidator port.

Given a batch of memories that the clustering step flagged as
semantically redundant, produce a single merged replacement. The
consolidator is **only** responsible for the merge step — the
clustering and persistence happen in the application service so the
LLM client stays stateless and easy to swap (e.g. for a NullConsolidator
when the default provider is ``fake``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind


@dataclass(frozen=True, slots=True)
class MergeProposal:
    """What the consolidator thinks the cluster should collapse to."""

    content: str
    kind: MemoryKind
    salience: float
    tags: tuple[str, ...] = field(default_factory=tuple)


class MemoryConsolidatorPort(Protocol):
    async def merge(
        self,
        cluster: list[MemoryItem],
        *,
        character: Character | None = None,
        operator_primary_language: str = "zh-TW",
    ) -> MergeProposal | None:
        """Return a merged memory for ``cluster``.

        Returns ``None`` when the consolidator has no useful merge (the
        LLM refused, the output was malformed, the cluster is too small
        to bother). The caller leaves the cluster untouched in that case.

        ``character`` (optional) lets the consolidator route through the
        per-character LLM override chain so cluster merging follows the
        same pin operators set for chat / post-turn extraction. Implementations
        that don't talk to the LLM (e.g. ``NullMemoryConsolidator``)
        should ignore it.

        ``operator_primary_language`` (BCP 47) is the content language for
        the player-visible merged ``content`` shown in MemoryBrowserPanel.
        Without it the consolidator's Chinese scaffolding can re-Sinicise
        a merged memory even when the source memories are English /
        Japanese. Defaults to ``zh-TW`` (ship-first) for legacy callers.
        """
