"""BD7 — 分歧劇場走過的路 → arc 劇本草稿."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from kokoro_link.application.services.arc_template_intake_service import (
    BeatDraft,
    TemplateDraft,
)
from kokoro_link.application.services.drama_to_arc_draft_service import (
    DramaToArcDraftService,
)
from kokoro_link.contracts.drama_to_arc import DramaToArcContext
from kokoro_link.contracts.fusion_to_arc import (
    FUSION_OPERATOR_MODE_OBSERVER,
    FUSION_OPERATOR_MODE_UNCHANGED,
    FUSION_OPERATOR_MODE_WRITE_IN,
)
from kokoro_link.domain.entities.branching_drama import (
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
    STATUS_READY,
    BranchingDrama,
    DramaNode,
    DramaSession,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.value_objects.character_state import CharacterState


# ── fixtures ──────────────────────────────────────────────────────────


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


def _drama(position: str = OPERATOR_POSITION_CENTRAL) -> BranchingDrama:
    return (
        BranchingDrama.create_pending(
            id="drama-1",
            character_ids=["c-a", "c-b"],
            prompt="Find the signal under the observatory glass.",
            total_segments=3,
            operator_position=position,
            operator_note="我演她失聯多年的哥哥",
        )
        .with_title("Glass Signal")
        .with_status(STATUS_READY)
    )


def _root_node() -> DramaNode:
    return DramaNode.create_root(
        id="root",
        drama_id="drama-1",
        title="Opening",
        summary="The glass roof hums.",
        appearing_character_ids=("c-a", "c-b"),
    )


def _child_node() -> DramaNode:
    return DramaNode.create_child(
        id="node-2",
        drama_id="drama-1",
        parent_node_id="root",
        depth=1,
        tone="dark",
        title="The Cut Wire",
        summary="She finds the wire has been cut on purpose.",
        appearing_character_ids=("c-a",),
    )


def _played_session(*, ended: bool = True) -> DramaSession:
    session = DramaSession.start(
        id="sess-1", drama_id="drama-1", root_node_id="root",
    )
    session = session.with_turn(node_id="root", narration="霧散開了。")
    session = session.with_exchange(
        player_input="我先開口。", response="她抬起頭看向聲音的方向。",
    )
    session = session.with_turn(
        node_id="node-2",
        narration="她把那截電線舉到燈下。",
        player_input="我們走深一點。",
        chosen_tone="dark",
    )
    return session.end() if ended else session


def _draft() -> TemplateDraft:
    return TemplateDraft(
        id="glass_signal_arc",
        title="Glass Signal",
        premise="A playable arc about a signal nobody was supposed to hear.",
        theme="discovery",
        tone="dramatic",
        duration_days=7,
        beats=(
            BeatDraft(
                sequence=0,
                day_offset=0,
                title="First Step",
                summary="The character decides whether to follow the hum.",
            ),
        ),
    )


# ── stubs ─────────────────────────────────────────────────────────────


class _DramaServiceStub:
    def __init__(
        self,
        *,
        drama: BranchingDrama | None,
        session: DramaSession | None,
        nodes: dict[str, DramaNode] | None = None,
        node_raises: bool = False,
    ) -> None:
        self.drama = drama
        self.session = session
        self.nodes = nodes or {"root": _root_node(), "node-2": _child_node()}
        self.node_raises = node_raises

    async def get(self, drama_id: str) -> BranchingDrama | None:
        return self.drama if drama_id == "drama-1" else None

    async def get_session(self, session_id: str) -> DramaSession | None:
        return self.session if session_id == "sess-1" else None

    async def get_node(self, node_id: str) -> DramaNode | None:
        if self.node_raises:
            raise RuntimeError("node store unavailable")
        return self.nodes.get(node_id)


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
        self.contexts: list[DramaToArcContext] = []

    async def adapt(self, context: DramaToArcContext) -> TemplateDraft | None:
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
        return self.profile


def _seed(character_id: str, address: str) -> CharacterOperatorRelationshipSeed:
    return CharacterOperatorRelationshipSeed(
        character_id=character_id,
        operator_id="op-1",
        relationship_label="舊識",
        user_address_name=address,
    )


_UNSET = object()


def _service(
    *,
    drama: BranchingDrama | None = _UNSET,  # type: ignore[assignment]
    session: DramaSession | None = None,
    adapter: _AdapterStub | None = None,
    characters: dict[str, Character] | None = None,
    seed_repository: _SeedRepositoryStub | None = None,
    operator_profile: _OperatorProfileStub | None = _OperatorProfileStub(),
    node_raises: bool = False,
) -> tuple[DramaToArcDraftService, _AdapterStub]:
    adapter = adapter or _AdapterStub(_draft())
    service = DramaToArcDraftService(
        drama_service=_DramaServiceStub(
            drama=_drama() if drama is _UNSET else drama,
            session=_played_session() if session is None else session,
            node_raises=node_raises,
        ),
        character_service=_CharacterServiceStub(  # type: ignore[arg-type]
            characters
            if characters is not None
            else {"c-a": _character("c-a"), "c-b": _character("c-b")},
        ),
        adapter=adapter,
        relationship_seed_repository=seed_repository,  # type: ignore[arg-type]
        operator_profile_service=_OperatorProfileServiceStub(operator_profile),
    )
    return service, adapter


# ── 路徑素材組裝 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_walked_path_carries_outline_and_transcript_in_play_order() -> None:
    service, adapter = _service()

    draft = await service.adapt("drama-1", "sess-1", user_id="alice")

    assert draft == _draft()
    path = adapter.contexts[0].path
    assert [b.sequence for b in path] == [0, 1]
    # The authored beat…
    assert path[0].title == "Opening"
    assert path[1].summary == "She finds the wire has been cut on purpose."
    assert path[1].depth == 1
    assert path[1].tone == "dark"
    # …joined to what that beat actually became for this player.
    assert path[0].narration == "霧散開了。"
    assert path[0].exchanges[0].player_input == "我先開口。"
    assert path[0].exchanges[0].response == "她抬起頭看向聲音的方向。"
    # Everything the player said travels as exchanges — there is no
    # beat-level input slot, because the drama service never writes one
    # (FX3).
    assert not hasattr(path[1], "player_input")


@pytest.mark.asyncio
async def test_drama_and_cast_travel_with_the_path() -> None:
    service, adapter = _service()

    await service.adapt("drama-1", "sess-1", user_id="alice")

    context = adapter.contexts[0]
    assert context.drama.id == "drama-1"
    assert [c.id for c in context.characters] == ["c-a", "c-b"]


@pytest.mark.asyncio
async def test_missing_node_keeps_the_transcript_it_cannot_recover() -> None:
    """A lost outline row must not cost the narration — that is the part
    that exists nowhere else."""
    service, adapter = _service(node_raises=True)

    draft = await service.adapt("drama-1", "sess-1", user_id="alice")

    assert draft == _draft()
    path = adapter.contexts[0].path
    assert [b.title for b in path] == ["", ""]
    assert path[0].narration == "霧散開了。"


@pytest.mark.asyncio
async def test_instruction_is_trimmed_and_forwarded() -> None:
    service, adapter = _service()

    await service.adapt(
        "drama-1", "sess-1", user_id="alice", instruction="  安靜一點  ",
    )

    assert adapter.contexts[0].instruction == "安靜一點"


@pytest.mark.asyncio
async def test_fail_soft_none_from_adapter_propagates() -> None:
    service, _ = _service(adapter=_AdapterStub(None))

    assert await service.adapt("drama-1", "sess-1", user_id="alice") is None


# ── ended 前置 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_playing_session_is_refused() -> None:
    service, adapter = _service(session=_played_session(ended=False))

    with pytest.raises(ValueError, match="not ended"):
        await service.adapt("drama-1", "sess-1", user_id="alice")

    assert adapter.contexts == []


@pytest.mark.asyncio
async def test_ended_session_with_no_turns_is_refused() -> None:
    empty = DramaSession.start(
        id="sess-1", drama_id="drama-1", root_node_id="root",
    ).end()
    service, adapter = _service(session=empty)

    with pytest.raises(ValueError, match="no played path"):
        await service.adapt("drama-1", "sess-1", user_id="alice")

    assert adapter.contexts == []


# ── ownership ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_session_is_not_found() -> None:
    service, adapter = _service()

    with pytest.raises(ValueError, match="not found"):
        await service.adapt("drama-1", "sess-other", user_id="alice")

    assert adapter.contexts == []


@pytest.mark.asyncio
async def test_session_from_another_drama_is_not_found() -> None:
    """The (drama, session) pair is the address — answering 'wrong drama'
    would confirm the id exists somewhere."""
    foreign = DramaSession.start(
        id="sess-1", drama_id="drama-9", root_node_id="root",
    ).with_turn(node_id="root", narration="…").end()
    service, adapter = _service(session=foreign)

    with pytest.raises(ValueError, match="not found"):
        await service.adapt("drama-1", "sess-1", user_id="alice")

    assert adapter.contexts == []


@pytest.mark.asyncio
async def test_cast_owned_by_another_user_is_refused() -> None:
    service, adapter = _service(
        characters={
            "c-a": _character("c-a"),
            "c-b": _character("c-b", user_id="mallory"),
        },
    )

    with pytest.raises(ValueError, match="Character not found"):
        await service.adapt("drama-1", "sess-1", user_id="alice")

    assert adapter.contexts == []


@pytest.mark.asyncio
async def test_missing_drama_is_not_found() -> None:
    service, adapter = _service(drama=None)

    with pytest.raises(ValueError, match="not found"):
        await service.adapt("drama-1", "sess-1", user_id="alice")

    assert adapter.contexts == []


# ── 位置 → 模式 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("position", "expected_mode"),
    [
        (OPERATOR_POSITION_CENTRAL, FUSION_OPERATOR_MODE_WRITE_IN),
        (OPERATOR_POSITION_PRESENT, FUSION_OPERATOR_MODE_OBSERVER),
        (OPERATOR_POSITION_ABSENT, FUSION_OPERATOR_MODE_UNCHANGED),
    ],
)
async def test_unstated_mode_is_prefilled_from_how_it_was_played(
    position: str, expected_mode: str,
) -> None:
    service, adapter = _service(drama=_drama(position))

    await service.adapt("drama-1", "sess-1", user_id="alice")

    assert adapter.contexts[0].operator_mode == expected_mode


@pytest.mark.asyncio
async def test_explicit_mode_overrides_the_prefill() -> None:
    """A player who watched a drama may still ask to be written in."""
    service, adapter = _service(drama=_drama(OPERATOR_POSITION_ABSENT))

    await service.adapt(
        "drama-1",
        "sess-1",
        user_id="alice",
        operator_mode=FUSION_OPERATOR_MODE_WRITE_IN,
    )

    assert adapter.contexts[0].operator_mode == FUSION_OPERATOR_MODE_WRITE_IN


@pytest.mark.asyncio
async def test_unknown_mode_raises_instead_of_picking_a_branch() -> None:
    service, adapter = _service()

    with pytest.raises(ValueError, match="operator_mode"):
        await service.adapt(
            "drama-1", "sess-1", user_id="alice", operator_mode="spectator",
        )

    assert adapter.contexts == []


# ── 玩家與角色的關係素材（紅線 5） ──────────────────────────────────────


@pytest.mark.asyncio
async def test_write_in_loads_relationship_facts_per_cast_member() -> None:
    repository = _SeedRepositoryStub({
        "c-a": _seed("c-a", "小羽"),
        "c-b": _seed("c-b", "阿羽"),
    })
    service, adapter = _service(seed_repository=repository)

    await service.adapt(
        "drama-1",
        "sess-1",
        user_id="alice",
        operator_mode=FUSION_OPERATOR_MODE_WRITE_IN,
    )

    lines = adapter.contexts[0].operator_relationship_lines
    assert lines[0] == "Char c-a："
    assert "- 角色怎麼稱呼玩家：小羽" in lines
    assert "Char c-b：" in lines
    assert repository.calls == [("c-a", "op-1"), ("c-b", "op-1")]


@pytest.mark.asyncio
async def test_unchanged_never_reads_the_players_relationship() -> None:
    repository = _SeedRepositoryStub({"c-a": _seed("c-a", "小羽")})
    service, adapter = _service(
        drama=_drama(OPERATOR_POSITION_ABSENT), seed_repository=repository,
    )

    await service.adapt("drama-1", "sess-1", user_id="alice")

    assert adapter.contexts[0].operator_relationship_lines == ()
    assert repository.calls == []


@pytest.mark.asyncio
async def test_relationship_lookup_failure_still_produces_a_draft() -> None:
    service, adapter = _service(seed_repository=_SeedRepositoryStub(raises=True))

    draft = await service.adapt("drama-1", "sess-1", user_id="alice")

    assert draft == _draft()
    assert adapter.contexts[0].operator_relationship_lines == ()


@pytest.mark.asyncio
async def test_no_operator_profile_means_no_relationship_facts() -> None:
    repository = _SeedRepositoryStub({"c-a": _seed("c-a", "小羽")})
    service, adapter = _service(
        seed_repository=repository, operator_profile=None,
    )

    await service.adapt("drama-1", "sess-1", user_id="alice")

    assert adapter.contexts[0].operator_relationship_lines == ()
    assert repository.calls == []
