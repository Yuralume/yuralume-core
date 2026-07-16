"""Build deterministic diversity evidence for reply generation and gates."""

from __future__ import annotations

import math
from collections.abc import Sequence

from kokoro_link.contracts.embedder import EmbedderPort
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.domain.entities.conversation import Message, MessageRole

_MAX_ASSISTANT_LINES = 8


async def build_reply_diversity_evidence(
    *,
    recent_messages: Sequence[Message],
    self_repetition_hint: str | None = None,
    embedder: EmbedderPort | None = None,
) -> ReplyDiversityEvidence:
    assistant_lines = _recent_assistant_lines(recent_messages)
    metadata: dict[str, object] = {}
    max_similarity: float | None = None
    mean_similarity: float | None = None
    if embedder is not None and getattr(embedder, "is_operational", False):
        try:
            vectors = await embedder.embed_many(assistant_lines)
            similarities = _pairwise_cosine([
                vector for vector in vectors if vector is not None
            ])
            if similarities:
                max_similarity = max(similarities)
                mean_similarity = sum(similarities) / len(similarities)
            metadata["embedding_checked"] = True
        except Exception as exc:  # fail-soft evidence path
            metadata["embedding_error"] = repr(exc)
    return ReplyDiversityEvidence(
        assistant_line_count=len(assistant_lines),
        max_self_similarity=max_similarity,
        mean_self_similarity=mean_similarity,
        self_repetition_hint=(self_repetition_hint or "").strip(),
        phrase_frequency_lines=_frequency_lines(self_repetition_hint),
        metadata=metadata,
    )


def _recent_assistant_lines(messages: Sequence[Message]) -> list[str]:
    lines: list[str] = []
    for message in reversed(messages):
        if message.role is not MessageRole.ASSISTANT:
            continue
        text = " ".join(message.content.strip().split())
        if not text:
            continue
        lines.append(text)
        if len(lines) >= _MAX_ASSISTANT_LINES:
            break
    return list(reversed(lines))


def _frequency_lines(self_repetition_hint: str | None) -> tuple[str, ...]:
    hint = (self_repetition_hint or "").strip()
    if not hint:
        return ()
    return (
        "self_repetition extractor 已點名近期模式；請把它當成頻率窗 evidence，而不是禁句清單。",
    )


def _pairwise_cosine(vectors: Sequence[Sequence[float]]) -> list[float]:
    similarities: list[float] = []
    for left_index in range(len(vectors)):
        for right_index in range(left_index + 1, len(vectors)):
            value = _cosine(vectors[left_index], vectors[right_index])
            if value is not None:
                similarities.append(value)
    return similarities


def _cosine(left: Sequence[float], right: Sequence[float]) -> float | None:
    if not left or not right or len(left) != len(right):
        return None
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return None
    return dot / (left_norm * right_norm)
