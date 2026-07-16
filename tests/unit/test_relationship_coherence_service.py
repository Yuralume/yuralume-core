"""Unit tests for :class:`RelationshipCoherenceService`.

Dream-time self-healing of address/identity contamination. The service
collects authoritative facts (seed, character name, operator profile,
rename-log) + suspect stores (persona name/nickname, observed salutation,
recent memory participants), hands them to a high-reasoning detector, and
applies the structured repair plan under strict invariants:

- persona name/nickname reversals go through the persona service's
  supersede path (never writes back the global profile);
- a contaminated salutation is cleared (aligned to seed);
- a contaminated memory has its salience lowered and its operator
  participant display name reconciled — content is never rewritten and
  the row is never deleted;
- a ``confirmed_by_user`` seed is never mutated;
- coherent data yields a no-op;
- a detector exception never blocks the dream pass.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.relationship_coherence_service import (
    RelationshipCoherenceService,
)
from kokoro_link.contracts.relationship_coherence import (
    CoherenceFacts,
    CoherenceRepairPlan,
    CoherenceSuspects,
    CoherenceTranscriptTurn,
    MemoryRepair,
    PersonaFieldRepair,
    RelationshipCoherenceDetectorPort,
    SalutationRepair,
)
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_address_preference import (
    OperatorAddressPreference,
)
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.profile_field import (
    EvidenceRef,
    ProfileField,
)


_CHAR_ID = "char-A"
_OP_ID = "op-1"


# ---- test doubles -----------------------------------------------------------


class _StubDetector(RelationshipCoherenceDetectorPort):
    def __init__(self, plan: CoherenceRepairPlan | Exception) -> None:
        self._plan = plan
        self.calls = 0
        self.last_facts: CoherenceFacts | None = None
        self.last_suspects: CoherenceSuspects | None = None

    async def detect(self, *, facts, suspects):
        self.calls += 1
        self.last_facts = facts
        self.last_suspects = suspects
        if isinstance(self._plan, Exception):
            raise self._plan
        return self._plan


class _FakeSeedRepo:
    def __init__(self, seed: CharacterOperatorRelationshipSeed | None) -> None:
        self._seed = seed

    async def get(self, character_id, operator_id):
        return self._seed


class _FakeChangeLog:
    async def latest(self, *, character_id, operator_id, direction):
        return None


class _FakeCharacterRepo:
    def __init__(self, character: Character | None) -> None:
        self._character = character

    async def get(self, character_id):
        return self._character


class _FakeProfileService:
    def __init__(self, profile: OperatorProfile) -> None:
        self._profile = profile

    async def get_for_user(self, user_id):
        return self._profile


class _FakePreferenceRepo:
    def __init__(self, pref: OperatorAddressPreference | None) -> None:
        self.pref = pref
        self.upserts: list[OperatorAddressPreference] = []

    async def get(self, *, character_id, operator_id):
        return self.pref

    async def upsert(self, pref):
        self.pref = pref
        self.upserts.append(pref)


class _RecordingPersonaService:
    """Captures supersede/reject calls and serves a persona snapshot."""

    def __init__(self, persona: OperatorPersona) -> None:
        self._persona = persona
        self.transitions: list[tuple[str, str]] = []  # (field_id, state)
        self.invalidations = 0

    async def get_current(self, character_id, operator_id):
        return self._persona

    async def transition_field_state_for_operator(
        self, field_id, state, operator_id,
    ):
        self.transitions.append((field_id, state))
        return True

    def invalidate_cache(self, character_id=None, operator_id=None):
        self.invalidations += 1


class _FakeConversationRepo:
    def __init__(self, messages: list[Message] | None = None) -> None:
        self._messages = messages or []

    async def recent_messages_for_character(
        self, character_id, *, limit, exclude_tool_only=False,
    ):
        return self._messages[-limit:]


def _msg(role: MessageRole, content: str) -> Message:
    return Message(role=role, content=content)


class _FakeMemoryRepo:
    def __init__(self, memories: list[MemoryItem]) -> None:
        self._memories = {m.id: m for m in memories}
        self.update_calls: list[dict] = []

    async def list_all_for_character(
        self, character_id, *, kinds=None, world_scope="all",
    ):
        return list(self._memories.values())

    async def get(self, item_id):
        return self._memories.get(item_id)

    async def update_fields(
        self, item_id, *, content=None, salience=None, tags=None,
        participants=None,
    ):
        self.update_calls.append(
            {
                "item_id": item_id,
                "content": content,
                "salience": salience,
                "tags": tags,
                "participants": participants,
            },
        )
        item = self._memories.get(item_id)
        if item is None:
            return None
        from dataclasses import replace

        updates: dict = {}
        if salience is not None:
            updates["salience"] = salience
        if participants is not None:
            updates["participants"] = tuple(participants)
        updated = replace(item, **updates)
        self._memories[item_id] = updated
        return updated


# ---- fixtures / builders ----------------------------------------------------


def _seed(
    *,
    user_address_name: str = "小明",
    character_address_name: str = "哥哥",
    confirmed_by_user: bool = True,
) -> CharacterOperatorRelationshipSeed:
    return CharacterOperatorRelationshipSeed(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        user_address_name=user_address_name,
        character_address_name=character_address_name,
        confirmed_by_user=confirmed_by_user,
    )


class _StubCharacter:
    """Duck-typed character — the service only reads ``.name``."""

    def __init__(self, name: str) -> None:
        self.id = _CHAR_ID
        self.name = name
        self.user_id = _OP_ID


def _character(name: str = "夜斗") -> _StubCharacter:
    return _StubCharacter(name)


def _profile(display_name: str = "小明", aliases=()) -> OperatorProfile:
    return OperatorProfile(
        id=_OP_ID, display_name=display_name, aliases=tuple(aliases),
    )


def _name_field(
    *, field_id: str, value: str, field_key: str = "name",
    source: str = "extraction", confidence: float = 0.85,
) -> ProfileField:
    return ProfileField(
        field_id=field_id,
        field_key=field_key,
        layer=1,
        value=value,
        confidence=confidence,
        evidence_refs=(
            EvidenceRef(
                turn_id="t", conversation_id="c", quote=value,
                extracted_at=datetime.now(timezone.utc),
            ),
        ),
        last_updated=datetime.now(timezone.utc),
        update_count=1,
        source=source,
        content_mode=MessageContentMode.NORMAL,
        character_id=_CHAR_ID,
    )


def _persona(layer1: dict[str, ProfileField]) -> OperatorPersona:
    return OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer1_identity=layer1,
    )


def _memory(
    *, memory_id: str, content: str, salience: float,
    operator_display_name: str,
) -> MemoryItem:
    return MemoryItem(
        id=memory_id,
        character_id=_CHAR_ID,
        conversation_id="conv-1",
        kind=MemoryKind.EPISODIC,
        content=content,
        salience=salience,
        participants=(
            ParticipantRef(
                actor_kind="operator",
                actor_id=_OP_ID,
                display_name=operator_display_name,
            ),
        ),
    )


def _build_service(
    *,
    detector,
    persona_service,
    seed,
    character=None,
    profile=None,
    preference_repo=None,
    memory_repo=None,
    conversation_repo=None,
    max_repairs=8,
) -> RelationshipCoherenceService:
    return RelationshipCoherenceService(
        detector=detector,
        persona_service=persona_service,
        seed_repository=_FakeSeedRepo(seed),
        change_log_repository=_FakeChangeLog(),
        character_repository=_FakeCharacterRepo(character or _character()),
        operator_profile_service=_FakeProfileService(profile or _profile()),
        address_preference_repository=(
            preference_repo or _FakePreferenceRepo(None)
        ),
        memory_repository=memory_repo or _FakeMemoryRepo([]),
        conversation_repository=conversation_repo or _FakeConversationRepo(),
        max_repairs_per_run=max_repairs,
    )


# ---- tests ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_name_reversal_is_superseded():
    """A persona ``name`` whose value equals the character-address name
    (direction B) or character name must be retired via the persona
    service — never rewrite the global profile."""
    contaminated = _name_field(field_id="f-bad", value="哥哥")
    persona_service = _RecordingPersonaService(
        _persona({"name": contaminated}),
    )
    detector = _StubDetector(
        CoherenceRepairPlan(
            persona_field_repairs=(
                PersonaFieldRepair(
                    field_id="f-bad",
                    contradicts="seed_character_address_name",
                    reason="哥哥 is how the player addresses the character",
                ),
            ),
        ),
    )
    svc = _build_service(
        detector=detector, persona_service=persona_service, seed=_seed(),
    )

    await svc.heal_pair(_CHAR_ID, _OP_ID)

    assert ("f-bad", "superseded") in persona_service.transitions
    assert persona_service.invalidations >= 1


@pytest.mark.asyncio
async def test_salutation_collision_is_cleared():
    """An observed salutation colliding with the user-address name
    (direction A) is a direction inversion — clear it."""
    pref = OperatorAddressPreference(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        salutation="小明", formality_level="low",
    )
    pref_repo = _FakePreferenceRepo(pref)
    persona_service = _RecordingPersonaService(_persona({}))
    detector = _StubDetector(
        CoherenceRepairPlan(
            salutation_repair=SalutationRepair(
                contradicts="seed_user_address_name",
                reason="salutation matches how the character addresses player",
            ),
        ),
    )
    svc = _build_service(
        detector=detector, persona_service=persona_service, seed=_seed(),
        preference_repo=pref_repo,
    )

    await svc.heal_pair(_CHAR_ID, _OP_ID)

    assert pref_repo.pref is not None
    assert pref_repo.pref.salutation == ""
    # non-salutation bands preserved
    assert pref_repo.pref.formality_level == "low"


@pytest.mark.asyncio
async def test_contaminated_memory_lowered_and_reconciled_not_deleted():
    """A memory whose operator participant was tagged with a direction-B
    term gets salience lowered + participant display name reconciled;
    content is untouched and the row is never deleted."""
    mem = _memory(
        memory_id="m-1",
        content="哥哥今天帶我去吃拉麵",
        salience=0.8,
        operator_display_name="哥哥",
    )
    memory_repo = _FakeMemoryRepo([mem])
    persona_service = _RecordingPersonaService(_persona({}))
    detector = _StubDetector(
        CoherenceRepairPlan(
            memory_repairs=(
                MemoryRepair(
                    memory_id="m-1",
                    lower_salience_to=0.2,
                    reconcile_participant_to="小明",
                    reason="operator tagged with direction-B term",
                ),
            ),
        ),
    )
    svc = _build_service(
        detector=detector, persona_service=persona_service, seed=_seed(),
        memory_repo=memory_repo,
    )

    await svc.heal_pair(_CHAR_ID, _OP_ID)

    assert len(memory_repo.update_calls) == 1
    call = memory_repo.update_calls[0]
    assert call["item_id"] == "m-1"
    assert call["salience"] == 0.2
    # content never rewritten
    assert call["content"] is None
    # participants reconciled to corrected operator display name
    assert call["participants"] is not None
    names = [p.display_name for p in call["participants"]]
    assert "小明" in names
    assert "哥哥" not in names
    # the stored memory content is unchanged
    stored = await memory_repo.get("m-1")
    assert stored.content == "哥哥今天帶我去吃拉麵"


@pytest.mark.asyncio
async def test_coherent_data_is_a_noop():
    """When the detector returns an empty plan, nothing is mutated."""
    persona_service = _RecordingPersonaService(
        _persona({"name": _name_field(field_id="f-ok", value="小明")}),
    )
    pref_repo = _FakePreferenceRepo(
        OperatorAddressPreference(
            character_id=_CHAR_ID, operator_id=_OP_ID, salutation="哥哥",
        ),
    )
    memory_repo = _FakeMemoryRepo(
        [_memory(
            memory_id="m-1", content="今天很開心", salience=0.7,
            operator_display_name="小明",
        )],
    )
    detector = _StubDetector(CoherenceRepairPlan())  # empty = coherent
    svc = _build_service(
        detector=detector, persona_service=persona_service, seed=_seed(),
        preference_repo=pref_repo, memory_repo=memory_repo,
    )

    await svc.heal_pair(_CHAR_ID, _OP_ID)

    assert persona_service.transitions == []
    assert pref_repo.upserts == []
    assert memory_repo.update_calls == []


@pytest.mark.asyncio
async def test_confirmed_seed_is_never_mutated_and_still_heals_suspects():
    """A ``confirmed_by_user`` seed is truth; the service never writes it
    back, but still repairs the contaminated suspect stores."""
    seed = _seed(confirmed_by_user=True)
    seed_repo = _FakeSeedRepo(seed)
    persona_service = _RecordingPersonaService(
        _persona({"name": _name_field(field_id="f-bad", value="哥哥")}),
    )
    detector = _StubDetector(
        CoherenceRepairPlan(
            persona_field_repairs=(
                PersonaFieldRepair(
                    field_id="f-bad",
                    contradicts="seed_character_address_name",
                    reason="reversal",
                ),
            ),
        ),
    )
    svc = RelationshipCoherenceService(
        detector=detector,
        persona_service=persona_service,
        seed_repository=seed_repo,
        change_log_repository=_FakeChangeLog(),
        character_repository=_FakeCharacterRepo(_character()),
        operator_profile_service=_FakeProfileService(_profile()),
        address_preference_repository=_FakePreferenceRepo(None),
        memory_repository=_FakeMemoryRepo([]),
    )
    # spy: seed repo must not receive a save
    seed_repo.save_called = False  # type: ignore[attr-defined]

    async def _no_save(_seed):  # pragma: no cover - fails test if hit
        seed_repo.save_called = True  # type: ignore[attr-defined]

    seed_repo.save = _no_save  # type: ignore[attr-defined]

    await svc.heal_pair(_CHAR_ID, _OP_ID)

    assert getattr(seed_repo, "save_called", False) is False
    assert ("f-bad", "superseded") in persona_service.transitions
    # the facts passed to the detector marked the seed confirmed
    assert detector.last_facts is not None
    assert detector.last_facts.seed_confirmed_by_user is True


@pytest.mark.asyncio
async def test_detector_exception_is_swallowed():
    """A detector that raises must not propagate — the dream pass must
    still complete."""
    persona_service = _RecordingPersonaService(_persona({}))
    detector = _StubDetector(RuntimeError("model exploded"))
    svc = _build_service(
        detector=detector, persona_service=persona_service, seed=_seed(),
    )

    # must not raise
    await svc.heal_pair(_CHAR_ID, _OP_ID)

    assert persona_service.transitions == []


@pytest.mark.asyncio
async def test_repairs_are_capped_per_run():
    """The per-run repair cap bounds how many persona rows one pass
    retires so a bad model can't wipe the persona in one tick."""
    fields = {
        "name": _name_field(field_id="f1", value="哥哥"),
        "nickname": _name_field(
            field_id="f2", value="夜斗", field_key="nickname",
        ),
    }
    persona_service = _RecordingPersonaService(_persona(fields))
    detector = _StubDetector(
        CoherenceRepairPlan(
            persona_field_repairs=(
                PersonaFieldRepair(
                    field_id="f1", contradicts="seed_character_address_name",
                    reason="r",
                ),
                PersonaFieldRepair(
                    field_id="f2", contradicts="character_name", reason="r",
                ),
            ),
        ),
    )
    svc = _build_service(
        detector=detector, persona_service=persona_service, seed=_seed(),
        max_repairs=1,
    )

    await svc.heal_pair(_CHAR_ID, _OP_ID)

    assert len(persona_service.transitions) == 1


