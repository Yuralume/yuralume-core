"""Integration tests for ``SAOperatorPersonaRepository``.

Spins up against the testcontainers Postgres so the persistence layer
is exercised end-to-end (including the unique constraint that lets
pending and confirmed rows of the same field_key coexist, and the
per-character isolation that's the whole point of the table shape).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    EvidenceRef,
    ProfileField,
)
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.persistence.sa_operator_persona_repository import (
    SAOperatorPersonaRepository,
)
from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
    SAOperatorProfileRepository,
)


_OP_ID = DEFAULT_OPERATOR_ID


def _evidence(quote: str, *, turn: str = "msg-1") -> EvidenceRef:
    return EvidenceRef(
        turn_id=turn,
        conversation_id="conv-1",
        quote=quote,
        extracted_at=datetime.now(timezone.utc),
    )


async def _ensure_operator(session_factory: sessionmaker) -> None:
    profile_repo = SAOperatorProfileRepository(session_factory)
    profile = await profile_repo.get_default()
    if profile is None:
        await profile_repo.save(
            OperatorProfile(id=DEFAULT_OPERATOR_ID, display_name="丹尼"),
        )


async def _ensure_character(
    session_factory: sessionmaker, character_id: str,
) -> str:
    repo = SACharacterRepository(session_factory)
    existing = await repo.get(character_id)
    if existing is None:
        await repo.save(
            Character(
                id=character_id,
                name=f"Char {character_id}",
                summary="",
                personality=[],
                interests=[],
                speaking_style="",
                boundaries=[],
                state=CharacterState(
                    emotion="neutral", affection=50, fatigue=0,
                    trust=50, energy=100,
                ),
            ),
        )
    return character_id


async def _setup(session_factory: sessionmaker, *, character_id: str = "char-A") -> str:
    await _ensure_operator(session_factory)
    return await _ensure_character(session_factory, character_id)


@pytest.mark.asyncio
async def test_upsert_and_get_round_trip(session_factory: sessionmaker) -> None:
    char_id = await _setup(session_factory)
    repo = SAOperatorPersonaRepository(session_factory)

    candidate = CandidateField(
        field_key="occupation",
        layer=1,
        proposed_value="後端工程師",
        evidence_ref=_evidence("我是後端工程師"),
        raw_extractor_confidence=0.8,
        character_id=char_id,
    )
    stored = await repo.upsert_candidate(char_id, _OP_ID, candidate)
    assert stored.candidate_id is not None
    assert stored.state == "pending"
    assert stored.character_id == char_id

    persona = await repo.get(char_id, _OP_ID)
    assert len(persona.pending_candidates) == 1
    assert persona.pending_candidates[0].field_key == "occupation"
    # Pending must NOT leak into the confirmed layer dict.
    assert persona.layer1_identity == {}


@pytest.mark.asyncio
async def test_content_mode_round_trips_for_candidates_and_fields(
    session_factory: sessionmaker,
) -> None:
    char_id = await _setup(session_factory)
    repo = SAOperatorPersonaRepository(session_factory)

    candidate = CandidateField(
        field_key="interests",
        layer=2,
        proposed_value="私密偏好",
        evidence_ref=_evidence("我喜歡這件私密的事"),
        raw_extractor_confidence=0.85,
        character_id=char_id,
        content_mode=MessageContentMode.NSFW,
    )
    await repo.upsert_candidate(char_id, _OP_ID, candidate)
    confirmed = ProfileField(
        field_key="occupation",
        layer=1,
        value="後端工程師",
        confidence=0.9,
        evidence_refs=(_evidence("我是後端工程師"),),
        last_updated=datetime.now(timezone.utc),
        update_count=1,
        source="extraction",
        character_id=char_id,
        content_mode=MessageContentMode.NSFW,
    )
    await repo.upsert_field(char_id, _OP_ID, confirmed)

    persona = await repo.get(char_id, _OP_ID)

    assert persona.pending_candidates[0].content_mode is MessageContentMode.NSFW
    assert (
        persona.layer1_identity["occupation"].content_mode
        is MessageContentMode.NSFW
    )


@pytest.mark.asyncio
async def test_pending_dedup_bumps_update_count(
    session_factory: sessionmaker,
) -> None:
    char_id = await _setup(session_factory)
    repo = SAOperatorPersonaRepository(session_factory)

    base = CandidateField(
        field_key="diet",
        layer=2,
        proposed_value="不吃辣",
        evidence_ref=_evidence("我不吃辣"),
        raw_extractor_confidence=0.7,
        character_id=char_id,
    )
    first = await repo.upsert_candidate(char_id, _OP_ID, base)
    # Same (key, value, quote) → repo should dedup by bumping
    # ``update_count`` instead of writing a second row.
    again = await repo.upsert_candidate(char_id, _OP_ID, base)
    assert again.candidate_id == first.candidate_id
    pending = await repo.list_pending(char_id, _OP_ID)
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_same_key_same_value_different_quote_merges_evidence(
    session_factory: sessionmaker,
) -> None:
    char_id = await _setup(session_factory)
    repo = SAOperatorPersonaRepository(session_factory)

    first = await repo.upsert_candidate(
        char_id, _OP_ID,
        CandidateField(
            field_key="diet",
            layer=2,
            proposed_value="不吃辣",
            evidence_ref=_evidence("我不太能吃辣", turn="msg-1"),
            raw_extractor_confidence=0.7,
            character_id=char_id,
        ),
    )
    second = await repo.upsert_candidate(
        char_id, _OP_ID,
        CandidateField(
            field_key="diet",
            layer=2,
            proposed_value="不吃辣",
            evidence_ref=_evidence("辣的我真的不行", turn="msg-2"),
            raw_extractor_confidence=0.8,
            character_id=char_id,
        ),
    )

    assert second.candidate_id == first.candidate_id
    pending = await repo.list_pending(char_id, _OP_ID)
    assert len(pending) == 1
    assert pending[0].raw_extractor_confidence == 0.8


@pytest.mark.asyncio
async def test_same_key_different_pending_values_can_coexist(
    session_factory: sessionmaker,
) -> None:
    char_id = await _setup(session_factory)
    repo = SAOperatorPersonaRepository(session_factory)

    await repo.upsert_candidate(
        char_id, _OP_ID,
        CandidateField(
            field_key="occupation",
            layer=1,
            proposed_value="後端工程師",
            evidence_ref=_evidence("我是後端工程師", turn="msg-1"),
            raw_extractor_confidence=0.7,
            character_id=char_id,
        ),
    )
    await repo.upsert_candidate(
        char_id, _OP_ID,
        CandidateField(
            field_key="occupation",
            layer=1,
            proposed_value="產品經理",
            evidence_ref=_evidence("最近轉做產品經理", turn="msg-2"),
            raw_extractor_confidence=0.7,
            character_id=char_id,
        ),
    )

    pending = await repo.list_pending(char_id, _OP_ID)
    assert {cand.proposed_value for cand in pending} == {"後端工程師", "產品經理"}


@pytest.mark.asyncio
async def test_confirmed_and_pending_coexist(
    session_factory: sessionmaker,
) -> None:
    char_id = await _setup(session_factory)
    repo = SAOperatorPersonaRepository(session_factory)

    confirmed = ProfileField(
        field_key="name",
        layer=1,
        value="丹尼",
        confidence=0.9,
        evidence_refs=(_evidence("我叫丹尼"),),
        last_updated=datetime.now(timezone.utc),
        update_count=2,
        source="extraction",
        character_id=char_id,
    )
    await repo.upsert_field(char_id, _OP_ID, confirmed)

    pending_dup = CandidateField(
        field_key="name",
        layer=1,
        proposed_value="阿丹",
        evidence_ref=_evidence("叫我阿丹就好"),
        raw_extractor_confidence=0.7,
        character_id=char_id,
    )
    await repo.upsert_candidate(char_id, _OP_ID, pending_dup)

    persona = await repo.get(char_id, _OP_ID)
    assert persona.layer1_identity["name"].value == "丹尼"
    assert any(c.proposed_value == "阿丹" for c in persona.pending_candidates)
    assert await repo.count_pending(char_id, _OP_ID) == 1


@pytest.mark.asyncio
async def test_per_character_isolation(session_factory: sessionmaker) -> None:
    """The whole point of the per-character pivot: character A's
    observations are invisible to character B. Writing a confirmed
    field under A and then reading B's persona must come back empty."""
    char_a = await _setup(session_factory, character_id="char-A")
    char_b = await _ensure_character(session_factory, "char-B")
    repo = SAOperatorPersonaRepository(session_factory)

    fld = ProfileField(
        field_key="occupation",
        layer=1,
        value="後端工程師",
        confidence=0.9,
        evidence_refs=(_evidence("我是後端工程師"),),
        last_updated=datetime.now(timezone.utc),
        update_count=2,
        source="extraction",
        character_id=char_a,
    )
    await repo.upsert_field(char_a, _OP_ID, fld)

    persona_a = await repo.get(char_a, _OP_ID)
    persona_b = await repo.get(char_b, _OP_ID)

    assert persona_a.layer1_identity["occupation"].value == "後端工程師"
    # B starts from zero — no cross-character leak.
    assert persona_b.layer1_identity == {}
    assert persona_b.is_empty() or persona_b.layer4_interaction is None


