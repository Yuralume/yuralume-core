"""Unit tests for ``RelationshipMilestoneService`` (HUMANIZATION_ROADMAP §3.5).

Pure observation service — given a current interaction-volume band and the most
recent stored milestone (looked up via ``MemoryRepositoryPort.query``),
the service either:

- skips (feature off, no interactions, no crossing, idempotent re-run)
- appends a fixed-high salience ``relationship_milestone`` memory row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kokoro_link.application.services.relationship_milestone_service import (
    RelationshipMilestoneService,
)
from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_persona import InteractionStrength
from kokoro_link.domain.value_objects.familiarity import Familiarity
from kokoro_link.domain.value_objects.memory_kind import MemoryKind


_CHAR_ID = "char-A"
_OP_ID = "default"
_NOW = datetime(2026, 5, 21, 3, 0, tzinfo=timezone.utc)


def _strength(band: Familiarity, *, total: int = 4) -> InteractionStrength:
    return InteractionStrength(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        first_message_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        total_user_messages=total,
        days_since_first_contact=20,
        messages_last_7_days=3,
        messages_last_30_days=10,
        longest_session_minutes=45,
        shared_arc_realized_count=0,
        shared_drama_count=0,
        familiarity_band=band,
        computed_at=_NOW,
    )


def _build_service(
    *,
    band: Familiarity = Familiarity.ACQUAINTANCE,
    existing_milestones: list[MemoryItem] | None = None,
    settings: HumanizationSettings | None = None,
    strength: InteractionStrength | None = None,
    operator_profile_service: Any | None = None,
) -> tuple[RelationshipMilestoneService, MagicMock, AsyncMock]:
    persona_service = MagicMock()
    persona_service.get_interaction_strength = AsyncMock(
        return_value=strength if strength is not None else _strength(band),
    )

    memory_repo = AsyncMock()
    memory_repo.query = AsyncMock(return_value=list(existing_milestones or []))

    async def _capture_add(item: MemoryItem) -> MemoryItem:
        return item

    memory_repo.add = AsyncMock(side_effect=_capture_add)

    service = RelationshipMilestoneService(
        persona_service=persona_service,
        memory_repository=memory_repo,
        settings=settings or HumanizationSettings(),
        operator_profile_service=operator_profile_service,
    )
    return service, persona_service, memory_repo


def _existing_milestone(band: str) -> MemoryItem:
    return MemoryItem.create(
        character_id=_CHAR_ID,
        kind=MemoryKind.RELATIONSHIP_MILESTONE,
        content=f"先前里程碑：band={band}",
        salience=1.0,
        tags=("relationship_milestone", f"band:{band}", f"operator:{_OP_ID}"),
        created_at=datetime(2026, 5, 18, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_first_crossing_emits_milestone():
    """No prior milestone and current band has left stranger — write one."""
    svc, _, repo = _build_service(band=Familiarity.ACQUAINTANCE)

    emitted = await svc.check_and_emit(_CHAR_ID, _OP_ID, now=_NOW)

    assert emitted is not None
    assert emitted.kind.value == MemoryKind.RELATIONSHIP_MILESTONE.value
    assert emitted.salience == 1.0
    assert "band:acquaintance" in emitted.tags
    assert f"operator:{_OP_ID}" in emitted.tags
    assert "互動熱度" in emitted.content
    assert "關係進入" not in emitted.content
    repo.add.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_crossing_skipped_when_still_stranger():
    """Brand-new pair sitting at stranger — no useless anchor row."""
    svc, _, repo = _build_service(band=Familiarity.STRANGER)

    emitted = await svc.check_and_emit(_CHAR_ID, _OP_ID, now=_NOW)

    assert emitted is None
    repo.add.assert_not_called()


@pytest.mark.asyncio
async def test_no_emission_when_band_unchanged():
    """Already at acquaintance, last milestone also acquaintance → no-op."""
    svc, _, repo = _build_service(
        band=Familiarity.ACQUAINTANCE,
        existing_milestones=[_existing_milestone("acquaintance")],
    )

    emitted = await svc.check_and_emit(_CHAR_ID, _OP_ID, now=_NOW)

    assert emitted is None
    repo.add.assert_not_called()


@pytest.mark.asyncio
async def test_band_upgrade_emits_new_milestone():
    """acquaintance → familiar writes a new milestone."""
    svc, _, repo = _build_service(
        band=Familiarity.FAMILIAR,
        existing_milestones=[_existing_milestone("acquaintance")],
    )

    emitted = await svc.check_and_emit(_CHAR_ID, _OP_ID, now=_NOW)

    assert emitted is not None
    assert "band:familiar" in emitted.tags
    # Content references both previous and current bands as interaction
    # heat, not relationship-stage truth.
    assert "互動漸多" in emitted.content
    assert "互動頻繁" in emitted.content
    assert "關係從" not in emitted.content
    assert "初識" not in emitted.content


@pytest.mark.asyncio
async def test_feature_flag_off_short_circuits():
    settings = HumanizationSettings(relationship_milestone_enabled=False)
    svc, persona, repo = _build_service(
        band=Familiarity.ACQUAINTANCE,
        settings=settings,
    )

    emitted = await svc.check_and_emit(_CHAR_ID, _OP_ID, now=_NOW)

    assert emitted is None
    # Feature-flag rejection happens before any I/O — neither persona
    # nor memory repo are touched, keeping the cost truly zero.
    persona.get_interaction_strength.assert_not_called()
    repo.query.assert_not_called()
    repo.add.assert_not_called()


@pytest.mark.asyncio
async def test_no_emission_when_user_has_not_messaged_yet():
    svc, _, repo = _build_service(
        strength=InteractionStrength.empty(_CHAR_ID, _OP_ID, now=_NOW),
    )

    emitted = await svc.check_and_emit(_CHAR_ID, _OP_ID, now=_NOW)

    assert emitted is None
    repo.add.assert_not_called()


@pytest.mark.asyncio
async def test_repo_failure_returns_none_without_raising():
    """A failed ``add`` must not propagate — dream pass already applied
    its consolidation plan; we just lose this milestone row."""
    svc, _, repo = _build_service(band=Familiarity.ACQUAINTANCE)
    repo.add = AsyncMock(side_effect=RuntimeError("boom"))

    emitted = await svc.check_and_emit(_CHAR_ID, _OP_ID, now=_NOW)

    assert emitted is None


def _has_han(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _has_kana(text: str) -> bool:
    return any("぀" <= ch <= "ヿ" for ch in text)


class _FakeOperatorProfile:
    def __init__(self, primary_language: str) -> None:
        self.primary_language = primary_language


class _FakeOperatorProfileService:
    def __init__(self, primary_language: str) -> None:
        self._primary_language = primary_language

    async def get_for_user(self, user_id: str) -> _FakeOperatorProfile:
        return _FakeOperatorProfile(self._primary_language)


@pytest.mark.asyncio
async def test_milestone_content_defaults_to_zh_tw_without_profile_service():
    svc, _, _repo = _build_service(band=Familiarity.ACQUAINTANCE)

    emitted = await svc.check_and_emit(_CHAR_ID, _OP_ID, now=_NOW)

    assert emitted is not None
    assert _has_han(emitted.content)


@pytest.mark.asyncio
async def test_milestone_content_localizes_to_english():
    svc, _, _repo = _build_service(
        band=Familiarity.ACQUAINTANCE,
        operator_profile_service=_FakeOperatorProfileService("en-US"),
    )

    emitted = await svc.check_and_emit(_CHAR_ID, _OP_ID, now=_NOW)

    assert emitted is not None
    assert not _has_han(emitted.content), emitted.content
    assert "interacting more" in emitted.content.lower()


@pytest.mark.asyncio
async def test_milestone_content_localizes_to_japanese():
    svc, _, _repo = _build_service(
        band=Familiarity.ACQUAINTANCE,
        operator_profile_service=_FakeOperatorProfileService("ja-JP"),
    )

    emitted = await svc.check_and_emit(_CHAR_ID, _OP_ID, now=_NOW)

    assert emitted is not None
    assert _has_kana(emitted.content), emitted.content


@pytest.mark.asyncio
async def test_band_upgrade_content_localizes_to_english_with_both_labels():
    svc, _, _repo = _build_service(
        band=Familiarity.FAMILIAR,
        existing_milestones=[_existing_milestone("acquaintance")],
        operator_profile_service=_FakeOperatorProfileService("en-US"),
    )

    emitted = await svc.check_and_emit(_CHAR_ID, _OP_ID, now=_NOW)

    assert emitted is not None
    assert not _has_han(emitted.content), emitted.content
    assert "interacting more" in emitted.content.lower()
    assert "interact often" in emitted.content.lower()
