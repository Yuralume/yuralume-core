"""TU5 — the rename and the closed scene have to go back with the turn.

Own file rather than another block in ``test_turn_undo.py`` for the same
reason TU3 and TU4 got theirs: the harness is differently wired. The
address half needs an operator profile, a relationship-seed store and a
rename log present on *both* ChatService (which writes the rename during
post-turn) and TurnUndoService (which reverses it); the scene half needs
a scene-session repository that enforces the one-open-scene invariant, so
the reopen path is exercised against the real conflict rather than a
permissive stub.

Two things this suite deliberately asserts *around* rather than *on*:

* **The seed, not the preference row.** What a turn actually moves is
  ``character_operator_relationship_seed`` plus one
  ``operator_address_change_log`` entry per direction —
  ``OperatorAddressPreference`` is written by the dream pass on its own
  multi-hour cooldown and is almost never part of a turn. A suite that
  only checked the preference row would be green against a step that
  reverses nothing a player can see.
* **Not being touched is the harder half.** Most turns move neither
  subsystem, and a rollback that "restores" a rename that never happened
  or drags a live scene's idle clock backwards does more damage than one
  that restores nothing. Every positive case below has a negative twin.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.goal_service import GoalService
from kokoro_link.application.services.operator_persona_service import (
    OperatorPersonaService,
)
from kokoro_link.application.services.operator_profile_service import (
    OperatorProfileService,
)
from kokoro_link.application.services.relationship_names_service import (
    RelationshipNamesService,
)
from kokoro_link.application.services.state_tracker import StateChangeTracker
from kokoro_link.application.services.turn_undo_service import TurnUndoService
from kokoro_link.contracts.post_turn import AddressChangeSignal, PostTurnResult
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_address_preference import (
    OperatorAddressPreference,
)
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_MANUAL,
    SCENE_CLOSE_RESOLVED,
    SCENE_CLOSE_TIMEOUT,
    SCENE_LAYER_SIDE_STORY,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.address_change_event import (
    DIRECTION_CHARACTER,
    DIRECTION_PLAYER,
    SOURCE_OBSERVED,
    SOURCE_PLAYER_EDIT,
    AddressChangeEvent,
)
from kokoro_link.domain.services.address_resolver import resolve_player_address
from kokoro_link.domain.value_objects.profile_field import (
    EvidenceRef,
    ProfileField,
)
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.persistence.models import Base
from kokoro_link.infrastructure.persistence.sa_operator_persona_repository import (
    SAOperatorPersonaRepository,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_address_change_log import (
    InMemoryAddressChangeLogRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_address_preferences import (
    InMemoryOperatorAddressPreferenceRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_goals import (
    InMemoryGoalRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_state_history import (
    InMemoryStateHistoryRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_scene_sessions import (
    InMemoryStorySceneSessionRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_journals import (
    InMemoryTurnJournalRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine


class _SeedStore:
    """Keyed in-memory ``CharacterOperatorRelationshipSeed`` store.

    There is no shipped in-memory adapter for this port (production
    self-host runs the SQL one), and the single-seed fake the
    relationship-names suite uses would hide a step that reached for the
    wrong pair. Keying on the pair is the whole point of the fake.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], CharacterOperatorRelationshipSeed] = {}

    async def get(
        self, character_id: str, operator_id: str,
    ) -> CharacterOperatorRelationshipSeed | None:
        return self.rows.get((character_id, operator_id))

    async def save(self, seed: CharacterOperatorRelationshipSeed) -> None:
        self.rows[(seed.character_id, seed.operator_id)] = seed

    async def delete_for_character(self, character_id: str) -> int:
        keys = [k for k in self.rows if k[0] == character_id]
        for key in keys:
            self.rows.pop(key)
        return len(keys)


class _RenamingPostTurnProcessor:
    """Emits whatever address signals the test hands it, nothing else.

    No memories and no state suggestion: a failure in this suite should
    only ever be about the address path.
    """

    def __init__(self) -> None:
        self.address_changes: list[AddressChangeSignal] = []

    async def process(
        self, *, character, conversation_id, user_message, assistant_message,
        recent_messages=None, active_schedule=None, active_arc=None,
        operator=None, now=None, **kwargs,
    ) -> PostTurnResult:
        return PostTurnResult(
            memories=[],
            address_changes=list(self.address_changes),
        )