@pytest.mark.asyncio
async def test_facts_and_suspects_are_collected_for_detector():
    """The service must feed the detector authoritative facts + suspects
    drawn from the real stores (structural collection, no keyword rules)."""
    contaminated = _name_field(field_id="f-bad", value="哥哥")
    persona_service = _RecordingPersonaService(
        _persona({"name": contaminated}),
    )
    pref_repo = _FakePreferenceRepo(
        OperatorAddressPreference(
            character_id=_CHAR_ID, operator_id=_OP_ID, salutation="小明",
        ),
    )
    memory_repo = _FakeMemoryRepo(
        [_memory(
            memory_id="m-1", content="哥哥帶我出去", salience=0.8,
            operator_display_name="哥哥",
        )],
    )
    detector = _StubDetector(CoherenceRepairPlan())
    svc = _build_service(
        detector=detector, persona_service=persona_service,
        seed=_seed(user_address_name="小明", character_address_name="哥哥"),
        character=_character("夜斗"), profile=_profile("小明"),
        preference_repo=pref_repo, memory_repo=memory_repo,
    )

    await svc.heal_pair(_CHAR_ID, _OP_ID)

    assert detector.calls == 1
    facts = detector.last_facts
    suspects = detector.last_suspects
    assert facts.seed_user_address_name == "小明"
    assert facts.seed_character_address_name == "哥哥"
    assert facts.character_name == "夜斗"
    assert facts.operator_display_name == "小明"
    # suspects reflect the real stores
    assert any(f.value == "哥哥" for f in suspects.persona_fields)
    assert suspects.observed_salutation == "小明"
    assert any(m.memory_id == "m-1" for m in suspects.memories)


