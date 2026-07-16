from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.infrastructure.diversity.reply_evidence import (
    build_reply_diversity_evidence,
)


class _Embedder:
    dimension = 2
    is_operational = True

    def __init__(self, vectors):
        self.vectors = vectors

    async def embed(self, text: str):
        del text
        return None

    async def embed_many(self, texts):
        del texts
        if isinstance(self.vectors, Exception):
            raise self.vectors
        return self.vectors


def _assistant(text: str) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text, created_at=_NOW)


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text, created_at=_NOW)


_NOW = datetime(2026, 6, 22, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_reply_diversity_evidence_computes_embedding_similarity() -> None:
    evidence = await build_reply_diversity_evidence(
        recent_messages=[
            _user("嗨"),
            _assistant("第一句"),
            _assistant("第二句"),
            _assistant("第三句"),
        ],
        self_repetition_hint="近期常用同一種開場。",
        embedder=_Embedder([(1.0, 0.0), (0.9, 0.1), (0.0, 1.0)]),
    )

    assert evidence.assistant_line_count == 3
    assert evidence.max_self_similarity is not None
    assert evidence.max_self_similarity > 0.9
    assert evidence.mean_self_similarity is not None
    assert evidence.self_repetition_hint == "近期常用同一種開場。"
    assert evidence.phrase_frequency_lines
    assert evidence.metadata["embedding_checked"] is True


@pytest.mark.asyncio
async def test_reply_diversity_evidence_fails_soft_when_embedder_fails() -> None:
    evidence = await build_reply_diversity_evidence(
        recent_messages=[_assistant("第一句"), _assistant("第二句")],
        embedder=_Embedder(RuntimeError("embed down")),
    )

    assert evidence.assistant_line_count == 2
    assert evidence.max_self_similarity is None
    assert "embed down" in evidence.metadata["embedding_error"]


@pytest.mark.asyncio
async def test_reply_diversity_evidence_skips_similarity_without_embedder() -> None:
    evidence = await build_reply_diversity_evidence(
        recent_messages=[_assistant("第一句"), _assistant("第二句")],
        self_repetition_hint="",
        embedder=None,
    )

    assert evidence.assistant_line_count == 2
    assert evidence.max_self_similarity is None
    assert evidence.phrase_frequency_lines == ()
