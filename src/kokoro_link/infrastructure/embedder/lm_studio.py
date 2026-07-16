"""LM Studio / OpenAI-compatible embeddings adapter.

The /v1/embeddings endpoint accepts either a single string or an array
of strings as ``input``. We always send arrays so ``embed`` and
``embed_many`` share one implementation path. The response preserves
input order, so we zip back by index.

Failure policy (intentionally strict, Phase B hardening):

- HTTP / network / JSON errors raise ``EmbedderError``. We do **not**
  swallow them. Callers decide whether to abort a write (atomic) or
  degrade a read (prompt gets fewer relevant memories that turn).
- A response that's well-formed but missing a slot still raises —
  partial success is treated as failure so the write path can't
  silently persist half-embedded batches.

Rationale: earlier versions returned ``None`` on any failure, which
meant a brief LM Studio hiccup would silently write
embedding-less memories. Those poisoned semantic retrieval later
without any visible symptom. Failing loudly keeps data coherent.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort

_LOGGER = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_BATCH = 32


class LMStudioEmbedder(EmbedderPort):
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        dimension: int = 1024,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        request_dimensions: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._dimension = dimension
        self._timeout_seconds = timeout_seconds
        self._request_dimensions = request_dimensions

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def is_operational(self) -> bool:
        return True

    async def embed(self, text: str) -> tuple[float, ...] | None:
        results = await self.embed_many([text])
        return results[0] if results else None

    async def embed_many(
        self, texts: Sequence[str],
    ) -> list[tuple[float, ...] | None]:
        if not texts:
            return []

        out: list[tuple[float, ...] | None] = []
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            for start in range(0, len(texts), _MAX_BATCH):
                chunk = list(texts[start : start + _MAX_BATCH])
                chunk_vectors = await self._embed_chunk(client, chunk)
                out.extend(chunk_vectors)
        return out

    async def _embed_chunk(
        self,
        client: httpx.AsyncClient,
        chunk: list[str],
    ) -> list[tuple[float, ...]]:
        payload = {"model": self._model, "input": chunk}
        if self._request_dimensions:
            payload["dimensions"] = self._dimension
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = await client.post(
                f"{self._base_url}/embeddings",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise EmbedderError(
                f"LM Studio /embeddings HTTP failure for {len(chunk)} texts: {exc}"
            ) from exc
        except ValueError as exc:  # JSONDecodeError subclasses ValueError
            raise EmbedderError(
                f"LM Studio /embeddings returned non-JSON body: {exc}"
            ) from exc

        data = body.get("data")
        if not isinstance(data, list):
            raise EmbedderError("Embedding response missing 'data' array")

        ordered: list[tuple[float, ...] | None] = [None] * len(chunk)
        for entry in data:
            if not isinstance(entry, dict):
                continue
            raw_vector = entry.get("embedding")
            if not isinstance(raw_vector, list):
                continue
            try:
                vector = tuple(float(v) for v in raw_vector)
            except (TypeError, ValueError):
                continue
            index = entry.get("index")
            if not isinstance(index, int) or not (0 <= index < len(chunk)):
                for i, slot in enumerate(ordered):
                    if slot is None:
                        ordered[i] = vector
                        break
                continue
            ordered[index] = vector

        missing = [i for i, v in enumerate(ordered) if v is None]
        if missing:
            raise EmbedderError(
                f"Embedding response missed {len(missing)} of {len(chunk)} inputs "
                f"(indices {missing[:5]}…)"
            )
        return [v for v in ordered if v is not None]