class _Harness:
    """ChatService and TurnUndoService sharing one set of stores.

    ``persona_repository`` is optional and, when supplied, is the real
    ``SAOperatorPersonaRepository`` over an in-process SQLite database
    (see :func:`persona_repository`). Wiring it is not a detail: the
    rename's third write goes through
    ``RelationshipNamesService(persona_service=...)``, so a harness that
    leaves ``persona_service`` at ``None`` never triggers the reconcile
    and is green against a step that reverses nothing of it.
    """

    def __init__(self, persona_repository=None) -> None:
        self.characters = InMemoryCharacterRepository()
        self.conversations = InMemoryConversationRepository()
        self.memories = InMemoryMemoryRepository()
        self.state_history = InMemoryStateHistoryRepository()
        self.journals = InMemoryTurnJournalRepository()
        self.seeds = _SeedStore()
        self.change_log = InMemoryAddressChangeLogRepository()
        self.address_preferences = InMemoryOperatorAddressPreferenceRepository()
        self.scenes = InMemoryStorySceneSessionRepository()
        self.post_turn = _RenamingPostTurnProcessor()

        registry = InMemoryChatModelRegistry(default_provider_id="fake")
        registry.register(FakeChatModel(provider_id="fake"))

        self.persona_repository = persona_repository
        self.persona_service = (
            None if persona_repository is None
            else OperatorPersonaService(
                repository=persona_repository,
                strength_calculator=None,  # type: ignore[arg-type]
                settings=SimpleNamespace(),  # type: ignore[arg-type]
            )
        )
        self.profiles = InMemoryOperatorProfileRepository()
        self.names = RelationshipNamesService(
            seed_repository=self.seeds,
            change_log_repository=self.change_log,
            persona_service=self.persona_service,
        )
        self.chat = ChatService(
            character_repository=self.characters,
            conversation_repository=self.conversations,
            memory_repository=self.memories,
            post_turn_processor=self.post_turn,
            prompt_context_builder=DefaultPromptContextBuilder(),
            model_registry=registry,
            state_engine=SimpleStateEngine(),
            goal_service=GoalService(InMemoryGoalRepository()),
            state_tracker=StateChangeTracker(self.state_history),
            journal_repository=self.journals,
            operator_profile_service=OperatorProfileService(self.profiles),
            relationship_names_service=self.names,
            relationship_seed_repository=self.seeds,
            address_preference_repository=self.address_preferences,
            story_scene_sessions=self.scenes,
        )
        self.character_service = CharacterService(self.characters)
        self.undo = TurnUndoService(
            journal_repository=self.journals,
            conversation_repository=self.conversations,
            character_repository=self.characters,
            memory_repository=self.memories,
            state_history_repository=self.state_history,
            address_preference_repository=self.address_preferences,
            address_change_log_repository=self.change_log,
            relationship_seed_repository=self.seeds,
            scene_session_repository=self.scenes,
            operator_persona_repository=persona_repository,
        )

    async def new_character(self, name: str = "Yuki") -> tuple[str, str]:
        """Create a character and return ``(character_id, operator_id)``."""
        created = await self.character_service.create_character(
            CreateCharacterRequest(name=name),
        )
        character = await self.characters.get(created.id)
        assert character is not None
        return created.id, character.user_id

    async def seed_names(
        self, character_id: str, operator_id: str,
        *, user_address_name: str = "", character_address_name: str = "",
    ) -> None:
        await self.seeds.save(CharacterOperatorRelationshipSeed(
            character_id=character_id,
            operator_id=operator_id,
            relationship_label="同事",
            user_address_name=user_address_name,
            character_address_name=character_address_name,
        ))

    async def names_now(
        self, character_id: str, operator_id: str,
    ) -> tuple[str, str]:
        seed = await self.seeds.get(character_id, operator_id)
        assert seed is not None
        return seed.user_address_name, seed.character_address_name

    async def log_for(
        self, character_id: str, operator_id: str,
    ) -> list[AddressChangeEvent]:
        return await self.change_log.list_for_pair(
            character_id=character_id, operator_id=operator_id,
        )

    async def set_display_name(self, operator_id: str, name: str) -> None:
        """Give the operator a real platform display name.

        The bottom rung of ``resolve_player_address``'s direction-A
        ladder. Without it the placeholder 「操作者」 is treated as "no
        name", and a test asserting what the character calls the player
        after an undo would be asserting against a sentinel instead of a
        value.
        """
        await self.profiles.save(
            OperatorProfile(id=operator_id, display_name=name),
        )

    async def player_address_now(
        self, character_id: str, operator_id: str,
    ) -> str:
        """What the character would call the player right now.

        Runs the real resolver over the three sources it ranks, which is
        the only assertion that can tell "the seed was restored" apart
        from "the player stopped being called 森森" — the persona row
        sits between the seed and the profile and outranks both when the
        seed is empty.
        """
        return resolve_player_address(
            seed=await self.seeds.get(character_id, operator_id),
            persona=await self.persona_repository.get(
                character_id, operator_id,
            ),
            profile=await self.profiles.get(operator_id),
        ).primary

    async def persona_name_now(
        self, character_id: str, operator_id: str,
    ) -> str | None:
        persona = await self.persona_repository.get(character_id, operator_id)
        field = persona.fields_by_layer(1).get("name")
        return None if field is None else field.value

    async def seed_persona_name(
        self, character_id: str, operator_id: str, value: str,
        *, source: str = "extraction",
    ) -> None:
        await self.persona_repository.upsert_field(
            character_id, operator_id,
            ProfileField(
                character_id=character_id,
                field_key="name",
                layer=1,
                value=value,
                confidence=0.85,
                evidence_refs=(
                    EvidenceRef(
                        turn_id="seeded",
                        conversation_id="seeded",
                        quote=value,
                        extracted_at=datetime.now(timezone.utc),
                    ),
                ),
                last_updated=datetime.now(timezone.utc),
                update_count=1,
                source=source,
            ),
        )

    async def open_scene(
        self, character_id: str, conversation_id: str,
        *, opened_at: datetime | None = None,
    ) -> StorySceneSession:
        scene = StorySceneSession.open_scene(
            character_id=character_id,
            conversation_id=conversation_id,
            source_layer=SCENE_LAYER_SIDE_STORY,
            title="頂樓的雨",
            dramatic_question="她會說出口嗎？",
            opened_at=opened_at,
        )
        await self.scenes.add(scene)
        return scene


