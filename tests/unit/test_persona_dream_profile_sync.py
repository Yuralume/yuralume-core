"""Unit tests for the dream service's OperatorProfile boundary.

Per-character persona names must not silently become global
OperatorProfile.display_name values. Global profile data is
operator-declared; learned names stay scoped to the character that
heard them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kokoro_link.application.services.persona_dream_service import (
    PersonaDreamService,
)
from kokoro_link.bootstrap.settings import PersonaSettings
from kokoro_link.contracts.persona_consolidator import (
    ConsolidationResult,
    PromoteAction,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    EvidenceRef,
    ProfileField,
)


_CHAR_ID = "char-A"
_OP_ID = "default"


def _candidate(
    *, candidate_id: str = "cand-1", field_key: str = "name",
    layer: int = 1, value: str = "丹尼",
    content_mode: MessageContentMode = MessageContentMode.NORMAL,
) -> CandidateField:
    return CandidateField(
        field_key=field_key,
        layer=layer,
        proposed_value=value,
        evidence_ref=EvidenceRef(
            turn_id="t",
            conversation_id="c",
            quote="我叫丹尼",
            extracted_at=datetime.now(timezone.utc),
        ),
        raw_extractor_confidence=0.85,
        candidate_id=candidate_id,
        content_mode=content_mode,
        character_id=_CHAR_ID,
    )


def _field(
    *,
    field_id: str = "field-1",
    field_key: str = "occupation",
    layer: int = 1,
    value: str = "工程師",
    content_mode: MessageContentMode = MessageContentMode.NORMAL,
) -> ProfileField:
    return ProfileField(
        field_id=field_id,
        field_key=field_key,
        layer=layer,
        value=value,
        confidence=0.9,
        evidence_refs=(
            EvidenceRef(
                turn_id="t",
                conversation_id="c",
                quote=value,
                extracted_at=datetime.now(timezone.utc),
            ),
        ),
        last_updated=datetime.now(timezone.utc),
        update_count=1,
        source="extraction",
        content_mode=content_mode,
        character_id=_CHAR_ID,
    )


def _build_service(
    *,
    profile_returns: OperatorProfile,
    plan: ConsolidationResult,
    pending: list[CandidateField],
    decay: list[ProfileField] | None = None,
) -> tuple[PersonaDreamService, MagicMock]:
    repo = AsyncMock()
    repo.list_pending = AsyncMock(return_value=pending)
    repo.list_confirmed_for_decay = AsyncMock(return_value=decay or [])
    repo.upsert_field = AsyncMock(side_effect=lambda _ch, _op, fld: fld)
    repo.mark_state = AsyncMock()
    repo.mark_field_state = AsyncMock()

    consolidator = AsyncMock()
    consolidator.consolidate = AsyncMock(return_value=plan)

    persona_service = MagicMock()
    persona_service.get_current = AsyncMock()
    persona_service.invalidate_cache = MagicMock()

    profile_service = MagicMock()
    profile_service.get_current = AsyncMock(return_value=profile_returns)
    profile_service.update_default = AsyncMock()

    return (
        PersonaDreamService(
            consolidator=consolidator,
            repository=repo,
            persona_service=persona_service,
            settings=PersonaSettings(),
            operator_profile_service=profile_service,
        ),
        profile_service,
    )


@pytest.mark.asyncio
async def test_promote_layer1_name_does_not_sync_when_profile_unset():
    cand = _candidate()
    plan = ConsolidationResult(
        promotions=[
            PromoteAction(
                candidate_id="cand-1",
                field_key="name",
                layer=1,
                value="丹尼",
                new_confidence=0.9,
            ),
        ],
    )
    svc, profile = _build_service(
        profile_returns=OperatorProfile.default(),
        plan=plan,
        pending=[cand],
    )
    await svc.run_consolidation(_CHAR_ID, _OP_ID)
    profile.update_default.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_still_skips_sync_when_profile_has_manual_name():
    """Operator has saved their own name — dream job must NEVER
    overwrite a manually-chosen ``display_name``."""
    cand = _candidate()
    plan = ConsolidationResult(
        promotions=[
            PromoteAction(
                candidate_id="cand-1",
                field_key="name",
                layer=1,
                value="阿丹",
                new_confidence=0.95,
            ),
        ],
    )
    svc, profile = _build_service(
        profile_returns=OperatorProfile(id="default", display_name="丹尼"),
        plan=plan,
        pending=[cand],
    )
    await svc.run_consolidation(_CHAR_ID, _OP_ID)
    profile.update_default.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_other_layer1_field_does_not_sync():
    """Only ``name`` syncs to display_name. ``occupation`` etc. stay
    inside the persona table."""
    cand = _candidate(field_key="occupation", value="工程師")
    plan = ConsolidationResult(
        promotions=[
            PromoteAction(
                candidate_id="cand-1",
                field_key="occupation",
                layer=1,
                value="工程師",
                new_confidence=0.9,
            ),
        ],
    )
    svc, profile = _build_service(
        profile_returns=OperatorProfile.default(),
        plan=plan,
        pending=[cand],
    )
    await svc.run_consolidation(_CHAR_ID, _OP_ID)
    profile.update_default.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_low_confidence_name_does_not_sync():
    """A 0.7 promote (single observation) is too thin — we need ≥0.85
    before mirroring into the canonical profile row."""
    cand = _candidate()
    plan = ConsolidationResult(
        promotions=[
            PromoteAction(
                candidate_id="cand-1",
                field_key="name",
                layer=1,
                value="丹尼",
                new_confidence=0.7,
            ),
        ],
    )
    svc, profile = _build_service(
        profile_returns=OperatorProfile.default(),
        plan=plan,
        pending=[cand],
    )
    await svc.run_consolidation(_CHAR_ID, _OP_ID)
    profile.update_default.assert_not_awaited()


@pytest.mark.asyncio
async def test_dream_consolidation_filters_nsfw_candidates_and_decay_fields():
    safe = _candidate(candidate_id="safe", value="工程師")
    sensitive = _candidate(
        candidate_id="nsfw",
        value="NSFW 候選",
        content_mode=MessageContentMode.NSFW,
    )
    sensitive_decay = _field(
        field_id="field-nsfw",
        value="NSFW 偏好",
        content_mode=MessageContentMode.NSFW,
    )
    svc, _profile = _build_service(
        profile_returns=OperatorProfile.default(),
        plan=ConsolidationResult(),
        pending=[safe, sensitive],
        decay=[sensitive_decay],
    )

    await svc.run_consolidation(_CHAR_ID, _OP_ID)

    kwargs = svc._consolidator.consolidate.await_args.kwargs  # noqa: SLF001
    assert kwargs["pending"] == [safe]
    assert kwargs["decay_candidates"] == []
