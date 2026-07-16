"""Integration-style tests for MemoryConsolidationService.

Uses the in-memory repo + stub consolidator to exercise the full
pipeline (decay + cluster + merge swap) without LLM / DB.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from kokoro_link.application.services.memory_consolidation_service import (
    MemoryConsolidationService,
)
from kokoro_link.contracts.memory_consolidator import MergeProposal
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.decay import DecayPolicy
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository


class _StubConsolidator:
    """Echoes cluster contents into a merged string so we can assert
    on what reached the merge call.
    """

    def __init__(self) -> None:
        self.calls: list[list[MemoryItem]] = []

    async def merge(  # noqa: ANN001
        self, cluster, *, character=None, operator_primary_language="zh-TW",
    ):
        del character, operator_primary_language
        self.calls.append(list(cluster))
        return MergeProposal(
            content="MERGED: " + " / ".join(c.content for c in cluster),
            kind=cluster[0].kind,
            salience=max(c.salience for c in cluster),
            tags=(),
        )


class _SkipConsolidator:
    async def merge(  # noqa: ANN001
        self, cluster, *, character=None, operator_primary_language="zh-TW",
    ):
        del character, operator_primary_language
        return None


class _StubEmbedder:
    """Trivially marks items as embedded by length."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def dimension(self) -> int:
        return 3

    @property
    def is_operational(self) -> bool:
        return True

    async def embed(self, text: str):
        raise NotImplementedError

    async def embed_many(
        self, texts: Sequence[str],
    ) -> list[tuple[float, ...] | None]:
        self.calls += 1
        return [(float(len(t)), 0.0, 0.0) for t in texts]


def _item(
    *,
    content: str,
    character_id: str = "c1",
    kind: MemoryKind = MemoryKind.SEMANTIC,
    salience: float = 0.5,
    age_days: float = 0.0,
    access_count: int = 0,
    embedding: tuple[float, ...] | None = None,
    audience: str = "",
) -> MemoryItem:
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    return MemoryItem(
        id=str(uuid4()),
        character_id=character_id,
        conversation_id=None,
        kind=kind,
        content=content,
        salience=salience,
        created_at=created,
        access_count=access_count,
        embedding=embedding,
        audience=audience,
    )


@pytest.mark.asyncio
async def test_decay_only_removes_stale_items() -> None:
    repo = InMemoryMemoryRepository()
    await repo.add(_item(content="stale", salience=0.1, age_days=200))
    await repo.add(_item(content="fresh", salience=0.1, age_days=2))
    await repo.add(_item(content="important", salience=0.9, age_days=400))

    service = MemoryConsolidationService(
        memory_repository=repo,
        consolidator=_SkipConsolidator(),
    )

    report = await service.consolidate("c1", decay_only=True)
    assert report.decayed == 1
    assert report.memories_after == 2
    remaining = {m.content for m in await repo.list_all_for_character("c1")}
    assert remaining == {"fresh", "important"}


@pytest.mark.asyncio
async def test_dry_run_does_not_mutate() -> None:
    repo = InMemoryMemoryRepository()
    await repo.add(_item(content="stale", salience=0.1, age_days=200))
    await repo.add(_item(
        content="a", salience=0.7, embedding=(1.0, 0.0, 0.0),
    ))
    await repo.add(_item(
        content="b", salience=0.7, embedding=(0.99, 0.0, 0.0),
    ))

    service = MemoryConsolidationService(
        memory_repository=repo,
        consolidator=_StubConsolidator(),
    )
    report = await service.consolidate("c1", dry_run=True)
    assert report.decayed == 1
    assert report.clusters_found >= 1
    # but nothing actually removed
    assert await repo.count_for_character("c1") == 3