# ---------- C: the rename -------------------------------------------------


@pytest.mark.asyncio
async def test_undo_reverts_an_observed_rename_and_its_log_entry() -> None:
    """The ticket's headline case, end to end.

    The player says 「叫我森森」, the post-turn extractor's signal goes
    through the real ``RelationshipNamesService``, and the seed the
    prompt builder reads now says 森森. After the undo the character has
    to be back on 老師 *and* the ``observed`` log entry — which is what
    the prompt surfaces as "使用者從 X 改成希望你叫 Y" — has to be gone
    with it, or the character explains a rename that no longer exists.
    """
    h = _Harness()
    character_id, operator_id = await h.new_character()
    await h.seed_names(character_id, operator_id, user_address_name="老師")
    h.post_turn.address_changes = [AddressChangeSignal(
        direction=DIRECTION_PLAYER, new_value="森森", subject="operator_self",
    )]

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="以後叫我森森",
    ))

    assert await h.names_now(character_id, operator_id) == ("森森", "")
    entries = await h.log_for(character_id, operator_id)
    assert [(e.direction, e.old_value, e.new_value, e.source) for e in entries] == [
        (DIRECTION_PLAYER, "老師", "森森", SOURCE_OBSERVED),
    ]

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.reverted_address_log_entries == 1
    assert result.restored_address_preference is True
    assert await h.names_now(character_id, operator_id) == ("老師", "")
    assert await h.log_for(character_id, operator_id) == []


