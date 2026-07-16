"""Unit tests for the DispositionDrift stack (HUMANIZATION_ROADMAP §3.1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kokoro_link.application.services.disposition_drift_service import (
    DispositionDriftService,
)
from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.contracts.disposition_drift import (
    DispositionDriftInput,
    DispositionDriftProposal,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.disposition_drift_record import (
    DispositionDriftRecord,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.disposition import CharacterDisposition
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.disposition.llm_drift_judge import (
    LLMDispositionDriftJudge,
)
from kokoro_link.infrastructure.repositories.in_memory_disposition_drift import (
    InMemoryDispositionDriftHistoryRepository,
)


_CHAR = "char-A"
_NOW = datetime(2026, 5, 21, 4, 0, tzinfo=timezone.utc)


# ---- entity --------------------------------------------------------------


def test_record_rejects_identical_bands():
    with pytest.raises(ValueError, match="actual shift"):
        DispositionDriftRecord.new(
            character_id=_CHAR,
            dimension="candor",
            from_band="medium",
            to_band="medium",
            reason="x",
        )


def test_record_rejects_unknown_dimension():
    with pytest.raises(ValueError, match="dimension"):
        DispositionDriftRecord.new(
            character_id=_CHAR,
            dimension="bogus",
            from_band="medium",
            to_band="high",
            reason="x",
        )


# ---- repo ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_repo_latest_for_dimension():
    repo = InMemoryDispositionDriftHistoryRepository()
    earlier = DispositionDriftRecord.new(
        character_id=_CHAR,
        dimension="candor",
        from_band="medium",
        to_band="high",
        reason="r1",
        now=_NOW - timedelta(days=10),
    )
    later = DispositionDriftRecord.new(
        character_id=_CHAR,
        dimension="candor",
        from_band="high",
        to_band="medium",
        reason="r2",
        now=_NOW - timedelta(days=2),
    )
    other_dim = DispositionDriftRecord.new(
        character_id=_CHAR,
        dimension="sharing_drive",
        from_band="medium",
        to_band="high",
        reason="r3",
        now=_NOW - timedelta(days=1),
    )
    await repo.add(earlier)
    await repo.add(later)
    await repo.add(other_dim)

    latest = await repo.latest_for_dimension(_CHAR, "candor")
    assert latest is not None
    assert latest.reason == "r2"


# ---- service -------------------------------------------------------------


def _character(
    *,
    disposition: CharacterDisposition | None = None,
) -> Character:
    return Character.create(
        name="Mio",
        summary="工程師",
        personality=["內向"],
        interests=["咖啡"],
        speaking_style="安靜",
        boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
        disposition=disposition or CharacterDisposition.DEFAULT,
    )


def _memory(content: str = "重要事件", salience: float = 0.8) -> MemoryItem:
    return MemoryItem.create(
        character_id=_CHAR,
        kind=MemoryKind.EPISODIC,
        content=content,
        salience=salience,
    )


def _build_service(
    *,
    character: Character | None = None,
    memories: list[MemoryItem] | None = None,
    proposal: DispositionDriftProposal | None = None,
    settings: HumanizationSettings | None = None,
    history_seed: list[DispositionDriftRecord] | None = None,
) -> tuple[DispositionDriftService, InMemoryDispositionDriftHistoryRepository, MagicMock]:
    character_obj = character or _character()
    char_repo = MagicMock()
    char_repo.get = AsyncMock(return_value=character_obj)
    char_repo.save = AsyncMock()

    memory_repo = AsyncMock()
    memory_repo.list_all_for_character = AsyncMock(
        return_value=memories or [_memory(f"記憶 {i}") for i in range(6)],
    )

    history = InMemoryDispositionDriftHistoryRepository()
    if history_seed:
        for row in history_seed:
            history._rows.append(row)  # type: ignore[attr-defined]

    judge = MagicMock()
    judge.judge = AsyncMock(return_value=proposal)

    svc = DispositionDriftService(
        character_repository=char_repo,
        history_repository=history,
        memory_repository=memory_repo,
        emotion_event_repository=None,
        judge=judge,
        settings=settings or HumanizationSettings(),
    )
    return svc, history, char_repo


@pytest.mark.asyncio
async def test_service_applies_proposal_within_cooldown_window():
    proposal = DispositionDriftProposal(
        dimension="candor",
        direction="up",
        reason="多次直白表達",
        evidence_quote="記憶 3",
    )
    svc, history, char_repo = _build_service(proposal=proposal)
    record = await svc.run_for_character(_CHAR, now=_NOW)
    assert record is not None
    assert record.dimension == "candor"
    assert record.from_band == "medium"
    assert record.to_band == "high"
    char_repo.save.assert_awaited_once()
    saved = char_repo.save.await_args.args[0]
    assert saved.disposition.candor == "high"


@pytest.mark.asyncio
async def test_service_rejects_when_judge_returns_none():
    svc, history, char_repo = _build_service(proposal=None)
    record = await svc.run_for_character(_CHAR, now=_NOW)
    assert record is None
    char_repo.save.assert_not_called()


@pytest.mark.asyncio
async def test_service_blocked_by_cooldown():
    proposal = DispositionDriftProposal(
        dimension="candor", direction="up", reason="r", evidence_quote="",
    )
    recent = DispositionDriftRecord.new(
        character_id=_CHAR, dimension="candor",
        from_band="medium", to_band="high",
        reason="prior", now=_NOW - timedelta(days=5),
    )
    svc, _, char_repo = _build_service(
        proposal=proposal, history_seed=[recent],
    )
    record = await svc.run_for_character(_CHAR, now=_NOW)
    assert record is None
    char_repo.save.assert_not_called()


@pytest.mark.asyncio
async def test_service_respects_extreme_band_guard():
    """high + direction=up → no movement (already at extreme)."""
    character = _character(
        disposition=CharacterDisposition(candor="high"),
    )
    proposal = DispositionDriftProposal(
        dimension="candor", direction="up", reason="r", evidence_quote="",
    )
    svc, _, char_repo = _build_service(
        character=character, proposal=proposal,
    )
    record = await svc.run_for_character(_CHAR, now=_NOW)
    assert record is None
    char_repo.save.assert_not_called()


@pytest.mark.asyncio
async def test_service_feature_flag_off_short_circuits():
    proposal = DispositionDriftProposal(
        dimension="candor", direction="up", reason="r", evidence_quote="",
    )
    svc, _, char_repo = _build_service(
        proposal=proposal,
        settings=HumanizationSettings(disposition_drift_enabled=False),
    )
    record = await svc.run_for_character(_CHAR, now=_NOW)
    assert record is None
    char_repo.get.assert_not_called()


@pytest.mark.asyncio
async def test_service_too_few_memories_skipped():
    svc, _, char_repo = _build_service(
        memories=[_memory("only one")],
        proposal=DispositionDriftProposal(
            dimension="candor", direction="up", reason="r",
            evidence_quote="",
        ),
    )
    record = await svc.run_for_character(_CHAR, now=_NOW)
    assert record is None


# ---- LLM judge hallucination guard --------------------------------------


@pytest.mark.asyncio
async def test_llm_judge_rejects_hallucinated_quote():
    class _Model:
        async def generate(self, prompt: str) -> str:
            return (
                '{"dimension": "candor", "direction": "up", '
                '"reason": "對方主動分享", "evidence_quote": "完全沒講過這句"}'
            )

        async def generate_stream(self, prompt: str):  # pragma: no cover
            yield ""

    judge = LLMDispositionDriftJudge(model=_Model())
    payload = DispositionDriftInput(
        character_id=_CHAR,
        character_name="Mio",
        disposition=CharacterDisposition.DEFAULT,
        emotion_event_summary="",
        high_salience_memories=(_memory("使用者今天聊到工作壓力"),),
    )
    proposal = await judge.judge(payload)
    assert proposal is None


@pytest.mark.asyncio
async def test_llm_judge_returns_proposal_with_verbatim_quote():
    class _Model:
        async def generate(self, prompt: str) -> str:
            return (
                '{"dimension": "sharing_drive", "direction": "up", '
                '"reason": "對方主動分享多次脆弱面", '
                '"evidence_quote": "使用者今天聊到工作壓力"}'
            )

        async def generate_stream(self, prompt: str):  # pragma: no cover
            yield ""

    judge = LLMDispositionDriftJudge(model=_Model())
    payload = DispositionDriftInput(
        character_id=_CHAR,
        character_name="Mio",
        disposition=CharacterDisposition.DEFAULT,
        emotion_event_summary="",
        high_salience_memories=(_memory("使用者今天聊到工作壓力"),),
    )
    proposal = await judge.judge(payload)
    assert proposal is not None
    assert proposal.dimension == "sharing_drive"
    assert proposal.direction == "up"
    assert proposal.evidence_quote == "使用者今天聊到工作壓力"
