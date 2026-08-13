"""Resolve the content language an LLM call is producing output for.

The prompt layer already tells the model which language to write in
(``infrastructure.prompt.operator_language``), but the *adapter* layer
never learns it — ``ChatModelPort.generate`` takes a prompt and nothing
else. Output post-processing that depends on the language (today:
Simplified→Traditional script normalisation) therefore needs the fact
carried down separately, and this service is where the active-LLM
providers get it while resolving a model.

Resolution order is ``operator_id`` → ``character.user_id``, because a
call made on behalf of an explicit actor should follow that actor even
when a character is also in scope. An unknown or unstored operator
yields ``""``, which every consumer must treat as "do nothing" — a
missing language is not a reason to guess one.

The lookup is cached: it runs on every model resolve (several times per
turn), while the value behind it is close to immutable — the locale
guardrail in ``player_locale_service`` caps changes at two per thirty
days. A short TTL keeps a genuine change visible within a minute
without asking the database each time.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict

from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 60.0
"""Long enough that one turn's several resolves share a single read,
short enough that a player who just changed their language sees the
effect before they can finish wondering whether it worked."""

_MAX_CACHED_OPERATORS = 512
"""Bound for the hosted case, where the operator set is unbounded.
Evicts least-recently-used; a miss costs one indexed primary-key read."""


class OperatorOutputLanguageResolver:
    """Cached ``operator → primary_language`` lookup for adapter binding."""

    def __init__(
        self,
        *,
        repository: OperatorProfileRepositoryPort,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._repository = repository
        self._ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()

    async def resolve(
        self,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
    ) -> str:
        """Return the BCP 47 content language, or ``""`` when unknown.

        Never raises: a persistence failure here would abort a turn over
        a post-processing hint, so it degrades to ``""`` (no
        normalisation) and logs.
        """
        resolved_id = (operator_id or "").strip()
        if not resolved_id and character is not None:
            resolved_id = (getattr(character, "user_id", "") or "").strip()
        if not resolved_id:
            return ""

        cached = self._read_cache(resolved_id)
        if cached is not None:
            return cached

        try:
            profile = await self._repository.get(resolved_id)
        except Exception:  # noqa: BLE001 — see docstring
            _LOGGER.exception(
                "output language lookup failed for operator %s; "
                "skipping language-dependent post-processing",
                resolved_id,
            )
            return ""

        language = (getattr(profile, "primary_language", "") or "").strip()
        self._write_cache(resolved_id, language)
        return language

    def invalidate(self, operator_id: str) -> None:
        """Drop a cached entry, for callers that just wrote a new locale
        and want the change to take effect before the TTL lapses."""
        self._cache.pop((operator_id or "").strip(), None)

    # ---- cache ---------------------------------------------------------

    def _read_cache(self, operator_id: str) -> str | None:
        entry = self._cache.get(operator_id)
        if entry is None:
            return None
        language, expires_at = entry
        if expires_at <= time.monotonic():
            del self._cache[operator_id]
            return None
        self._cache.move_to_end(operator_id)
        return language

    def _write_cache(self, operator_id: str, language: str) -> None:
        self._cache[operator_id] = (
            language,
            time.monotonic() + self._ttl_seconds,
        )
        self._cache.move_to_end(operator_id)
        while len(self._cache) > _MAX_CACHED_OPERATORS:
            self._cache.popitem(last=False)