@pytest.mark.asyncio
async def test_undo_reverts_both_directions_of_one_turn() -> None:
    """One turn can rename in both directions; both have to come back.

    A step that restored only ``user_address_name`` would still look
    green on the headline test above.
    """
    h = _Harness()
    character_id, operator_id = await h.new_character()
    await h.seed_names(
        character_id, operator_id,
        user_address_name="老師", character_address_name="學姊",
    )
    h.post_turn.address_changes = [
        AddressChangeSignal(
            direction=DIRECTION_PLAYER, new_value="森森",
            subject="operator_self",
        ),
        AddressChangeSignal(
            direction=DIRECTION_CHARACTER, new_value="小雪",
        ),
    ]

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="叫我森森，我叫你小雪",
    ))
    assert await h.names_now(character_id, operator_id) == ("森森", "小雪")

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.reverted_address_log_entries == 2
    assert await h.names_now(character_id, operator_id) == ("老師", "學姊")
    assert await h.log_for(character_id, operator_id) == []


@pytest.mark.asyncio
async def test_undo_of_an_ordinary_turn_leaves_an_earlier_rename_standing() -> None:
    """The negative twin, and the more damaging failure of the two.

    Turn 1 renames, turn 2 is ordinary. Undoing turn 2 must not reach
    back past its own ``turn_started_at`` — a rename the player made and
    kept is not a side effect of the turn being reversed.
    """
    h = _Harness()
    character_id, operator_id = await h.new_character()
    await h.seed_names(character_id, operator_id, user_address_name="老師")
    h.post_turn.address_changes = [AddressChangeSignal(
        direction=DIRECTION_PLAYER, new_value="森森", subject="operator_self",
    )]
    first = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="以後叫我森森",
    ))

    h.post_turn.address_changes = []
    await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, conversation_id=first.conversation_id,
        message="今天天氣不錯",
    ))

    result = await h.undo.undo_last_turn(first.conversation_id)

    assert result.reverted_address_log_entries == 0
    assert result.restored_address_preference is False
    assert await h.names_now(character_id, operator_id) == ("森森", "")
    surviving = await h.log_for(character_id, operator_id)
    assert [e.new_value for e in surviving] == ["森森"]


@pytest.mark.asyncio
async def test_undo_never_reverts_a_settings_ui_edit() -> None:
    """``player_edit`` inside the turn's window survives the undo.

    A rename typed into the settings UI is a deliberate act that happens
    to have landed while a turn was in flight, not a side effect of it.
    Scoping the delete to ``observed`` is the only thing separating the
    two, so it gets its own test rather than riding on the source field
    being passed through.
    """
    h = _Harness()
    character_id, operator_id = await h.new_character()
    await h.seed_names(character_id, operator_id, user_address_name="老師")
    h.post_turn.address_changes = []
    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="嗨",
    ))
    # After the turn started, through the same service the settings route
    # uses — default source is ``player_edit``.
    await h.names.update_names(
        character_id=character_id, operator_id=operator_id,
        user_address_name="森森",
    )

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.reverted_address_log_entries == 0
    assert await h.names_now(character_id, operator_id) == ("森森", "")
    surviving = await h.log_for(character_id, operator_id)
    assert [(e.source, e.new_value) for e in surviving] == [
        (SOURCE_PLAYER_EDIT, "森森"),
    ]


@pytest.mark.asyncio
async def test_undo_of_an_ordinary_turn_does_not_rewrite_the_preference_row() -> None:
    """An untouched ``OperatorAddressPreference`` row is not a restore.

    The journal snapshots this row on every turn, so the pre-turn
    snapshot is present even when the turn never went near it. Writing
    it back would be a redundant write the result then reports as a
    restore — telling the player their address preference was rolled
    back on a turn that only said 「嗨」.
    """
    h = _Harness()
    character_id, operator_id = await h.new_character()
    pre_turn = OperatorAddressPreference(
        character_id=character_id, operator_id=operator_id,
        salutation="小雪", formality_level="low",
        updated_at=datetime.now(timezone.utc) - timedelta(hours=6),
    )
    await h.address_preferences.upsert(pre_turn)
    h.post_turn.address_changes = []

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="嗨",
    ))
    journal = await h.journals.get_latest(response.conversation_id)
    assert journal is not None
    assert journal.prev_address_preference is not None, (
        "the journal has to carry the snapshot for this test to be about "
        "the guard rather than about an absent snapshot"
    )

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.restored_address_preference is False
    assert await h.address_preferences.get(
        character_id=character_id, operator_id=operator_id,
    ) == pre_turn