@pytest.mark.asyncio
async def test_consolidation_swaps_cluster_for_merge() -> None:
    repo = InMemoryMemoryRepository()
    await repo.add(_item(
        content="使用者喜歡咖啡", salience=0.6,
        embedding=(1.0, 0.0, 0.0),
    ))
    await repo.add(_item(
        content="Alex 愛手沖", salience=0.7,
        embedding=(0.98, 0.0, 0.0),
    ))
    await repo.add(_item(
        content="使用者住在台中", salience=0.8,
        embedding=(0.0, 1.0, 0.0),  # different cluster
    ))

    consolidator = _StubConsolidator()
    service = MemoryConsolidationService(
        memory_repository=repo,
        consolidator=consolidator,
        embedder=_StubEmbedder(),
    )
    report = await service.consolidate(
        "c1", similarity_threshold=0.9,
    )

    assert report.clusters_found == 1
    assert report.clusters_merged == 1
    assert report.memories_replaced == 2

    remaining = await repo.list_all_for_character("c1")
    contents = {m.content for m in remaining}
    # the unrelated memory survives
    assert "使用者住在台中" in contents
    # the two clustered memories are replaced by one merge
    assert any(c.startswith("MERGED:") for c in contents)
    assert len(remaining) == 2


@pytest.mark.asyncio
async def test_merge_preserves_private_audience() -> None:
    # Privacy is monotone: if any merged member was private, the merge
    # stays private so a consolidation can't de-privatise (and re-expose
    # to the feed) a memory the extractor judged private.
    repo = InMemoryMemoryRepository()
    await repo.add(_item(
        content="使用者要我叫他森森", salience=0.6,
        embedding=(1.0, 0.0, 0.0), audience="private",
    ))
    await repo.add(_item(
        content="使用者的暱稱", salience=0.7,
        embedding=(0.98, 0.0, 0.0), audience="shareable",
    ))

    service = MemoryConsolidationService(
        memory_repository=repo,
        consolidator=_StubConsolidator(),
        embedder=_StubEmbedder(),
    )
    report = await service.consolidate("c1", similarity_threshold=0.9)

    assert report.clusters_merged == 1
    merged = [
        m for m in await repo.list_all_for_character("c1")
        if m.content.startswith("MERGED:")
    ]
    assert len(merged) == 1
    assert merged[0].audience == "private"
    assert merged[0].is_shareable_to_feed is False


@pytest.mark.asyncio
async def test_consolidation_skip_when_consolidator_returns_none() -> None:
    repo = InMemoryMemoryRepository()
    await repo.add(_item(
        content="a", salience=0.6, embedding=(1.0, 0.0, 0.0),
    ))
    await repo.add(_item(
        content="b", salience=0.6, embedding=(0.99, 0.0, 0.0),
    ))

    service = MemoryConsolidationService(
        memory_repository=repo,
        consolidator=_SkipConsolidator(),
        embedder=_StubEmbedder(),
    )
    report = await service.consolidate("c1")
    assert report.clusters_found == 1
    assert report.clusters_merged == 0
    assert report.memories_after == 2  # nothing replaced


@pytest.mark.asyncio
async def test_decay_policy_override() -> None:
    repo = InMemoryMemoryRepository()
    await repo.add(_item(content="moderate", salience=0.4, age_days=50))

    service = MemoryConsolidationService(
        memory_repository=repo,
        consolidator=_SkipConsolidator(),
    )
    report = await service.consolidate(
        "c1",
        decay_only=True,
        decay_policy=DecayPolicy(min_salience=0.5, max_age_days=30),
    )
    assert report.decayed == 1


@pytest.mark.asyncio
async def test_no_cluster_when_all_kinds_differ() -> None:
    repo = InMemoryMemoryRepository()
    await repo.add(_item(
        content="fact", salience=0.7,
        embedding=(1.0, 0.0, 0.0), kind=MemoryKind.SEMANTIC,
    ))
    await repo.add(_item(
        content="event", salience=0.7,
        embedding=(1.0, 0.0, 0.0), kind=MemoryKind.EPISODIC,
    ))

    service = MemoryConsolidationService(
        memory_repository=repo,
        consolidator=_StubConsolidator(),
        embedder=_StubEmbedder(),
    )
    report = await service.consolidate("c1")
    assert report.clusters_found == 0