@pytest.mark.asyncio
async def test_recent_transcript_is_passed_as_first_hand_evidence():
    """The service must feed the detector the recent raw transcript
    (windowed user+assistant turns) so it can adjudicate which derived
    value is dirty — not just the derived (memory) layer."""
    convo = _FakeConversationRepo(
        [
            _msg(MessageRole.USER, "哥哥你今天有空嗎"),
            _msg(MessageRole.ASSISTANT, "有啊小明，怎麼了"),
            _msg(MessageRole.USER, "叫我小明就好"),
        ],
    )
    persona_service = _RecordingPersonaService(
        _persona({"name": _name_field(field_id="f-bad", value="哥哥")}),
    )
    detector = _StubDetector(CoherenceRepairPlan())
    svc = _build_service(
        detector=detector, persona_service=persona_service,
        seed=_seed(user_address_name="小明", character_address_name="哥哥"),
        conversation_repo=convo,
    )

    await svc.heal_pair(_CHAR_ID, _OP_ID)

    facts = detector.last_facts
    assert facts is not None
    assert len(facts.recent_transcript) == 3
    roles = [t.role for t in facts.recent_transcript]
    assert "user" in roles
    assert "assistant" in roles
    contents = [t.content for t in facts.recent_transcript]
    assert "叫我小明就好" in contents


