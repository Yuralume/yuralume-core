"""BD10 — a drama says out loud which look it is drawn in.

Before this slot the answer was never stated, only inferred: the scene
prompt styled itself off ``characters[0]``. Two consequences nobody chose
followed from that one line — a cast mixing an anime character with a
realistic one drew the second in the first one's look, and reordering the
cast picker repainted the entire production.

So the vertical slice under test is "the implicit thing became an explicit
one, without changing what already exists":

* the **entity** carries a closed-vocabulary slot that is strict on write;
* the **column** is nullable and never backfilled, because ``NULL`` here
  means something ("written before the column existed") that no default
  can say;
* **create** resolves an unstated look from the first character and
  *stores* it, so the drama made by a pre-BD10 client is still a drama with
  a look written on it;
* the **scene prompt** reads the row for every picture — first draw, lazy
  layer and manual redraw alike — and only falls back to the old
  per-character resolution for a row that really is ``NULL``.
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
from kokoro_link.application.services.branching_drama_service import (
    BranchingDramaService,
    _PipelineContext,
)
from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.application.services.visual_generation_style import (
    VisualGenerationStyleService,
    visual_generation_style_prompt,
)
from kokoro_link.domain.entities.branching_drama import (
    BranchingDrama,
    DramaNode,
    normalise_drama_visual_style,
    read_stored_visual_style,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.branching_drama_models import (
    BranchingDramaNodeRow,
    BranchingDramaRow,
    BranchingDramaSessionRow,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.sa_branching_drama_repository import (
    SABranchingDramaRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_branching_drama import (
    InMemoryBranchingDramaRepository,
)

_ANIME_PROMPT = visual_generation_style_prompt("anime")
_REALISTIC_PROMPT = visual_generation_style_prompt("realistic")


def _character(letter: str, style: str = "") -> Character:
    char = Character.create(
        name=f"Char-{letter}",
        summary="s",
        personality=["calm"],
        interests=["coffee"],
        speaking_style="quiet",
        boundaries=[],
        appearance=f"appearance of {letter}",
        visual_generation_style=style,
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    object.__setattr__(char, "id", f"c-{letter}")
    return char


NODE = DramaNode.create_root(
    drama_id="d-1",
    title="序幕",
    summary="兩人在咖啡廳相遇。",
    appearing_character_ids=("c-a", "c-b"),
)


# ── entity ────────────────────────────────────────────────────────────


def test_an_unstated_look_stays_unstated_on_the_entity() -> None:
    """``None`` must not collapse into ``anime`` down here.

    The create *service* is what turns "unstated" into a stored look; if
    the entity guessed instead, a pre-BD10 row read back through this same
    constructor would claim a look it was never drawn in.
    """
    drama = BranchingDrama.create_pending(
        character_ids=["c-a", "c-b"], prompt="下雪的旅館",
    )
    assert drama.visual_style is None


@pytest.mark.parametrize("style", ["anime", "realistic"])
def test_create_pending_keeps_every_legal_look(style: str) -> None:
    drama = BranchingDrama.create_pending(
        character_ids=["c-a", "c-b"], prompt="p", visual_style=style,
    )
    assert drama.visual_style == style


def test_casing_is_canonicalised_and_blank_means_unstated() -> None:
    assert normalise_drama_visual_style("  Realistic ") == "realistic"
    assert normalise_drama_visual_style("") is None
    assert normalise_drama_visual_style("   ") is None
    assert normalise_drama_visual_style(None) is None


def test_off_vocabulary_look_is_rejected_at_the_write_boundary() -> None:
    """A typo must fail loudly, not draw the drama in a look nobody picked."""
    with pytest.raises(ValueError):
        normalise_drama_visual_style("watercolour")
    with pytest.raises(ValueError):
        BranchingDrama.create_pending(
            character_ids=["c-a", "c-b"], prompt="p",
            visual_style="watercolour",
        )
    with pytest.raises(ValueError):
        normalise_drama_visual_style(3)


def test_entity_rejects_an_off_vocabulary_look_on_construction() -> None:
    with pytest.raises(ValueError):
        BranchingDrama(
            id="d-1",
            character_ids=("c-a",),
            prompt="p",
            title="t",
            total_segments=6,
            status="ready",
            visual_style="watercolour",
        )


def test_stored_reader_degrades_instead_of_raising() -> None:
    """The read path may never 500 the drama list over one odd row.

    Same lesson as the ``total_segments`` ceiling: this constructor is also
    the row mapper, and a value it refuses takes down the list *and* the
    delete endpoint that is the only way to remove the row.
    """
    assert read_stored_visual_style("ANIME") == "anime"
    assert read_stored_visual_style("watercolour") is None
    assert read_stored_visual_style(None) is None
    assert read_stored_visual_style("") is None


# ── DTO ───────────────────────────────────────────────────────────────


def test_create_request_defaults_keep_an_old_client_working() -> None:
    req = CreateBranchingDramaRequest(
        character_ids=["c-a", "c-b"], prompt="p",
    )
    assert req.visual_style is None


def test_create_request_normalises_and_rejects() -> None:
    assert CreateBranchingDramaRequest(
        character_ids=["c-a", "c-b"], prompt="p", visual_style="ANIME",
    ).visual_style == "anime"
    assert CreateBranchingDramaRequest(
        character_ids=["c-a", "c-b"], prompt="p", visual_style="  ",
    ).visual_style is None
    with pytest.raises(ValueError):
        CreateBranchingDramaRequest(
            character_ids=["c-a", "c-b"], prompt="p",
            visual_style="watercolour",
        )


def test_response_echoes_the_look_including_the_legacy_null() -> None:
    drama = BranchingDrama.create_pending(
        character_ids=["c-a", "c-b"], prompt="p", visual_style="realistic",
    )
    assert BranchingDramaResponse.from_domain(drama).visual_style == (
        "realistic"
    )

    legacy = BranchingDrama.create_pending(
        character_ids=["c-a", "c-b"], prompt="p",
    )
    # Not "anime": naming a look this drama does not render would be worse
    # than saying nothing.
    assert BranchingDramaResponse.from_domain(legacy).visual_style is None


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
@pytest.mark.parametrize("style", ["anime", "realistic"])
async def test_sa_repository_round_trips_the_look(sa_repo, style) -> None:  # noqa: ANN001
    repo, _factory = sa_repo
    drama = BranchingDrama.create_pending(
        character_ids=["c-a", "c-b"], prompt="下雪的旅館",
        visual_style=style,
    )
    await repo.add(drama)

    stored = await repo.get(drama.id)
    assert stored is not None
    assert stored.visual_style == style


@pytest.mark.asyncio
async def test_sa_repository_save_does_not_lose_the_look(sa_repo) -> None:  # noqa: ANN001
    """The title lands minutes after creation — the look must survive it."""
    repo, _factory = sa_repo
    drama = BranchingDrama.create_pending(
        character_ids=["c-a", "c-b"], prompt="p", visual_style="realistic",
    )
    await repo.add(drama)

    await repo.save((await repo.get(drama.id)).with_title("有名字了"))

    stored = await repo.get(drama.id)
    assert stored.title == "有名字了"
    assert stored.visual_style == "realistic"


@pytest.mark.asyncio
async def test_pre_bd10_row_reads_back_as_unstated(sa_repo) -> None:  # noqa: ANN001
    """The migration backfills nothing and the mapper substitutes nothing.

    A default here would repaint every existing drama whose first character
    was realistic — silently, and only in the pictures.
    """
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
        row.visual_style = None
        row.created_at = now
        row.updated_at = now
        session.add(row)
        await session.commit()

    stored = await repo.get("legacy-1")
    assert stored is not None
    assert stored.visual_style is None


@pytest.mark.asyncio
async def test_an_unreadable_stored_look_does_not_break_the_list(  # noqa: ANN201
    sa_repo,  # noqa: ANN001
) -> None:
    repo, factory = sa_repo
    now = datetime.now(timezone.utc)
    async with factory() as session:
        row = BranchingDramaRow()
        row.id = "odd-1"
        row.character_ids_json = '["c-a"]'
        row.prompt = "p"
        row.title = "t"
        row.total_segments = 6
        row.status = "ready"
        row.visual_style = "watercolour"
        row.created_at = now
        row.updated_at = now
        session.add(row)
        await session.commit()

    listed = await repo.list_recent()
    assert [d.id for d in listed] == ["odd-1"]
    assert listed[0].visual_style is None


@pytest.mark.asyncio
async def test_in_memory_repository_round_trips_the_look() -> None:
    repo = InMemoryBranchingDramaRepository()
    drama = BranchingDrama.create_pending(
        character_ids=["c-a", "c-b"], prompt="p", visual_style="realistic",
    )
    await repo.add(drama)

    stored = await repo.get(drama.id)
    assert stored.visual_style == "realistic"


# ── scene prompt ──────────────────────────────────────────────────────


@dataclass
class _CharServiceStub:
    by_id: dict[str, Character]

    async def get_character_entity(self, character_id: str):  # noqa: ANN201
        return self.by_id.get(character_id)


class _NullBriefBuilder:
    def build_persona_only_many(self, characters):  # noqa: ANN001, ANN201
        return [
            CharacterBrief(
                character_id=c.id, name=c.name, summary="s",
                text=f"brief for {c.name}",
            )
            for c in characters
        ]


class _StubPreferences:
    """Just enough of the preferences port for the legacy fallback path."""

    def __init__(self, style: str | None = None) -> None:
        self._style = style

    async def get(self, key: str, *, user_id: str | None = None):  # noqa: ANN201, ARG002
        return {"style": self._style} if self._style else None

    async def set(self, key, value, *, user_id=None) -> None:  # noqa: ANN001, ANN201, ARG002
        return None


def _service(
    *,
    characters: list[Character],
    visual_style_service: VisualGenerationStyleService | None = None,
) -> BranchingDramaService:
    return BranchingDramaService(
        repository=InMemoryBranchingDramaRepository(),
        character_service=_CharServiceStub(
            by_id={c.id: c for c in characters},
        ),
        brief_builder=_NullBriefBuilder(),
        planner=object(),
        director=object(),
        visual_style_service=visual_style_service,
        image_prefetch_depth=0,
    )


def _ctx(
    characters: list[Character], visual_style: str | None,
) -> _PipelineContext:
    return _PipelineContext(
        drama_id="d-1",
        prompt="p",
        total_segments=3,
        characters=characters,
        briefs=_NullBriefBuilder().build_persona_only_many(characters),
        visual_style=visual_style,
    )


@pytest.mark.asyncio
async def test_stored_look_wins_over_the_first_character() -> None:
    """The regression this whole ticket exists for.

    An anime lead and a realistic co-star used to mean the co-star got
    drawn as anime. With the look on the drama, the cast order stops
    deciding anything.
    """
    characters = [_character("a", "anime"), _character("b", "realistic")]
    service = _service(characters=characters)

    prompt = await service._build_scene_prompt(  # noqa: SLF001
        NODE, _ctx(characters, "realistic"),
    )

    assert prompt.endswith(_REALISTIC_PROMPT)
    assert _ANIME_PROMPT not in prompt


@pytest.mark.asyncio
async def test_stored_look_does_not_consult_the_style_service_at_all() -> None:
    """Not merely "the right answer" — the per-character lookup is gone.

    A style service that still got asked would be a second source of truth
    waiting to disagree with the row on some later path.
    """
    characters = [_character("a", "anime")]
    asked: list[Any] = []

    class _ShoutingStyleService(VisualGenerationStyleService):
        async def styled_prompt(self, positive, **kwargs):  # noqa: ANN001, ANN201
            asked.append(kwargs)
            raise AssertionError("the drama already stated its look")

        async def get_style_for_character(self, character, **kwargs):  # noqa: ANN001, ANN201
            asked.append(kwargs)
            raise AssertionError("the drama already stated its look")

    service = _service(
        characters=characters,
        visual_style_service=_ShoutingStyleService(
            preferences=_StubPreferences(),
        ),
    )

    prompt = await service._build_scene_prompt(  # noqa: SLF001
        NODE, _ctx(characters, "realistic"),
    )

    assert asked == []
    assert prompt.endswith(_REALISTIC_PROMPT)


@pytest.mark.asyncio
async def test_null_look_falls_back_to_the_first_character() -> None:
    """A pre-BD10 drama keeps drawing exactly as it always did."""
    characters = [_character("a", "realistic"), _character("b", "anime")]
    service = _service(
        characters=characters,
        visual_style_service=VisualGenerationStyleService(
            preferences=_StubPreferences(),
        ),
    )

    prompt = await service._build_scene_prompt(  # noqa: SLF001
        NODE, _ctx(characters, None),
    )

    assert prompt.endswith(_REALISTIC_PROMPT)


@pytest.mark.asyncio
async def test_null_look_without_a_style_service_keeps_the_default() -> None:
    characters = [_character("a", "realistic")]
    service = _service(characters=characters)

    prompt = await service._build_scene_prompt(  # noqa: SLF001
        NODE, _ctx(characters, None),
    )

    # No preference store wired at all — the historical branch, unchanged.
    assert prompt.endswith(_ANIME_PROMPT)


@pytest.mark.asyncio
async def test_the_scene_description_still_precedes_the_style_block() -> None:
    characters = [_character("a", "anime"), _character("b", "anime")]
    service = _service(characters=characters)

    prompt = await service._build_scene_prompt(  # noqa: SLF001
        NODE, _ctx(characters, "anime"),
    )

    assert prompt.startswith(NODE.summary)
    assert "appearance of a" in prompt
    assert "visual novel scene, cinematic lighting" in prompt


# ── create: the implicit answer becomes a stored one ──────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["anime", "realistic"])
async def test_an_unstated_look_is_resolved_and_stored_at_create(  # noqa: ANN201
    style: str,
) -> None:
    """The fusion 「換個玩法」 handoff and every pre-BD10 client land here.

    They send nothing, and what they get is not a ``NULL`` row that keeps
    re-deriving its look forever — it is the look they would have been
    drawn in, written down once.
    """
    characters = [_character("a", style), _character("b", "anime")]
    service = _service(
        characters=characters,
        visual_style_service=VisualGenerationStyleService(
            preferences=_StubPreferences(),
        ),
    )

    resolved = await service._resolve_visual_style(  # noqa: SLF001
        None, characters=characters, user_id="u-1",
    )

    assert resolved == style


@pytest.mark.asyncio
async def test_a_stated_look_is_not_second_guessed() -> None:
    characters = [_character("a", "anime")]
    service = _service(
        characters=characters,
        visual_style_service=VisualGenerationStyleService(
            preferences=_StubPreferences(),
        ),
    )

    resolved = await service._resolve_visual_style(  # noqa: SLF001
        "realistic", characters=characters, user_id="u-1",
    )

    assert resolved == "realistic"


@pytest.mark.asyncio
async def test_an_inheriting_first_character_resolves_the_owner_preference(
) -> None:
    """Character-level blank means "inherit" — it is not a third style."""
    characters = [_character("a", "")]
    service = _service(
        characters=characters,
        visual_style_service=VisualGenerationStyleService(
            preferences=_StubPreferences("realistic"),
        ),
    )

    resolved = await service._resolve_visual_style(  # noqa: SLF001
        None, characters=characters, user_id="u-1",
    )

    assert resolved == "realistic"


@pytest.mark.asyncio
async def test_resolution_always_answers_even_with_nothing_to_go_on() -> None:
    """A drama created today may never store the legacy ``NULL``."""
    service = _service(characters=[])

    assert await service._resolve_visual_style(  # noqa: SLF001
        None, characters=[], user_id=None,
    ) == "anime"

    characters = [_character("a", "")]
    assert await _service(characters=characters)._resolve_visual_style(  # noqa: SLF001
        None, characters=characters, user_id=None,
    ) == "anime"


@pytest.mark.asyncio
async def test_create_writes_the_resolved_look_onto_the_row() -> None:
    """End to end through the real create path, not just the resolver."""
    characters = [_character("a", "realistic"), _character("b", "anime")]
    service = _service(
        characters=characters,
        visual_style_service=VisualGenerationStyleService(
            preferences=_StubPreferences(),
        ),
    )
    # The pipeline itself is out of scope here — the planner is a bare
    # object() — so stop after the row is written.
    spawned: list[Any] = []

    async def _capture(**kwargs: Any) -> None:
        runner = kwargs.get("runner")
        if runner is not None:
            runner.close()
        spawned.append(kwargs)

    service._track_and_spawn = _capture  # type: ignore[assignment]  # noqa: SLF001

    drama = await service.create(
        character_ids=["c-a", "c-b"], prompt="下雪的旅館", total_segments=3,
    )

    assert drama.visual_style == "realistic"
    stored = await service._repo.get(drama.id)  # noqa: SLF001
    assert stored is not None and stored.visual_style == "realistic"
    assert spawned, "the generation pipeline should still have been spawned"


# ── migration ─────────────────────────────────────────────────────────


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "alembic" / "versions"
        / "r8f4z1c10043_branching_drama_visual_style.py"
    )
    spec = importlib.util.spec_from_file_location("_bd10_migration", path)
    assert spec is not None and spec.loader is not None
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

    assert migration.revision == "r8f4z1c10043"
    assert migration.down_revision == "q7e3y0b10042"

    repo_root = Path(__file__).parents[2]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = list(script.get_heads())
    assert len(heads) == 1, "the migration graph must stay linear"
    chain = {
        rev.revision for rev in script.iterate_revisions(heads[0], "base")
    }
    assert "r8f4z1c10043" in chain


def test_migration_adds_one_nullable_column_without_backfill(
    monkeypatch,  # noqa: ANN001
) -> None:
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.upgrade()

    assert [table for table, _ in recorder.added_columns] == [
        "branching_dramas",
    ]
    (column,) = (col for _table, col in recorder.added_columns)
    assert column.name == "visual_style"
    assert column.type.length == 16
    assert column.nullable is True
    # No server default and no UPDATE: stamping a look into existing rows
    # would repaint every drama whose first character was realistic.
    assert column.server_default is None


def test_migration_downgrade_drops_the_column(monkeypatch) -> None:  # noqa: ANN001
    migration = _load_migration()
    recorder = _RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)

    migration.downgrade()

    assert recorder.dropped_columns == [("branching_dramas", "visual_style")]