@pytest.mark.asyncio
async def test_undo_restores_a_preference_row_the_dream_pass_moved() -> None:
    """The rare case the snapshot exists for, kept honest.

    The guard above must not turn into "never restore": when the row
    really did drift after the turn started, the pre-turn value goes
    back.
    """
    h = _Harness()
    character_id, operator_id = await h.new_character()
    pre_turn = OperatorAddressPreference(
        character_id=character_id, operator_id=operator_id,
        salutation="小雪", formality_level="low",
        updated_at=datetime.now(timezone.utc) - timedelta(hours=6),
    )
    await h.address_preferences.upsert(pre_turn)
    h.post_turn.address_changes = []

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="嗨",
    ))
    await h.address_preferences.upsert(pre_turn.with_updates(
        salutation="老師", formality_level="high",
    ))

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.restored_address_preference is True
    assert await h.address_preferences.get(
        character_id=character_id, operator_id=operator_id,
    ) == pre_turn


@pytest.mark.asyncio
async def test_undo_without_the_address_repositories_is_a_no_op() -> None:
    """Self-host can run with the rename subsystem unwired; the step
    reports nothing rather than failing the whole rollback."""
    h = _Harness()
    undo = TurnUndoService(
        journal_repository=h.journals,
        conversation_repository=h.conversations,
        character_repository=h.characters,
        memory_repository=h.memories,
        state_history_repository=h.state_history,
    )
    character_id, operator_id = await h.new_character()
    await h.seed_names(character_id, operator_id, user_address_name="老師")
    h.post_turn.address_changes = [AddressChangeSignal(
        direction=DIRECTION_PLAYER, new_value="森森", subject="operator_self",
    )]
    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="以後叫我森森",
    ))

    result = await undo.undo_last_turn(response.conversation_id)

    assert result.reverted_address_log_entries == 0
    assert result.restored_address_preference is False
    assert result.reverted_messages == 2
    assert await h.names_now(character_id, operator_id) == ("森森", "")


# ---------- D: the scene --------------------------------------------------


@pytest.mark.asyncio
async def test_undo_reopens_a_scene_the_turn_closed() -> None:
    """A close is a one-way door for the player; the undo is the only
    thing that can put a wrongly-closed 起幕 scene back.

    The scene is closed *after* the turn's journal was taken, which is
    exactly the shape ``_close_scene_if_resolved`` produces: the verdict
    lands at the end of the turn the journal snapshotted at the start of.
    """
    h = _Harness()
    character_id, _ = await h.new_character()
    h.post_turn.address_changes = []
    opening = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="開場",
    ))
    scene = await h.open_scene(character_id, opening.conversation_id)

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, conversation_id=opening.conversation_id,
        message="我想我該說了",
    ))
    journal = await h.journals.get_latest(response.conversation_id)
    assert journal is not None
    assert journal.prev_scene_session is not None, (
        "the journal has to capture the open scene, or the step has "
        "nothing to restore from"
    )
    closed = await h.scenes.close(
        scene.id, reason=SCENE_CLOSE_RESOLVED,
        at=datetime.now(timezone.utc),
    )
    assert closed is not None and not closed.is_open

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.restored_scene_session is True
    reopened = await h.scenes.get(scene.id)
    assert reopened is not None
    assert reopened.is_open
    assert reopened.closed_at is None
    assert reopened.closed_reason is None
    assert await h.scenes.get_open_for_character(character_id) is not None


