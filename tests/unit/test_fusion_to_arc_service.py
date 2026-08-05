from __future__ import annotations

from dataclasses import dataclass

import pytest

from kokoro_link.application.services.arc_template_intake_service import (
    BeatDraft,
    TemplateDraft,
)
from kokoro_link.application.services.fusion_to_arc_service import (
    FusionToArcDraftService,
)
from kokoro_link.contracts.fusion_to_arc import (
    FUSION_OPERATOR_MODE_OBSERVER,
    FUSION_OPERATOR_MODE_UNCHANGED,
    FUSION_OPERATOR_MODE_WRITE_IN,
    FusionToArcContext,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.fusion_story import FusionStory
from kokoro_link.domain.value_objects.character_state import CharacterState


def _character(character_id: str, *, user_id: str = "alice") -> Character:
    character = Character.create(
        name=f"Char {character_id}",
        summary="A character summary.",
        personality=[],
        interests=[],
        speaking_style="plain",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
        user_id=user_id,
    )
    object.__setattr__(character, "id", character_id)
    return character


def _story(*, ready: bool = True) -> FusionStory:
    story = FusionStory.create_pending(
        id="fusion-1",
        character_ids=["c-a", "c-b"],
        prompt="A promise is re-opened.",
    )
    if ready:
        return story.with_full_text("A finished fusion story.")
    return story


def _draft() -> TemplateDraft:
    return TemplateDraft(
        id="promise_arc",
        title="Promise Arc",
        premise="A playable arc about trust returning through small scenes.",
        theme="friendship",
        tone="daily",
        duration_days=7,
        beats=(
            BeatDraft(
                sequence=0,
                day_offset=0,
                title="First Step",
                summary="The character chooses whether to ask about the promise.",
            ),
        ),
    )


@dataclass
class _FusionStoryServiceStub:
    story: FusionStory | None

    async def get(self, story_id: str) -> FusionStory | None:
        assert story_id == "fusion-1"
        return self.story


@dataclass
class _CharacterServiceStub:
    characters: dict[str, Character]

    async def get_character_entity(
        self,
        character_id: str,
        *,
        user_id: str | None = None,
    ) -> Character | None:
        character = self.characters.get(character_id)
        if user_id and character and character.user_id != user_id:
            return None
        return character


class _AdapterStub:
    def __init__(self, draft: TemplateDraft | None) -> None:
        self.draft = draft
        self.contexts: list[FusionToArcContext] = []

    async def adapt(self, context: FusionToArcContext) -> TemplateDraft | None:
        self.contexts.append(context)
        return self.draft


class _SeedRepositoryStub:
    def __init__(
        self,
        seeds: dict[str, CharacterOperatorRelationshipSeed] | None = None,
        *,
        raises: bool = False,
    ) -> None:
        self.seeds = seeds or {}
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    async def get(
        self, character_id: str, operator_id: str,
    ) -> CharacterOperatorRelationshipSeed | None:
        self.calls.append((character_id, operator_id))
        if self.raises:
            raise RuntimeError("relationship store unavailable")
        return self.seeds.get(character_id)


@dataclass
class _OperatorProfileStub:
    id: str = "op-1"


class _OperatorProfileServiceStub:
    def __init__(self, profile: _OperatorProfileStub | None) -> None:
        self.profile = profile

    async def get_for_user(self, user_id: str) -> _OperatorProfileStub | None:
        assert user_id == "alice"
        return self.profile


def _seed(character_id: str, address: str) -> CharacterOperatorRelationshipSeed:
    return CharacterOperatorRelationshipSeed(
        character_id=character_id,
        operator_id="op-1",
        relationship_label="舊識",
        user_address_name=address,
    )


def _service(
    *,
    story: FusionStory | None,
    adapter: _AdapterStub | None = None,
    seed_repository: _SeedRepositoryStub | None = None,
    operator_profile: _OperatorProfileStub | None = _OperatorProfileStub(),
) -> tuple[FusionToArcDraftService, _AdapterStub]:
    adapter = adapter or _AdapterStub(_draft())
    service = FusionToArcDraftService(
        fusion_story_service=_FusionStoryServiceStub(story),  # type: ignore[arg-type]
        character_service=_CharacterServiceStub({  # type: ignore[arg-type]
            "c-a": _character("c-a"),
            "c-b": _character("c-b"),
        }),
        adapter=adapter,
        relationship_seed_repository=seed_repository,  # type: ignore[arg-type]
        operator_profile_service=_OperatorProfileServiceStub(operator_profile),
    )
    return service, adapter


@pytest.mark.asyncio
async def test_service_adapts_ready_story_without_saving_anything() -> None:
    service, adapter = _service(story=_story())

    draft = await service.adapt(
        "fusion-1",
        user_id="alice",
        operator_primary_language="zh-TW",
        instruction="Make the beats quieter.",
    )

    assert draft == _draft()
    assert len(adapter.contexts) == 1
    assert adapter.contexts[0].story.status == "ready"
    assert [c.id for c in adapter.contexts[0].characters] == ["c-a", "c-b"]
    assert adapter.contexts[0].instruction == "Make the beats quieter."


@pytest.mark.asyncio
async def test_service_rejects_not_ready_story() -> None:
    service, adapter = _service(story=_story(ready=False))

    with pytest.raises(ValueError, match="not ready"):
        await service.adapt("fusion-1", user_id="alice")

    assert adapter.contexts == []


@pytest.mark.asyncio
async def test_service_propagates_fail_soft_none_from_adapter() -> None:
    service, _ = _service(story=_story(), adapter=_AdapterStub(None))

    draft = await service.adapt("fusion-1", user_id="alice")

    assert draft is None


# ---------- OP1-C: the creator's three-way choice ----------------------


@pytest.mark.asyncio
async def test_unstated_mode_resolves_to_the_plans_protocol_default() -> None:
    """§3.2 — nobody chose, so the story is left alone."""
    service, adapter = _service(story=_story())

    await service.adapt("fusion-1", user_id="alice")

    assert adapter.contexts[0].operator_mode == FUSION_OPERATOR_MODE_UNCHANGED


@pytest.mark.asyncio
async def test_chosen_mode_reaches_the_adapter() -> None:
    service, adapter = _service(story=_story())

    await service.adapt(
        "fusion-1",
        user_id="alice",
        operator_mode=FUSION_OPERATOR_MODE_OBSERVER,
    )

    assert adapter.contexts[0].operator_mode == FUSION_OPERATOR_MODE_OBSERVER


@pytest.mark.asyncio
async def test_unknown_mode_raises_instead_of_picking_a_branch() -> None:
    service, adapter = _service(story=_story())

    with pytest.raises(ValueError, match="operator_mode"):
        await service.adapt(
            "fusion-1", user_id="alice", operator_mode="spectator",
        )

    assert adapter.contexts == []


@pytest.mark.asyncio
async def test_write_in_mode_loads_relationship_facts_per_cast_member() -> None:
    repository = _SeedRepositoryStub({
        "c-a": _seed("c-a", "小羽"),
        "c-b": _seed("c-b", "阿羽"),
    })
    service, adapter = _service(story=_story(), seed_repository=repository)

    await service.adapt(
        "fusion-1",
        user_id="alice",
        operator_mode=FUSION_OPERATOR_MODE_WRITE_IN,
    )

    lines = adapter.contexts[0].operator_relationship_lines
    # Grouped per character: a fusion story is a cast, and an ungrouped
    # pile of address terms leaves the model guessing whose is whose.
    assert lines[0] == "Char c-a："
    assert "- 角色怎麼稱呼玩家：小羽" in lines
    assert "Char c-b：" in lines
    assert "- 角色怎麼稱呼玩家：阿羽" in lines
    assert repository.calls == [("c-a", "op-1"), ("c-b", "op-1")]


@pytest.mark.asyncio
async def test_unchanged_mode_never_reads_the_players_relationship() -> None:
    """紅線 5 — the surest way to keep a prompt from writing the player
    into a story is to never tell it who they are."""
    repository = _SeedRepositoryStub({"c-a": _seed("c-a", "小羽")})
    service, adapter = _service(story=_story(), seed_repository=repository)

    await service.adapt(
        "fusion-1",
        user_id="alice",
        operator_mode=FUSION_OPERATOR_MODE_UNCHANGED,
    )

    assert adapter.contexts[0].operator_relationship_lines == ()
    assert repository.calls == []


@pytest.mark.asyncio
async def test_relationship_lookup_failure_still_produces_a_draft() -> None:
    repository = _SeedRepositoryStub(raises=True)
    service, adapter = _service(story=_story(), seed_repository=repository)

    draft = await service.adapt(
        "fusion-1",
        user_id="alice",
        operator_mode=FUSION_OPERATOR_MODE_WRITE_IN,
    )

    assert draft == _draft()
    assert adapter.contexts[0].operator_relationship_lines == ()


@pytest.mark.asyncio
async def test_no_operator_profile_means_no_relationship_facts() -> None:
    repository = _SeedRepositoryStub({"c-a": _seed("c-a", "小羽")})
    service, adapter = _service(
        story=_story(), seed_repository=repository, operator_profile=None,
    )

    await service.adapt(
        "fusion-1",
        user_id="alice",
        operator_mode=FUSION_OPERATOR_MODE_WRITE_IN,
    )

    assert adapter.contexts[0].operator_relationship_lines == ()
    assert repository.calls == []