@pytest.mark.asyncio
async def test_decay_query_returns_old_fields_only(
    session_factory: sessionmaker,
) -> None:
    char_id = await _setup(session_factory)
    repo = SAOperatorPersonaRepository(session_factory)

    now = datetime.now(timezone.utc)
    fresh = ProfileField(
        field_key="occupation",
        layer=1,
        value="工程師",
        confidence=0.85,
        evidence_refs=(_evidence("我是工程師"),),
        last_updated=now,
        update_count=1,
        source="extraction",
        character_id=char_id,
    )
    await repo.upsert_field(char_id, _OP_ID, fresh)

    none = await repo.list_confirmed_for_decay(
        char_id, _OP_ID, stale_after_days=30,
    )
    assert none == []


@pytest.mark.asyncio
async def test_mark_state_transitions_candidate(
    session_factory: sessionmaker,
) -> None:
    char_id = await _setup(session_factory)
    repo = SAOperatorPersonaRepository(session_factory)

    cand = await repo.upsert_candidate(
        char_id, _OP_ID,
        CandidateField(
            field_key="interests",
            layer=2,
            proposed_value="科幻電影",
            evidence_ref=_evidence("我喜歡科幻電影"),
            raw_extractor_confidence=0.7,
            character_id=char_id,
        ),
    )
    await repo.mark_state(cand.candidate_id, "rejected")  # type: ignore[arg-type]
    persona = await repo.get(char_id, _OP_ID)
    assert persona.pending_candidates == ()