@pytest.mark.asyncio
async def test_transcript_evidence_heals_derived_contamination():
    """Transcript shows the player calling the character 哥哥 while asking
    to be called 小明; the detector (given that evidence) rules persona
    name=哥哥 is contamination → heal the derived value, anchored to seed."""
    convo = _FakeConversationRepo(
        [
            _msg(MessageRole.USER, "哥哥，我是小明"),
            _msg(MessageRole.ASSISTANT, "好的小明"),
        ],
    )
    persona_service = _RecordingPersonaService(
        _persona({"name": _name_field(field_id="f-bad", value="哥哥")}),
    )
    detector = _StubDetector(
        CoherenceRepairPlan(
            persona_field_repairs=(
                PersonaFieldRepair(
                    field_id="f-bad",
                    contradicts="seed_user_address_name",
                    reason="transcript: player is 小明, 哥哥 is how they call the character",
                ),
            ),
        ),
    )
    svc = _build_service(
        detector=detector, persona_service=persona_service,
        seed=_seed(user_address_name="小明", character_address_name="哥哥"),
        conversation_repo=convo,
    )

    await svc.heal_pair(_CHAR_ID, _OP_ID)

    assert ("f-bad", "superseded") in persona_service.transitions


@pytest.mark.asyncio
async def test_legitimate_recent_re_address_is_not_false_positive_healed():
    """When the transcript shows a legitimate recent re-address (player
    genuinely asked to be called a new name) the detector returns an empty
    plan; the service must not invent a repair and must not clear it."""
    convo = _FakeConversationRepo(
        [
            _msg(MessageRole.USER, "以後叫我阿明吧"),
            _msg(MessageRole.ASSISTANT, "好，阿明"),
        ],
    )
    # persona already reflects the legit re-address; nothing is dirty.
    persona_service = _RecordingPersonaService(
        _persona({"name": _name_field(field_id="f-ok", value="阿明")}),
    )
    pref_repo = _FakePreferenceRepo(
        OperatorAddressPreference(
            character_id=_CHAR_ID, operator_id=_OP_ID, salutation="哥哥",
        ),
    )
    detector = _StubDetector(CoherenceRepairPlan())  # detector: coherent
    svc = _build_service(
        detector=detector, persona_service=persona_service,
        seed=_seed(user_address_name="阿明", character_address_name="哥哥"),
        preference_repo=pref_repo, conversation_repo=convo,
    )

    await svc.heal_pair(_CHAR_ID, _OP_ID)

    # no false-positive healing
    assert persona_service.transitions == []
    assert pref_repo.upserts == []


@pytest.mark.asyncio
async def test_empty_transcript_window_falls_back_to_seed_authority():
    """With no relevant recent conversation, the service still functions:
    it relies on seed / rename-log as authority and does not become inert
    just because the transcript window is empty."""
    convo = _FakeConversationRepo([])  # empty window
    persona_service = _RecordingPersonaService(
        _persona({"name": _name_field(field_id="f-bad", value="哥哥")}),
    )
    detector = _StubDetector(
        CoherenceRepairPlan(
            persona_field_repairs=(
                PersonaFieldRepair(
                    field_id="f-bad",
                    contradicts="seed_character_address_name",
                    reason="matches seed direction-B truth",
                ),
            ),
        ),
    )
    svc = _build_service(
        detector=detector, persona_service=persona_service,
        seed=_seed(user_address_name="小明", character_address_name="哥哥"),
        conversation_repo=convo,
    )

    await svc.heal_pair(_CHAR_ID, _OP_ID)

    # detector still called with empty transcript, seed authority intact
    assert detector.calls == 1
    assert detector.last_facts.recent_transcript == ()
    assert detector.last_facts.seed_character_address_name == "哥哥"
    assert ("f-bad", "superseded") in persona_service.transitions
