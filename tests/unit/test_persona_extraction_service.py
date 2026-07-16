from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.persona_extraction_service import (
    PersonaExtractionService,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    EvidenceRef,
)


def _evidence() -> EvidenceRef:
    return EvidenceRef(
        turn_id="msg-1",
        conversation_id="conv-1",
        quote="我喜歡爵士樂",
        extracted_at=datetime(2026, 6, 13, tzinfo=timezone.utc),
    )


def _candidate() -> CandidateField:
    return CandidateField(
        field_key="interests",
        layer=2,
        proposed_value="爵士樂",
        evidence_ref=_evidence(),
        raw_extractor_confidence=0.82,
        character_id="char-1",
    )


class _Extractor:
    def __init__(self, candidates: list[CandidateField]) -> None:
        self.candidates = candidates

    async def extract(self, **kwargs):
        return self.candidates


class _Repository:
    def __init__(self) -> None:
        self.candidates: list[CandidateField] = []

    async def upsert_candidate(self, character_id, operator_id, candidate):
        self.candidates.append(candidate)
        return candidate


class _PersonaService:
    def __init__(self) -> None:
        self.invalidated: list[tuple[str, str]] = []

    async def get_current(self, character_id, operator_id):
        return OperatorPersona.empty(character_id, operator_id)

    def invalidate_cache(self, character_id, operator_id) -> None:
        self.invalidated.append((character_id, operator_id))


@pytest.mark.asyncio
async def test_nsfw_turn_marks_persona_candidates_sensitive() -> None:
    repo = _Repository()
    service = PersonaExtractionService(
        extractor=_Extractor([_candidate()]),
        repository=repo,  # type: ignore[arg-type]
        persona_service=_PersonaService(),  # type: ignore[arg-type]
    )

    written = await service.run_after_turn(
        character_id="char-1",
        operator=OperatorProfile.default(),
        conversation_id="conv-1",
        user_message_id="msg-1",
        user_text="我喜歡爵士樂",
        assistant_text="我記住了",
        content_mode=MessageContentMode.NSFW,
    )

    assert written == 1
    assert repo.candidates[0].content_mode is MessageContentMode.NSFW


@pytest.mark.asyncio
async def test_normal_turn_keeps_persona_candidates_normal() -> None:
    repo = _Repository()
    service = PersonaExtractionService(
        extractor=_Extractor([replace(_candidate(), content_mode=MessageContentMode.NSFW)]),
        repository=repo,  # type: ignore[arg-type]
        persona_service=_PersonaService(),  # type: ignore[arg-type]
    )

    await service.run_after_turn(
        character_id="char-1",
        operator=OperatorProfile.default(),
        conversation_id="conv-1",
        user_message_id="msg-1",
        user_text="我喜歡爵士樂",
        assistant_text="我記住了",
        content_mode=MessageContentMode.NORMAL,
    )

    assert repo.candidates[0].content_mode is MessageContentMode.NORMAL
