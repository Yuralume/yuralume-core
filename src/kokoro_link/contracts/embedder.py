"""Text embedding port.

An embedder converts a text snippet into a fixed-length float vector
suitable for cosine-similarity search. Used to enrich memory items and
query contexts for semantic retrieval.

Two implementations are shipped:

- ``LMStudioEmbedder``: OpenAI-compatible ``/v1/embeddings`` client,
  wired to the user's local LM Studio (e.g. ``text-embedding-bge-m3``,
  1024-dim). Network / HTTP failures raise ``EmbedderError`` — callers
  decide whether to abort a write or degrade a read.
- ``NullEmbedder``: **intentional** no-op used only when the default
  LLM provider is ``fake`` (i.e. there's no real model in the first
  place). Returns ``None`` so callers short-circuit to the legacy
  salience × recency ranker without writing broken data.

Silent fallback was rejected on purpose: if a user forgets to start
LM Studio, we must not quietly write embedding-less memories — those
would poison semantic retrieval later without any visible symptom.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class EmbedderError(RuntimeError):
    """Raised when a configured embedder cannot produce vectors.

    Distinct from an ordinary exception so callers (ChatService,
    ScheduleMemorializer, backfill CLI) can catch specifically this
    class and decide their policy — typically "skip the write,
    preserve the pending state, surface the failure in logs".
    """


class EmbedderPort(Protocol):
    @property
    def dimension(self) -> int:
        """Size of the vectors returned."""

    @property
    def is_operational(self) -> bool:
        """``True`` for embedders that are expected to produce real
        vectors. ``NullEmbedder`` returns ``False`` so write paths can
        distinguish "no embedder configured, skipping by design" from
        "embedder configured but currently failing".
        """

    async def embed(self, text: str) -> tuple[float, ...] | None:
        """Return the vector for ``text``.

        Operational embedders raise ``EmbedderError`` on
        connectivity / HTTP failures; only the null implementation
        returns ``None`` intentionally.
        """

    async def embed_many(
        self, texts: Sequence[str],
    ) -> list[tuple[float, ...] | None]:
        """Batch embed. Operational embedders either return all
        vectors (guaranteed non-``None``) or raise
        ``EmbedderError``. The null embedder returns all ``None``.
        """