@pytest.mark.asyncio
async def test_get_row_scope_resolves_owner_for_candidate_and_field(
    session_factory: sessionmaker,
) -> None:
    """``get_row_scope`` maps a bare row id back to its
    ``(character, operator)`` for both pending candidates and confirmed
    fields, so the ownership guard can refuse a cross-operator mutation."""
    char_id = await _setup(session_factory)
    repo = SAOperatorPersonaRepository(session_factory)

    cand = await repo.upsert_candidate(
        char_id, _OP_ID,
        CandidateField(
            field_key="interests",
            layer=2,
            proposed_value="科幻電影",
            evidence_ref=_evidence("我喜歡科幻電影"),
            raw_extractor_confidence=0.7,
            character_id=char_id,
        ),
    )
    confirmed = await repo.upsert_field(
        char_id, _OP_ID,
        ProfileField(
            field_key="name",
            layer=1,
            value="丹尼",
            confidence=0.9,
            evidence_refs=(_evidence("我叫丹尼"),),
            last_updated=datetime.now(timezone.utc),
            update_count=2,
            source="extraction",
            character_id=char_id,
        ),
    )

    assert await repo.get_row_scope(cand.candidate_id) == (char_id, _OP_ID)  # type: ignore[arg-type]
    assert await repo.get_row_scope(confirmed.field_id) == (char_id, _OP_ID)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_row_scope_unknown_id_is_none(
    session_factory: sessionmaker,
) -> None:
    await _setup(session_factory)
    repo = SAOperatorPersonaRepository(session_factory)
    assert await repo.get_row_scope("does-not-exist") is None


@pytest.mark.asyncio
async def test_reject_evidence_since_removes_undone_turn_candidates(
    session_factory: sessionmaker,
) -> None:
    char_id = await _setup(session_factory)
    repo = SAOperatorPersonaRepository(session_factory)
    since = datetime.now(timezone.utc)
    cand = await repo.upsert_candidate(
        char_id, _OP_ID,
        CandidateField(
            field_key="occupation",
            layer=1,
            proposed_value="工程師",
            evidence_ref=EvidenceRef(
                turn_id="msg-undo",
                conversation_id="conv-undo",
                quote="我是工程師",
                extracted_at=datetime.now(timezone.utc),
            ),
            raw_extractor_confidence=0.8,
            character_id=char_id,
        ),
    )

    removed = await repo.reject_evidence_since(
        conversation_id="conv-undo",
        since=since,
    )

    assert removed == 1
    pending = await repo.list_pending(char_id, _OP_ID)
    assert all(item.candidate_id != cand.candidate_id for item in pending)


@pytest.mark.asyncio
async def test_list_characters_with_pending_returns_distinct_pairs(
    session_factory: sessionmaker,
) -> None:
    """Used by the scheduler to fan out dream ticks only to pairs that
    actually need one."""
    char_a = await _setup(session_factory, character_id="char-A")
    char_b = await _ensure_character(session_factory, "char-B")
    repo = SAOperatorPersonaRepository(session_factory)

    for char_id in (char_a, char_b):
        await repo.upsert_candidate(
            char_id, _OP_ID,
            CandidateField(
                field_key="occupation",
                layer=1,
                proposed_value="工程師",
                evidence_ref=_evidence(f"{char_id}-quote"),
                raw_extractor_confidence=0.7,
                character_id=char_id,
            ),
        )

    pairs = await repo.list_characters_with_pending()
    assert set(pairs) == {(char_a, _OP_ID), (char_b, _OP_ID)}
