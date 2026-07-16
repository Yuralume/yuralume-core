"""The dream pass runs the relationship-coherence tail, fail-soft.

The coherence self-heal is wired as the last dream tail stage. It must be
invoked once per pass, and a failure inside it must never break the dream
pass (the consolidation plan the pass already applied must still return).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kokoro_link.application.services.persona_dream_service import (
    PersonaDreamService,
)
from kokoro_link.bootstrap.settings import PersonaSettings
from kokoro_link.contracts.persona_consolidator import ConsolidationResult


_CHAR_ID = "char-A"
_OP_ID = "op-1"


def _build_service(coherence) -> PersonaDreamService:
    repo = AsyncMock()
    repo.list_pending = AsyncMock(return_value=[])
    repo.list_confirmed_for_decay = AsyncMock(return_value=[])
    consolidator = AsyncMock()
    consolidator.consolidate = AsyncMock(return_value=ConsolidationResult())
    persona_service = MagicMock()
    persona_service.get_current = AsyncMock()
    persona_service.invalidate_cache = MagicMock()
    svc = PersonaDreamService(
        consolidator=consolidator,
        repository=repo,
        persona_service=persona_service,
        settings=PersonaSettings(),
    )
    svc.set_relationship_coherence_service(coherence)
    return svc


@pytest.mark.asyncio
async def test_coherence_tail_is_invoked_once_per_pass():
    coherence = MagicMock()
    coherence.heal_pair = AsyncMock()
    svc = _build_service(coherence)

    await svc.run_consolidation(_CHAR_ID, _OP_ID, now=datetime.now(timezone.utc))

    coherence.heal_pair.assert_awaited_once_with(_CHAR_ID, _OP_ID)


@pytest.mark.asyncio
async def test_coherence_failure_does_not_break_dream_pass():
    coherence = MagicMock()
    coherence.heal_pair = AsyncMock(side_effect=RuntimeError("boom"))
    svc = _build_service(coherence)

    # must not raise — the dream pass returns its (empty) plan
    result = await svc.run_consolidation(
        _CHAR_ID, _OP_ID, now=datetime.now(timezone.utc),
    )

    assert isinstance(result, ConsolidationResult)
    coherence.heal_pair.assert_awaited_once()