@pytest.mark.asyncio
async def test_undo_does_not_drag_a_live_scenes_clock_backwards() -> None:
    """The negative twin: an ordinary in-scene turn leaves the scene open.

    Re-saving the pre-turn snapshot over a live scene would rewind
    ``last_activity_at``, which is the idle clock the SC1-E timeout
    closer reads — so a blind restore would hand the timeout closer a
    scene that looks abandoned and let it close a scene the player is
    still sitting in.
    """
    h = _Harness()
    character_id, _ = await h.new_character()
    h.post_turn.address_changes = []
    opening = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="開場",
    ))
    scene = await h.open_scene(
        character_id, opening.conversation_id,
        opened_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, conversation_id=opening.conversation_id,
        message="還在這裡",
    ))
    await h.scenes.touch_activity(scene.id, at=datetime.now(timezone.utc))
    before_undo = await h.scenes.get(scene.id)
    assert before_undo is not None
    assert before_undo.last_activity_at > scene.last_activity_at

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.restored_scene_session is False
    after_undo = await h.scenes.get(scene.id)
    assert after_undo == before_undo


@pytest.mark.asyncio
async def test_undo_will_not_reopen_when_another_scene_is_already_open() -> None:
    """One open scene per character is a storage invariant, not a wish.

    The turn closed scene A, the player then opened scene B. Reopening A
    would give the character two live scenes; the conflict is caught and
    reported as "not restored", and neither scene is disturbed.
    """
    h = _Harness()
    character_id, _ = await h.new_character()
    h.post_turn.address_changes = []
    opening = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="開場",
    ))
    first_scene = await h.open_scene(character_id, opening.conversation_id)

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, conversation_id=opening.conversation_id,
        message="收尾",
    ))
    await h.scenes.close(
        first_scene.id, reason=SCENE_CLOSE_RESOLVED,
        at=datetime.now(timezone.utc),
    )
    second_scene = await h.open_scene(character_id, opening.conversation_id)

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.restored_scene_session is False
    stale = await h.scenes.get(first_scene.id)
    assert stale is not None and not stale.is_open
    live = await h.scenes.get_open_for_character(character_id)
    assert live is not None and live.id == second_scene.id


@pytest.mark.asyncio
async def test_undo_of_a_turn_with_no_scene_touches_nothing() -> None:
    """No scene was open pre-turn, so there is no snapshot — and a scene
    opened *after* the turn started is not this turn's to close."""
    h = _Harness()
    character_id, _ = await h.new_character()
    h.post_turn.address_changes = []
    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="嗨",
    ))
    journal = await h.journals.get_latest(response.conversation_id)
    assert journal is not None
    assert journal.prev_scene_session is None
    later_scene = await h.open_scene(character_id, response.conversation_id)

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.restored_scene_session is False
    still_there = await h.scenes.get(later_scene.id)
    assert still_there is not None and still_there.is_open


@pytest.mark.asyncio
async def test_undo_without_a_scene_repository_is_a_no_op() -> None:
    """The 起幕 subsystem is optional; its absence must not fail the
    rollback or claim a scene came back."""
    h = _Harness()
    undo = TurnUndoService(
        journal_repository=h.journals,
        conversation_repository=h.conversations,
        character_repository=h.characters,
        memory_repository=h.memories,
        state_history_repository=h.state_history,
    )
    character_id, _ = await h.new_character()
    h.post_turn.address_changes = []
    opening = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="開場",
    ))
    scene = await h.open_scene(character_id, opening.conversation_id)
    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, conversation_id=opening.conversation_id,
        message="收尾",
    ))
    await h.scenes.close(
        scene.id, reason=SCENE_CLOSE_RESOLVED, at=datetime.now(timezone.utc),
    )

    result = await undo.undo_last_turn(response.conversation_id)

    assert result.restored_scene_session is False
    assert result.reverted_messages == 2
    still_closed = await h.scenes.get(scene.id)
    assert still_closed is not None and not still_closed.is_open


