"""BDD-style smoke test for BranchingDramaService.

Wires in-memory repo + stub character service + scripted planner /
director and walks the full lifecycle:

1. Create a drama → initial layers generated in background
2. Start a session → root narration is returned
3. Advance twice → deeper layers generated lazily, reaches ending

Uses ``total_segments=3`` to keep the tree small.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

import pytest
import pytest_asyncio

from kokoro_link.application.services.branching_drama_director import (
    BranchingDramaDirector,
)
from kokoro_link.application.services.branching_drama_planner import (
    BranchingDramaPlanner,
    NodeOutline,
)
from kokoro_link.application.services.branching_drama_service import (
    BranchingDramaService,
)
from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
    FusionCharacterBriefBuilder,
)
from kokoro_link.domain.entities.branching_drama import (
    SESSION_ENDED,
    SESSION_PLAYING,
    STATUS_READY,
    TONE_DARK,
    TONE_NEUTRAL,
    TONE_SUNNY,
    DramaNode,
    DramaSessionTurn,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.application.services.character_activity_anchor import (
    CharacterActivityAnchor,
)
from kokoro_link.infrastructure.repositories.in_memory_branching_drama import (
    InMemoryBranchingDramaRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage


# ── stubs ─────────────────────────────────────────────────────────────


def _make_character(letter: str) -> Character:
    char = Character.create(
        name=f"Char-{letter}",
        summary=f"summary {letter}",
        personality=["calm"],
        interests=["coffee"],
        speaking_style="quiet",
        boundaries=[],
        appearance=f"short dark hair, traveler coat {letter}",
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation=f"androgynous traveler {letter}",
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    object.__setattr__(char, "id", f"c-{letter}")
    return char


@dataclass
class _CharServiceStub:
    by_id: dict[str, Character]

    async def get_character_entity(
        self, character_id: str,
    ) -> Character | None:
        return self.by_id.get(character_id)


class _ScriptedPlanner:
    """Returns deterministic outlines without calling an LLM."""

    def __init__(self) -> None:
        self.root_calls = 0
        self.children_calls = 0

    async def plan_root(
        self, *, prompt, briefs, total_segments,
    ) -> tuple[str, NodeOutline]:
        self.root_calls += 1
        return "測試劇場", NodeOutline(
            title="序幕",
            summary="角色們在咖啡廳相遇。",
            appearing_character_ids=tuple(b.character_id for b in briefs),
        )

    async def plan_children(
        self, *, prompt, briefs, parent_summary,
        path_context, depth, total_segments,
    ) -> dict[str, NodeOutline]:
        self.children_calls += 1
        all_ids = tuple(b.character_id for b in briefs)
        is_ending = depth == total_segments - 1
        suffix = "結局。" if is_ending else "繼續。"
        return {
            TONE_DARK: NodeOutline(
                title=f"暗-{depth}",
                summary=f"黑暗方向 depth={depth}。{suffix}",
                appearing_character_ids=all_ids,
            ),
            TONE_SUNNY: NodeOutline(
                title=f"光-{depth}",
                summary=f"陽光方向 depth={depth}。{suffix}",
                appearing_character_ids=all_ids,
            ),
            TONE_NEUTRAL: NodeOutline(
                title=f"中-{depth}",
                summary=f"中性方向 depth={depth}。{suffix}",
                appearing_character_ids=all_ids,
            ),
        }


class _ScriptedDirector:
    """Returns deterministic narration / classification."""

    def __init__(self, *, tone_sequence: list[str] | None = None) -> None:
        self._tone_idx = 0
        self._tone_seq = tone_sequence or [TONE_SUNNY, TONE_DARK]

    async def narrate(
        self, *, node, briefs, previous_turns, player_input="",
    ) -> str:
        return f"場景敘事：{node.title}（{node.summary}）"

    async def respond_in_scene(
        self, *, node, briefs, previous_turns, exchanges, player_input,
    ) -> tuple[str, str | None]:
        hint = "準備離開" if len(exchanges) >= 1 else None
        return f"回應：{player_input}", hint

    async def classify_tone(
        self, *, exchanges, children,
    ) -> str:
        tone = self._tone_seq[self._tone_idx % len(self._tone_seq)]
        self._tone_idx += 1
        return tone


class _NullBriefBuilder:
    async def build_many(self, characters):
        return self.build_persona_only_many(characters)

    def build_persona_only_many(self, characters):
        return [
            CharacterBrief(
                character_id=c.id,
                name=c.name,
                summary=c.summary or "",
                text=f"brief for {c.name}",
            )
            for c in characters
        ]


class _FakeSceneImagePort:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.characters: list[str | None] = []

    async def generate(
        self,
        *,
        positive: str,
        aspect: str = "landscape",
        character=None,
    ) -> bytes:
        self.prompts.append(f"{aspect}:{positive}")
        self.characters.append(
            getattr(character, "id", None) if character is not None else None,
        )
        return b"\x89PNG\r\n\x1a\nDRAMA"


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def char_a():
    return _make_character("a")


@pytest.fixture
def char_b():
    return _make_character("b")


@pytest.fixture
def repo():
    return InMemoryBranchingDramaRepository()


@pytest.fixture
def planner():
    return _ScriptedPlanner()


@pytest.fixture
def director():
    return _ScriptedDirector()


@pytest_asyncio.fixture
async def characters(char_a, char_b):
    """The cast, as rows — so the NF4 activity anchor has something to write."""
    repository = InMemoryCharacterRepository()
    await repository.save(char_a)
    await repository.save(char_b)
    return repository


@pytest_asyncio.fixture
async def service(repo, char_a, char_b, planner, director, characters):
    service = BranchingDramaService(
        repository=repo,
        character_service=_CharServiceStub(
            by_id={char_a.id: char_a, char_b.id: char_b},
        ),
        brief_builder=_NullBriefBuilder(),
        planner=planner,
        director=director,
        activity_anchor=CharacterActivityAnchor(characters),
    )
    yield service
    tasks = list(service._tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ── tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_generates_initial_layers(service, repo, planner):
    drama = await service.create(
        character_ids=["c-a", "c-b"],
        prompt="在咖啡廳發生的故事",
        total_segments=3,
    )
    await asyncio.sleep(0.5)

    drama = await repo.get(drama.id)
    assert drama is not None
    assert drama.status == STATUS_READY
    assert drama.title == "測試劇場"

    root = await repo.get_root_node(drama.id)
    assert root is not None
    assert root.depth == 0
    assert root.title == "序幕"

    # Only root + layer 1 generated at creation (lazy strategy)
    children = await repo.get_children(root.id)
    assert len(children) == 3
    tones = {c.tone for c in children}
    assert tones == {TONE_DARK, TONE_SUNNY, TONE_NEUTRAL}

    # Layer 2 NOT generated yet
    for child in children:
        gc = await repo.get_children(child.id)
        assert len(gc) == 0

    # Planner: 1 root + 1 layer-1
    assert planner.root_calls == 1
    assert planner.children_calls == 1


@pytest.mark.asyncio
async def test_scene_images_use_object_storage(
    repo, char_a, char_b, planner, director,
):
    storage = InMemoryObjectStorage(public_base_url="/media")
    scene_image = _FakeSceneImagePort()
    service = BranchingDramaService(
        repository=repo,
        character_service=_CharServiceStub(
            by_id={char_a.id: char_a, char_b.id: char_b},
        ),
        brief_builder=_NullBriefBuilder(),
        planner=planner,
        director=director,
        scene_image=scene_image,
        object_storage=storage,
    )

    drama = await service.create(
        character_ids=["c-a", "c-b"],
        prompt="在咖啡廳發生的故事",
        total_segments=3,
    )
    await asyncio.sleep(0.5)

    root = await repo.get_root_node(drama.id)
    assert root is not None
    assert root.image_path is not None
    assert root.image_path.startswith("/media/branching-dramas/")
    object_key = storage.object_key_from_url(root.image_path)
    assert object_key == f"branching-dramas/{drama.id}/{root.id}.png"
    assert (
        await storage.get_bytes(object_key=object_key)
        == b"\x89PNG\r\n\x1a\nDRAMA"
    )
    assert scene_image.prompts
    assert "Character gender identity: 非二元" in scene_image.prompts[0]
    assert (
        "Visual gender presentation: androgynous traveler a"
        in scene_image.prompts[0]
    )
    # The port gets a routing/attribution anchor, never a bare prompt —
    # the hosted adapter cannot resolve an account without it.
    assert scene_image.characters and scene_image.characters[0] == char_a.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("depth", "expected_images"),
    [
        # Default: root layer + the whole first branch layer (1 + 3).
        (2, 4),
        # Hosted setting: only the layer the player actually opens on.
        (1, 1),
        # Off — the port stays wired, nothing is drawn.
        (0, 0),
    ],
)
async def test_image_prefetch_depth_bounds_the_initial_draw(
    repo, char_a, char_b, planner, director, depth, expected_images,
):
    storage = InMemoryObjectStorage(public_base_url="/media")
    scene_image = _FakeSceneImagePort()
    service = BranchingDramaService(
        repository=repo,
        character_service=_CharServiceStub(
            by_id={char_a.id: char_a, char_b.id: char_b},
        ),
        brief_builder=_NullBriefBuilder(),
        planner=planner,
        director=director,
        scene_image=scene_image,
        object_storage=storage,
        image_prefetch_depth=depth,
    )

    drama = await service.create(
        character_ids=["c-a", "c-b"],
        prompt="在咖啡廳發生的故事",
        total_segments=3,
    )
    await asyncio.sleep(0.5)

    assert len(scene_image.prompts) == expected_images
    # The story itself never depends on the knob.
    drama = await repo.get(drama.id)
    assert drama is not None
    assert drama.status == STATUS_READY
    root = await repo.get_root_node(drama.id)
    assert root is not None
    if expected_images:
        assert root.image_path is not None
    else:
        assert root.image_path is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("depth", "expected_after_start"),
    [
        # Default: images follow the outline prefetch all the way down —
        # root, its 3 children, and their 9 grandchildren.
        (2, 13),
        # Narrowed: outlines still run 2 layers ahead (cheap text), but
        # only the layer the player is about to see gets drawn.
        (1, 4),
    ],
)
async def test_image_prefetch_depth_bounds_the_lazy_draw(
    repo, char_a, char_b, planner, director, depth, expected_after_start,
):
    storage = InMemoryObjectStorage(public_base_url="/media")
    scene_image = _FakeSceneImagePort()
    service = BranchingDramaService(
        repository=repo,
        character_service=_CharServiceStub(
            by_id={char_a.id: char_a, char_b.id: char_b},
        ),
        brief_builder=_NullBriefBuilder(),
        planner=planner,
        director=director,
        scene_image=scene_image,
        object_storage=storage,
        image_prefetch_depth=depth,
    )

    drama = await service.create(
        character_ids=["c-a", "c-b"],
        prompt="在咖啡廳發生的故事",
        total_segments=3,
    )
    await asyncio.sleep(0.5)
    await service.start_session(drama.id)
    await asyncio.sleep(0.5)

    assert len(scene_image.prompts) == expected_after_start
    # Outlines are unaffected by the image knob: the full tree is planned
    # either way, so gameplay never stalls waiting for a layer.
    root = await repo.get_root_node(drama.id)
    assert root is not None
    children = await repo.get_children(root.id)
    assert len(children) == 3
    for child in children:
        assert len(await repo.get_children(child.id)) == 3


@pytest.mark.asyncio
async def test_session_lifecycle(service, repo):
    drama = await service.create(
        character_ids=["c-a", "c-b"],
        prompt="在咖啡廳發生的故事",
        total_segments=3,
    )
    await asyncio.sleep(0.5)

    # Start session
    session, root, narration = await service.start_session(drama.id)
    assert session.status == SESSION_PLAYING
    assert len(session.turns) == 1
    assert session.turns[0].node_id == root.id
    assert "序幕" in narration

    # Interact within beat 1
    session, response, hint = await service.interact_session(
        session.id, player_input="我想在這個美好的日子散步",
    )
    assert "回應" in response
    assert len(session.turns[-1].exchanges) == 1

    # Advance 1: director classifies as "sunny"
    # Layer 1 children exist from creation; layer 2 generated lazily
    session, node1, narration1, is_ending = await service.advance_session(
        session.id,
    )
    assert node1.tone == TONE_SUNNY
    assert node1.depth == 1
    assert not is_ending
    assert len(session.turns) == 2

    # Interact within beat 2
    session, response, hint = await service.interact_session(
        session.id, player_input="突然覺得很不安",
    )
    assert len(session.turns[-1].exchanges) == 1

    # Advance 2: director classifies as "dark" → reaches depth 2 (final beat)
    # Children of the sunny node at depth 1 are generated lazily here
    session, node2, narration2, is_ending = await service.advance_session(
        session.id,
    )
    assert node2.tone == TONE_DARK
    assert node2.depth == 2
    assert is_ending
    assert session.status == SESSION_PLAYING
    assert len(session.turns) == 3

    # Interact at the final beat
    session, response, hint = await service.interact_session(
        session.id, player_input="最後的道別",
    )
    assert len(session.turns[-1].exchanges) == 1

    # Explicitly end the session
    session = await service.end_session(session.id)
    assert session.status == SESSION_ENDED


@pytest.mark.asyncio
async def test_lazy_generation_creates_children_on_advance(
    service, repo, planner,
):
    """Deeper layers are generated lazily during advance, not at creation."""
    drama = await service.create(
        character_ids=["c-a", "c-b"],
        prompt="懶生成測試",
        total_segments=4,
    )
    await asyncio.sleep(0.5)
    initial_children_calls = planner.children_calls

    session, root, _ = await service.start_session(drama.id)
    await asyncio.sleep(0.3)

    # Interact + advance to depth 1
    await service.interact_session(
        session.id, player_input="出發",
    )
    session, node1, _, is_ending = await service.advance_session(session.id)
    assert node1.depth == 1
    assert not is_ending

    # advance_session should have lazily generated children for node1
    children_of_node1 = await repo.get_children(node1.id)
    assert len(children_of_node1) == 3
    assert planner.children_calls > initial_children_calls


@pytest.mark.asyncio
async def test_cannot_advance_ended_session(service, repo):
    drama = await service.create(
        character_ids=["c-a", "c-b"],
        prompt="測試",
        total_segments=2,  # 2 segments → ending after 1 advance
    )
    await asyncio.sleep(0.5)

    session, _, _ = await service.start_session(drama.id)
    session, _, _, is_ending = await service.advance_session(
        session.id,
    )
    assert is_ending
    assert session.status == SESSION_PLAYING

    # Explicitly end
    session = await service.end_session(session.id)
    assert session.status == SESSION_ENDED

    with pytest.raises(ValueError, match="already ended"):
        await service.end_session(session.id)


@pytest.mark.asyncio
async def test_create_with_too_few_characters_fails(service):
    with pytest.raises(ValueError, match="at least"):
        await service.create(
            character_ids=["c-a"],
            prompt="獨角戲",
            total_segments=3,
        )


# ── NF4: every 分歧劇場 press is a foreground interaction ──────────────
#
# Each of these presses costs the player 螢火 and is a real turn with these
# characters — but none of them used to move ``last_active_at``, which is what
# dormancy, the idle down-shift and the freeze reaper all read. A player who
# lives in this surface therefore read as "never interacted": permanently
# dormant, no background life at all, on the very characters they pay to play
# with daily.


async def _anchors(characters, *ids: str):
    return [(await characters.get(cid)).state.last_active_at for cid in ids]


@pytest.mark.asyncio
async def test_creating_a_drama_marks_the_cast_engaged(service, characters):
    assert await _anchors(characters, "c-a", "c-b") == [None, None]

    await service.create(
        character_ids=["c-a", "c-b"], prompt="測試", total_segments=2,
    )
    await asyncio.sleep(0.5)

    stamps = await _anchors(characters, "c-a", "c-b")
    assert all(stamp is not None for stamp in stamps)
    # One instant for the whole cast — the members of one press must not end
    # up with anchors drifting apart.
    assert stamps[0] == stamps[1]


@pytest.mark.asyncio
async def test_starting_a_session_marks_the_cast_engaged(service, characters):
    drama = await service.create(
        character_ids=["c-a", "c-b"], prompt="測試", total_segments=2,
    )
    await asyncio.sleep(0.5)
    # Clear what the create press stamped, so what follows can only have been
    # written by ``start_session`` itself.
    for character in await characters.list():
        await characters.save(
            character.with_state(replace(character.state, last_active_at=None)),
        )
    assert await _anchors(characters, "c-a", "c-b") == [None, None]

    await service.start_session(drama.id)

    assert all(
        stamp is not None
        for stamp in await _anchors(characters, "c-a", "c-b")
    )


@pytest.mark.asyncio
async def test_an_in_beat_exchange_marks_the_cast_engaged(service, characters):
    drama = await service.create(
        character_ids=["c-a", "c-b"], prompt="測試", total_segments=2,
    )
    await asyncio.sleep(0.5)
    session, _, _ = await service.start_session(drama.id)
    before = await _anchors(characters, "c-a")

    await service.interact_session(session.id, player_input="你還好嗎？")

    after = await _anchors(characters, "c-a")
    assert after[0] is not None and after[0] >= before[0]


@pytest.mark.asyncio
async def test_advancing_a_beat_marks_the_cast_engaged(service, characters):
    drama = await service.create(
        character_ids=["c-a", "c-b"], prompt="測試", total_segments=2,
    )
    await asyncio.sleep(0.5)
    session, _, _ = await service.start_session(drama.id)

    await service.advance_session(session.id)

    assert all(
        stamp is not None
        for stamp in await _anchors(characters, "c-a", "c-b")
    )


@pytest.mark.asyncio
async def test_a_refused_press_does_not_mark_the_cast_engaged(
    service, characters,
):
    """The anchor records interactions that happened; a press that raised
    delivered nothing and was refunded."""
    with pytest.raises(ValueError):
        await service.start_session("no-such-drama")

    assert await _anchors(characters, "c-a", "c-b") == [None, None]


@pytest.mark.asyncio
async def test_a_service_without_an_anchor_still_plays(
    repo, char_a, char_b, planner, director,
):
    """Optional collaborator: rigs (and any deployment that has not wired it)
    keep the pre-NF4 path exactly."""
    service = BranchingDramaService(
        repository=repo,
        character_service=_CharServiceStub(
            by_id={char_a.id: char_a, char_b.id: char_b},
        ),
        brief_builder=_NullBriefBuilder(),
        planner=planner,
        director=director,
    )
    try:
        drama = await service.create(
            character_ids=["c-a", "c-b"], prompt="測試", total_segments=2,
        )
        await asyncio.sleep(0.5)
        session, _, narration = await service.start_session(drama.id)
        assert narration
        assert session.status == SESSION_PLAYING
    finally:
        tasks = list(service._tasks.values())  # noqa: SLF001
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
