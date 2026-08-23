"""BD2 — the player's declared place inside a branching drama.

Covers the whole vertical slice, because the slot is only worth anything
if it survives every hop: entity default and validation → persistence
round-trip (both repositories) → DTO defaults and echo → the three prompt
stages that actually change what gets written.

The prompt assertions deliberately test *semantics that must be present*
(a spectator drama must forbid addressing 「你」) rather than exact
sentences, so re-wording the guidance stays free while deleting it does
not.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from kokoro_link.application.dto.branching_drama import (
    BranchingDramaResponse,
    CreateBranchingDramaRequest,
)
from kokoro_link.application.services.branching_drama_critic import (
    _build_prompt as build_critic_prompt,
)
from kokoro_link.application.services.branching_drama_director import (
    _build_narrate_prompt,
    _build_scene_response_prompt,
)
from kokoro_link.application.services.branching_drama_planner import (
    _build_children_prompt,
    _build_root_prompt,
)
from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.domain.entities.branching_drama import (
    DEFAULT_OPERATOR_POSITION,
    OPERATOR_NOTE_MAX_CHARS,
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
    BranchingDrama,
    DramaNode,
    normalise_operator_note,
    normalise_operator_position,
)
from kokoro_link.infrastructure.persistence.branching_drama_models import (
    BranchingDramaNodeRow,
    BranchingDramaRow,
    BranchingDramaSessionRow,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.sa_branching_drama_repository import (
    SABranchingDramaRepository,
)
from kokoro_link.infrastructure.prompt.drama_operator_position_lines import (
    DRAMA_OPERATOR_POSITION_HEADER,
    STAGE_CRITIC,
    STAGE_DIRECTOR,
    STAGE_DIRECTOR_INTERACT,
    STAGE_PLANNER,
    render_drama_interact_framing,
    render_drama_operator_position_block,
    render_drama_operator_position_lines,
)
from kokoro_link.infrastructure.repositories.in_memory_branching_drama import (
    InMemoryBranchingDramaRepository,
)


BRIEFS = (
    CharacterBrief(
        character_id="c-a", name="A", summary="s", text="brief for A",
    ),
    CharacterBrief(
        character_id="c-b", name="B", summary="s", text="brief for B",
    ),
)

NODE = DramaNode.create_root(
    drama_id="d-1",
    title="序幕",
    summary="兩人在咖啡廳相遇。",
    appearing_character_ids=("c-a", "c-b"),
)


# ── entity ────────────────────────────────────────────────────────────


def test_create_pending_defaults_to_central() -> None:
    drama = BranchingDrama.create_pending(
        character_ids=["c-a", "c-b"], prompt="下雪的旅館",
    )

    assert drama.operator_position == OPERATOR_POSITION_CENTRAL
    assert drama.operator_position == DEFAULT_OPERATOR_POSITION
    assert drama.operator_note is None


@pytest.mark.parametrize(
    "position",
    [
        OPERATOR_POSITION_CENTRAL,
        OPERATOR_POSITION_PRESENT,
        OPERATOR_POSITION_ABSENT,
    ],
)
def test_create_pending_keeps_every_legal_position(position: str) -> None:
    drama = BranchingDrama.create_pending(
        character_ids=["c-a", "c-b"],
        prompt="下雪的旅館",
        operator_position=position,
        operator_note="我演她失聯多年的哥哥",
    )

    assert drama.operator_position == position
    assert drama.operator_note == "我演她失聯多年的哥哥"


def test_blank_position_falls_back_to_the_default_not_an_error() -> None:
    """A client sending an empty string means "didn't choose", which is
    the same thing as not sending the field at all."""
    assert normalise_operator_position("") == DEFAULT_OPERATOR_POSITION
    assert normalise_operator_position(None) == DEFAULT_OPERATOR_POSITION
    assert normalise_operator_position("  ABSENT ") == OPERATOR_POSITION_ABSENT


def test_off_vocabulary_position_is_rejected() -> None:
    with pytest.raises(ValueError):
        normalise_operator_position("narrator")
    with pytest.raises(ValueError):
        BranchingDrama.create_pending(
            character_ids=["c-a"], prompt="p", operator_position="narrator",
        )


def test_entity_rejects_an_off_vocabulary_position_on_construction() -> None:
    with pytest.raises(ValueError):
        BranchingDrama(
            id="d-1",
            character_ids=("c-a",),
            prompt="p",
            title="t",
            total_segments=3,
            status="ready",
            operator_position="protagonist",
        )


def test_note_is_coerced_not_rejected() -> None:
    assert normalise_operator_note("   ") is None
    assert normalise_operator_note(None) is None
    assert normalise_operator_note(42) is None
    assert normalise_operator_note("  我是她哥  ") == "我是她哥"
    long_note = "醫" * (OPERATOR_NOTE_MAX_CHARS + 40)
    assert len(normalise_operator_note(long_note)) == OPERATOR_NOTE_MAX_CHARS


# ── prompt block ──────────────────────────────────────────────────────


@pytest.mark.parametrize("stage", [STAGE_PLANNER, STAGE_DIRECTOR, STAGE_CRITIC])
def test_every_stage_gets_a_block_for_every_position(stage: str) -> None:
    for position in (
        OPERATOR_POSITION_CENTRAL,
        OPERATOR_POSITION_PRESENT,
        OPERATOR_POSITION_ABSENT,
    ):
        block = render_drama_operator_position_block(position, stage=stage)
        assert DRAMA_OPERATOR_POSITION_HEADER in block
        assert block.strip()


def test_absent_forbids_addressing_the_player_and_reframes_input() -> None:
    block = render_drama_operator_position_block(
        OPERATOR_POSITION_ABSENT, stage=STAGE_DIRECTOR,
    )

    assert "觀劇" in block
    assert "不要讓任何角色對玩家說話" in block
    # The whole point of 旁觀者: player text is a note to the director,
    # not a line spoken inside the scene.
    assert "導演指示" in block


def test_central_and_present_differ_on_who_the_story_is_about() -> None:
    central = render_drama_operator_position_block(
        OPERATOR_POSITION_CENTRAL, stage=STAGE_DIRECTOR,
    )
    present = render_drama_operator_position_block(
        OPERATOR_POSITION_PRESENT, stage=STAGE_DIRECTOR,
    )

    assert central != present
    assert "主演" in central
    assert "不是關於他的" in present


def test_note_rides_the_block_with_a_do_not_recite_rail() -> None:
    block = render_drama_operator_position_block(
        OPERATOR_POSITION_PRESENT,
        "我演她失聯多年的哥哥",
        stage=STAGE_DIRECTOR,
    )

    assert "我演她失聯多年的哥哥" in block
    assert "不要照稿念出" in block


def test_note_is_clipped_to_the_entity_ceiling() -> None:
    block = render_drama_operator_position_block(
        OPERATOR_POSITION_CENTRAL, "字" * 400, stage=STAGE_DIRECTOR,
    )

    assert "字" * OPERATOR_NOTE_MAX_CHARS in block
    assert "字" * (OPERATOR_NOTE_MAX_CHARS + 1) not in block


def test_blank_note_adds_nothing() -> None:
    with_blank = render_drama_operator_position_block(
        OPERATOR_POSITION_CENTRAL, "   ", stage=STAGE_DIRECTOR,
    )
    without = render_drama_operator_position_block(
        OPERATOR_POSITION_CENTRAL, stage=STAGE_DIRECTOR,
    )

    assert with_blank == without


def test_unknown_position_falls_back_to_the_default_framing() -> None:
    """Prompt assembly is on the gameplay path — a junk value must still
    produce the framing every pre-BD2 drama played under."""
    assert render_drama_operator_position_block(
        "narrator", stage=STAGE_DIRECTOR,
    ) == render_drama_operator_position_block(
        DEFAULT_OPERATOR_POSITION, stage=STAGE_DIRECTOR,
    )


def test_list_form_keeps_a_leading_separator_and_block_form_drops_it() -> None:
    lines = render_drama_operator_position_lines(
        OPERATOR_POSITION_CENTRAL, stage=STAGE_DIRECTOR,
    )

    assert lines[0] == ""
    assert not render_drama_operator_position_block(
        OPERATOR_POSITION_CENTRAL, stage=STAGE_DIRECTOR,
    ).startswith("\n")


# ── prompt stages ─────────────────────────────────────────────────────


def _planner_root(position: str, note: str | None = None) -> str:
    return _build_root_prompt(
        prompt="下雪的旅館",
        briefs=BRIEFS,
        total_segments=6,
        operator_position=position,
        operator_note=note,
    )


def test_planner_root_prompt_carries_the_position() -> None:
    prompt = _planner_root(OPERATOR_POSITION_ABSENT, "我只是看戲")

    assert DRAMA_OPERATOR_POSITION_HEADER in prompt
    assert "我只是看戲" in prompt


def test_planner_root_prompt_defaults_without_the_kwarg() -> None:
    """A caller that never heard of BD2 still gets a well-formed prompt —
    framed the way dramas were always framed."""
    prompt = _build_root_prompt(
        prompt="下雪的旅館", briefs=BRIEFS, total_segments=6,
    )

    assert DRAMA_OPERATOR_POSITION_HEADER in prompt
    assert "主演" in prompt


def test_planner_children_prompt_carries_the_position() -> None:
    prompt = _build_children_prompt(
        prompt="下雪的旅館",
        briefs=BRIEFS,
        parent_summary="上一段",
        path_context="",
        depth=1,
        total_segments=6,
        is_ending=False,
        operator_position=OPERATOR_POSITION_PRESENT,
    )

    assert DRAMA_OPERATOR_POSITION_HEADER in prompt


@pytest.mark.parametrize(
    "builder",
    [
        lambda: _planner_root(OPERATOR_POSITION_ABSENT),
        lambda: _build_children_prompt(
            prompt="p",
            briefs=BRIEFS,
            parent_summary="上一段",
            path_context="",
            depth=1,
            total_segments=6,
            is_ending=False,
            operator_position=OPERATOR_POSITION_ABSENT,
        ),
    ],
)
def test_planner_block_sits_before_the_output_rules(builder) -> None:  # noqa: ANN001
    """The shipped templates end with 輸出規則, and the parser depends on
    that JSON contract being the last word."""
    prompt = builder()

    assert prompt.index(DRAMA_OPERATOR_POSITION_HEADER) < prompt.index(
        "輸出規則",
    )


def test_director_narrate_prompt_carries_the_position() -> None:
    prompt = _build_narrate_prompt(
        node=NODE,
        briefs=BRIEFS,
        previous_turns=(),
        player_input="",
        operator_position=OPERATOR_POSITION_ABSENT,
        operator_note="我只是看戲",
    )

    assert DRAMA_OPERATOR_POSITION_HEADER in prompt
    assert "我只是看戲" in prompt
    # 要求 stays the closing section — the framing is context it is
    # written against, not a competing bullet after it.
    assert prompt.index(DRAMA_OPERATOR_POSITION_HEADER) < prompt.index("要求：")


def _scene_response(position: str) -> str:
    return _build_scene_response_prompt(
        node=NODE,
        briefs=BRIEFS,
        previous_turns=(),
        exchanges=(),
        player_input="我推開門",
        operator_position=position,
    )


def test_director_scene_response_prompt_carries_the_position() -> None:
    prompt = _scene_response(OPERATOR_POSITION_ABSENT)

    assert DRAMA_OPERATOR_POSITION_HEADER in prompt
    assert "導演指示" in prompt


def test_scene_response_skeleton_stops_contradicting_the_spectator() -> None:
    """FX3 — the fixed lines of this prompt used to be protagonist-only.

    ``interact`` has no critic pass, so a prompt that says 「以角色身分自然
    回應玩家」 above the block forbidding anyone to address the player is a
    coin flip nothing downstream corrects. The skeleton itself has to know
    where the player stands.
    """
    absent = _scene_response(OPERATOR_POSITION_ABSENT)
    central = _scene_response(OPERATOR_POSITION_CENTRAL)

    # The player's text is labelled for what it is in each framing…
    assert "玩家現在的行動/台詞" in central
    assert "玩家現在的行動/台詞" not in absent
    assert "導演指示" in absent
    # …and the deliverable is a reply in one and a scene in the other.
    assert "以角色身分自然回應玩家" in central
    assert "以角色身分自然回應玩家" not in absent
    assert "第三人稱" in absent


def test_scene_response_position_block_sits_after_the_fixed_instructions() -> None:
    """The block is the tie-breaker here, so it has to be read last.

    Narration puts it *above* 要求 (it is context the requirements are
    written against). The response prompt cannot: its 要求 and its
    transcript label are themselves framing, so the block closes them
    rather than opening them.
    """
    prompt = _scene_response(OPERATOR_POSITION_ABSENT)

    header_at = prompt.index(DRAMA_OPERATOR_POSITION_HEADER)
    assert header_at > prompt.index("要求：")
    assert header_at > prompt.index("我推開門")
    # …but still before the output contract the parser depends on.
    assert header_at < prompt.index("你的回覆必須是以下 JSON 格式")


def test_interact_framing_falls_back_rather_than_raising() -> None:
    """Gameplay path: an unknown value takes the framing every pre-BD2
    drama already played under, it does not 500 the beat."""
    assert render_drama_interact_framing("narrator") == (
        render_drama_interact_framing(OPERATOR_POSITION_CENTRAL)
    )
    assert render_drama_interact_framing(None) == (
        render_drama_interact_framing(DEFAULT_OPERATOR_POSITION)
    )


def test_interact_stage_has_its_own_rider() -> None:
    """Sharing the narrate stage's rider would put instructions about a
    reply into a prompt that never writes one."""
    interact = render_drama_operator_position_lines(
        OPERATOR_POSITION_ABSENT, stage=STAGE_DIRECTOR_INTERACT,
    )
    narrate = render_drama_operator_position_lines(
        OPERATOR_POSITION_ABSENT, stage=STAGE_DIRECTOR,
    )

    assert interact != narrate
    assert DRAMA_OPERATOR_POSITION_HEADER in interact


def test_critic_prompt_carries_the_position_before_the_output_rules() -> None:
    prompt = build_critic_prompt(
        node=NODE,
        paragraphs=["一段。", "兩段。"],
        briefs=BRIEFS,
        previous_turns=(),
        operator_position=OPERATOR_POSITION_ABSENT,
    )

    assert DRAMA_OPERATOR_POSITION_HEADER in prompt
    assert prompt.index(DRAMA_OPERATOR_POSITION_HEADER) < prompt.index(
        "輸出規則",
    )


def test_critic_flips_what_counts_as_a_defect() -> None:
    absent = build_critic_prompt(
        node=NODE,
        paragraphs=["一段。"],
        briefs=BRIEFS,
        previous_turns=(),
        operator_position=OPERATOR_POSITION_ABSENT,
    )
    central = build_critic_prompt(
        node=NODE,
        paragraphs=["一段。"],
        briefs=BRIEFS,
        previous_turns=(),
        operator_position=OPERATOR_POSITION_CENTRAL,
    )

    assert absent != central
    assert "是缺陷，必須指出來" in absent


# ── DTO ───────────────────────────────────────────────────────────────


def test_create_request_defaults_keep_an_old_client_working() -> None:
    payload = CreateBranchingDramaRequest(
        character_ids=["c-a", "c-b"], prompt="下雪的旅館",
    )

    assert payload.operator_position == OPERATOR_POSITION_CENTRAL
    assert payload.operator_note is None


def test_create_request_normalises_and_rejects() -> None:
    payload = CreateBranchingDramaRequest(
        character_ids=["c-a", "c-b"],
        prompt="p",
        operator_position=" Absent ",
    )
    assert payload.operator_position == OPERATOR_POSITION_ABSENT

    with pytest.raises(ValueError):
        CreateBranchingDramaRequest(
            character_ids=["c-a", "c-b"],
            prompt="p",
            operator_position="narrator",
        )


def test_response_echoes_the_slot() -> None:
    drama = BranchingDrama.create_pending(
        character_ids=["c-a", "c-b"],
        prompt="p",
        operator_position=OPERATOR_POSITION_PRESENT,
        operator_note="我陪著她",
    )

    body = BranchingDramaResponse.from_domain(drama)

    assert body.operator_position == OPERATOR_POSITION_PRESENT
    assert body.operator_note == "我陪著她"


# ── persistence ───────────────────────────────────────────────────────


@pytest_asyncio.fixture()
async def sa_repo():  # noqa: ANN201
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        for row in (
            BranchingDramaRow,
            BranchingDramaNodeRow,
            BranchingDramaSessionRow,
        ):
            await conn.run_sync(lambda sync, r=row: r.__table__.create(sync))
    factory = build_session_factory(engine)
    try:
        yield SABranchingDramaRepository(factory), factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sa_repository_round_trips_every_position(sa_repo) -> None:  # noqa: ANN001
    repo, _factory = sa_repo
    for position in (
        OPERATOR_POSITION_CENTRAL,
        OPERATOR_POSITION_PRESENT,
        OPERATOR_POSITION_ABSENT,
    ):
        drama = BranchingDrama.create_pending(
            character_ids=["c-a", "c-b"],
            prompt="下雪的旅館",
            operator_position=position,
            operator_note=f"note-{position}",
        )
        await repo.add(drama)

        stored = await repo.get(drama.id)
        assert stored is not None
        assert stored.operator_position == position
        assert stored.operator_note == f"note-{position}"


@pytest.mark.asyncio
async def test_sa_repository_save_does_not_lose_the_slot(sa_repo) -> None:  # noqa: ANN001
    repo, _factory = sa_repo
    drama = BranchingDrama.create_pending(
        character_ids=["c-a", "c-b"],
        prompt="p",
        operator_position=OPERATOR_POSITION_ABSENT,
        operator_note="我只是看戲",
    )
    await repo.add(drama)

    await repo.save((await repo.get(drama.id)).with_title("有名字了"))

    stored = await repo.get(drama.id)
    assert stored.title == "有名字了"
    assert stored.operator_position == OPERATOR_POSITION_ABSENT
    assert stored.operator_note == "我只是看戲"


@pytest.mark.asyncio
async def test_pre_bd2_row_reads_back_as_the_default(sa_repo) -> None:  # noqa: ANN001
    """The migration deliberately backfills nothing, so the mapper is the
    only thing standing between a legacy ``NULL`` and an entity that
    refuses to construct."""
    repo, factory = sa_repo
    now = datetime.now(timezone.utc)
    async with factory() as session:
        row = BranchingDramaRow()
        row.id = "legacy-1"
        row.character_ids_json = '["c-a", "c-b"]'
        row.prompt = "舊的劇場"
        row.title = "舊標題"
        row.total_segments = 6
        row.status = "ready"
        row.operator_position = None
        row.operator_note = None
        row.created_at = now
        row.updated_at = now
        session.add(row)
        await session.commit()

    stored = await repo.get("legacy-1")
    assert stored is not None
    assert stored.operator_position == DEFAULT_OPERATOR_POSITION
    assert stored.operator_note is None


@pytest.mark.asyncio
async def test_in_memory_repository_round_trips_the_slot() -> None:
    repo = InMemoryBranchingDramaRepository()
    drama = BranchingDrama.create_pending(
        character_ids=["c-a", "c-b"],
        prompt="p",
        operator_position=OPERATOR_POSITION_PRESENT,
        operator_note="我陪著她",
    )
    await repo.add(drama)

    stored = await repo.get(drama.id)
    assert stored.operator_position == OPERATOR_POSITION_PRESENT
    assert stored.operator_note == "我陪著她"


# ── service wiring ────────────────────────────────────────────────────


@dataclass
class _RecordingPlanner:
    """Captures what framing the outline planner was actually called with."""

    root_kwargs: dict[str, Any] | None = None
    children_kwargs: list[dict[str, Any]] | None = None

    async def plan_root(self, **kwargs: Any):  # noqa: ANN201
        from kokoro_link.application.services.branching_drama_planner import (
            NodeOutline,
        )

        self.root_kwargs = kwargs
        return "測試劇場", NodeOutline(
            title="序幕",
            summary="相遇。",
            appearing_character_ids=tuple(
                b.character_id for b in kwargs["briefs"]
            ),
        )

    async def plan_children(self, **kwargs: Any):  # noqa: ANN201
        from kokoro_link.application.services.branching_drama_planner import (
            NodeOutline,
        )
        from kokoro_link.domain.entities.branching_drama import (
            TONE_DARK,
            TONE_NEUTRAL,
            TONE_SUNNY,
        )

        if self.children_kwargs is None:
            self.children_kwargs = []
        self.children_kwargs.append(kwargs)
        ids = tuple(b.character_id for b in kwargs["briefs"])
        return {
            tone: NodeOutline(
                title=f"{tone}-{kwargs['depth']}",
                summary=f"{tone} 方向。",
                appearing_character_ids=ids,
            )
            for tone in (TONE_DARK, TONE_SUNNY, TONE_NEUTRAL)
        }


@pytest.mark.asyncio
async def test_create_persists_the_slot_and_hands_it_to_the_planner() -> None:
    import asyncio

    from kokoro_link.application.services.branching_drama_service import (
        BranchingDramaService,
    )
    from kokoro_link.domain.entities.character import Character
    from kokoro_link.domain.value_objects.character_state import CharacterState

    def _character(letter: str) -> Character:
        char = Character.create(
            name=f"Char-{letter}",
            summary="s",
            personality=["calm"],
            interests=["coffee"],
            speaking_style="quiet",
            boundaries=[],
            appearance="short dark hair",
            gender_identity="非二元",
            third_person_pronoun="TA",
            visual_gender_presentation="androgynous",
            state=CharacterState(
                emotion="平靜", affection=50, fatigue=0, trust=50, energy=100,
            ),
        )
        object.__setattr__(char, "id", f"c-{letter}")
        return char

    class _CharServiceStub:
        def __init__(self, by_id: dict[str, Character]) -> None:
            self._by_id = by_id

        async def get_character_entity(self, character_id: str):  # noqa: ANN201
            return self._by_id.get(character_id)

    class _NullBriefBuilder:
        def build_persona_only_many(self, characters):  # noqa: ANN001, ANN201
            return [
                CharacterBrief(
                    character_id=c.id, name=c.name, summary="", text="brief",
                )
                for c in characters
            ]

    class _StubDirector:
        async def narrate(self, **_kwargs: Any) -> str:
            return "敘事"

        async def respond_in_scene(self, **_kwargs: Any):  # noqa: ANN201
            return "回應", None

        async def classify_tone(self, **_kwargs: Any) -> str:
            from kokoro_link.domain.entities.branching_drama import (
                TONE_NEUTRAL,
            )

            return TONE_NEUTRAL

    char_a, char_b = _character("a"), _character("b")
    repo = InMemoryBranchingDramaRepository()
    planner = _RecordingPlanner()
    service = BranchingDramaService(
        repository=repo,
        character_service=_CharServiceStub(
            {char_a.id: char_a, char_b.id: char_b},
        ),
        brief_builder=_NullBriefBuilder(),
        planner=planner,
        director=_StubDirector(),
    )

    drama = await service.create(
        character_ids=[char_a.id, char_b.id],
        prompt="下雪的旅館",
        total_segments=3,
        operator_position=OPERATOR_POSITION_ABSENT,
        operator_note="我只是看戲",
    )

    for _ in range(200):
        stored = await repo.get(drama.id)
        if stored is not None and stored.is_terminal():
            break
        await asyncio.sleep(0.01)

    stored = await repo.get(drama.id)
    assert stored.operator_position == OPERATOR_POSITION_ABSENT
    assert stored.operator_note == "我只是看戲"

    assert planner.root_kwargs is not None
    assert planner.root_kwargs["operator_position"] == OPERATOR_POSITION_ABSENT
    assert planner.root_kwargs["operator_note"] == "我只是看戲"
    assert planner.children_kwargs
    assert all(
        call["operator_position"] == OPERATOR_POSITION_ABSENT
        for call in planner.children_kwargs
    )


# ── migration ─────────────────────────────────────────────────────────


MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "q7e3y0b10042_branching_drama_operator_position.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_branching_drama_operator_position_migration_module",
        MIGRATION_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    def __init__(self) -> None:
        self.added_columns: list[tuple[str, Any]] = []
        self.dropped_columns: list[tuple[str, str]] = []

    def add_column(self, table_name: str, column: Any) -> None:
        self.added_columns.append((table_name, column))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append((table_name, column_name))


def test_migration_chains_onto_the_previous_head() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    migration = _load_migration()

    assert migration.revision == "q7e3y0b10042"
    assert migration.down_revision == "p6d2x9a10041"

    repo_root = Path(__file__).parents[2]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = list(script.get_heads())
    # One head, and this revision on the way to it. Asserting the head *is*
    # this revision would have been a tripwire on every later migration
    # rather than on the branch this test cares about — the property under
    # test is that the graph stayed linear, not that BD2 is still last.
    assert len(heads) == 1, "the migration graph must stay linear"
    chain = {
        rev.revision
        for rev in script.iterate_revisions(heads[0], "base")
    }
    assert "q7e3y0b10042" in chain


def test_migration_adds_two_nullable_columns_without_backfill(
    monkeypatch,  # noqa: ANN001
) -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert [table for table, _ in recorder.added_columns] == [
        "branching_dramas",
        "branching_dramas",
    ]
    position, note = (column for _table, column in recorder.added_columns)
    assert position.name == "operator_position"
    assert position.type.length == 16
    assert position.nullable is True
    # No server default: a stamped value would be a destructive write
    # saying the same thing the mapper already says for free.
    assert position.server_default is None
    assert note.name == "operator_note"
    assert note.nullable is True
    assert note.server_default is None


def test_migration_downgrade_drops_both(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_columns == [
        ("branching_dramas", "operator_note"),
        ("branching_dramas", "operator_position"),
    ]