@pytest_asyncio.fixture
async def persona_repository():
    """The real ``SAOperatorPersonaRepository`` over in-process SQLite.

    A hand-rolled fake would have to re-implement supersede-then-insert,
    the one-confirmed-row-per-key constraint, and the state vocabulary —
    i.e. exactly the mechanics under test — so it would prove the fake
    and not the adapter. SQLite is the self-host production store, and
    the FK pragma is left off here for the same reason
    ``persistence/engine.py`` leaves it off in production, so the other
    stores in the harness can stay in-memory without their rows needing
    to exist.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield SAOperatorPersonaRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


# ---------- C2: the rename's persona half ---------------------------------


@pytest.mark.asyncio
async def test_undo_stops_the_character_calling_the_player_the_new_name(
    persona_repository,
) -> None:
    """The ticket's real symptom, asserted through the real resolver.

    「以後叫我森森」 the first time a player names themselves is the
    commonest shape of this rename, and its ``old_value`` is therefore
    the empty string. Restoring the seed alone puts an empty string back
    — and ``resolve_player_address`` ranks
    ``seed.user_address_name > persona layer1 name > profile.display_name``,
    so an empty seed falls straight through to the persona row the
    reconcile wrote and the character goes on saying 森森 after the
    undo. Asserting the seed here would have been green; asserting what
    the character actually calls the player is not.
    """
    h = _Harness(persona_repository)
    character_id, operator_id = await h.new_character()
    await h.set_display_name(operator_id, "阿丹")
    h.post_turn.address_changes = [AddressChangeSignal(
        direction=DIRECTION_PLAYER, new_value="森森", subject="operator_self",
    )]

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="以後叫我森森",
    ))

    assert await h.persona_name_now(character_id, operator_id) == "森森"
    assert await h.player_address_now(character_id, operator_id) == "森森"

    await h.undo.undo_last_turn(response.conversation_id)

    assert await h.persona_name_now(character_id, operator_id) is None
    assert await h.player_address_now(character_id, operator_id) == "阿丹"


@pytest.mark.asyncio
async def test_undo_brings_back_the_persona_name_the_rename_superseded(
    persona_repository,
) -> None:
    """Reversing the insert is only half of it.

    ``set_explicit_field_for_operator`` stamps the previous confirmed
    row ``superseded`` *before* inserting the new one, so an undo that
    only rejected the insert would leave the player with no learned name
    at all — the rename would have destroyed 小明 permanently and the
    undo would have called that a rollback.
    """
    h = _Harness(persona_repository)
    character_id, operator_id = await h.new_character()
    await h.seed_persona_name(character_id, operator_id, "小明")
    h.post_turn.address_changes = [AddressChangeSignal(
        direction=DIRECTION_PLAYER, new_value="森森", subject="operator_self",
    )]

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="以後叫我森森",
    ))
    assert await h.persona_name_now(character_id, operator_id) == "森森"

    await h.undo.undo_last_turn(response.conversation_id)

    assert await h.persona_name_now(character_id, operator_id) == "小明"


@pytest.mark.asyncio
async def test_undo_of_an_ordinary_turn_leaves_the_persona_name_alone(
    persona_repository,
) -> None:
    """The negative twin. A turn that renamed nothing must not reach the
    persona store at all — the learned name is a fact the player never
    asked to take back."""
    h = _Harness(persona_repository)
    character_id, operator_id = await h.new_character()
    await h.seed_persona_name(character_id, operator_id, "小明")
    h.post_turn.address_changes = []

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="今天天氣不錯",
    ))

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.reverted_address_log_entries == 0
    assert await h.persona_name_now(character_id, operator_id) == "小明"


@pytest.mark.asyncio
async def test_undo_does_not_resurrect_a_name_something_else_replaced(
    persona_repository,
) -> None:
    """The turn's window is not a licence over everything in it.

    The rename wrote 森森; the dream pass then superseded *that* and
    wrote 阿丹, still inside the reverted turn's window. Reversing by
    time alone would reject 阿丹 and put 森森 back — undoing a write
    this turn never made, and restoring the very value the undo exists
    to remove. Keying the revert on the value the deleted rename log
    says was written is what separates the two.
    """
    h = _Harness(persona_repository)
    character_id, operator_id = await h.new_character()
    h.post_turn.address_changes = [AddressChangeSignal(
        direction=DIRECTION_PLAYER, new_value="森森", subject="operator_self",
    )]

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="以後叫我森森",
    ))
    assert await h.persona_name_now(character_id, operator_id) == "森森"
    # A later, unrelated write to the same key inside the same window.
    await h.persona_service.set_explicit_field_for_operator(
        character_id=character_id, operator_id=operator_id,
        field_key="name", value="阿丹", observed=True,
    )

    await h.undo.undo_last_turn(response.conversation_id)

    assert await h.persona_name_now(character_id, operator_id) == "阿丹"


@pytest.mark.asyncio
async def test_undo_without_the_persona_repository_still_reverts_the_seed(
    persona_repository,
) -> None:
    """The persona store is optional wiring; its absence must not cost
    the rename rollback the half that does not need it."""
    h = _Harness(persona_repository)
    undo = TurnUndoService(
        journal_repository=h.journals,
        conversation_repository=h.conversations,
        character_repository=h.characters,
        memory_repository=h.memories,
        state_history_repository=h.state_history,
        address_change_log_repository=h.change_log,
        relationship_seed_repository=h.seeds,
    )
    character_id, operator_id = await h.new_character()
    await h.seed_names(character_id, operator_id, user_address_name="老師")
    h.post_turn.address_changes = [AddressChangeSignal(
        direction=DIRECTION_PLAYER, new_value="森森", subject="operator_self",
    )]
    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="以後叫我森森",
    ))

    result = await undo.undo_last_turn(response.conversation_id)

    assert result.reverted_address_log_entries == 1
    assert await h.names_now(character_id, operator_id) == ("老師", "")
    # Untouched, because nothing was wired to touch it.
    assert await h.persona_name_now(character_id, operator_id) == "森森"


# ---------- D2: whose close was it? ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [SCENE_CLOSE_MANUAL, SCENE_CLOSE_TIMEOUT])
async def test_undo_does_not_reopen_a_scene_the_turn_did_not_close(
    reason: str,
) -> None:
    """Only the in-turn verdict is this turn's close.

    ``manual`` is the player pressing 「結束場景」 and ``timeout`` is
    SC1-E's idle sweep deciding the player walked away. Undoing the
    turn that came before either one must not drag the scene back open,
    clear its ``closed_at`` / ``closed_reason``, rewind its activity
    clock, and then report ``restored_scene_session=True`` for a close
    the turn had nothing to do with.
    """
    h = _Harness()
    character_id, _ = await h.new_character()
    h.post_turn.address_changes = []
    opening = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="開場",
    ))
    scene = await h.open_scene(character_id, opening.conversation_id)

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, conversation_id=opening.conversation_id,
        message="還在這裡",
    ))
    closed = await h.scenes.close(
        scene.id, reason=reason, at=datetime.now(timezone.utc),
    )
    assert closed is not None

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.restored_scene_session is False
    after = await h.scenes.get(scene.id)
    assert after == closed
    assert await h.scenes.get_open_for_character(character_id) is None


@pytest.mark.asyncio
async def test_undo_does_not_reopen_a_resolved_close_from_before_the_turn(
) -> None:
    """``resolved`` is the right kind of close, not proof of ownership.

    Undo is last-turn-only, so a ``resolved`` close stamped before this
    turn even started belongs to an earlier one whose journal is already
    gone. Reason alone would reopen it; the ``closed_at`` window is what
    binds the close to the turn being reversed.
    """
    h = _Harness()
    character_id, _ = await h.new_character()
    h.post_turn.address_changes = []
    opening = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="開場",
    ))
    scene = await h.open_scene(character_id, opening.conversation_id)

    response = await h.chat.send_message(SendChatMessageRequest(
        character_id=character_id, conversation_id=opening.conversation_id,
        message="收尾",
    ))
    journal = await h.journals.get_latest(response.conversation_id)
    assert journal is not None
    closed = await h.scenes.close(
        scene.id, reason=SCENE_CLOSE_RESOLVED,
        at=journal.turn_started_at - timedelta(seconds=1),
    )
    assert closed is not None

    result = await h.undo.undo_last_turn(response.conversation_id)

    assert result.restored_scene_session is False
    after = await h.scenes.get(scene.id)
    assert after is not None and not after.is_open
