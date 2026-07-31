"""Unit tests for ``StoryArcBeat`` scene-structure fields and for
``StoryArc.source_seed_ids`` provenance (AE2).

Covers Phase 1 of ``docs/SCENE_BEAT_PLAN.md`` — the new beat fields
(``scene_characters`` / ``location`` / ``dramatic_question`` /
``scene_type`` / ``required``) plus their normalisation in ``create``
and ``with_fields``. Validation rules (empty scene_type, malformed
scene_characters entries) are tested at the domain boundary so a
planner regression can't silently corrupt persisted rows.

The ``TestSourceSeedIds*`` classes cover the AE2 slice: ``StoryArc``
records which ``StorySeed`` ids a planner folded into an arc, with the
same "coerce, don't reject" normalisation stance as
``source_template_id`` (see ``_normalise_source_seed_ids``) so a
misbehaving LLM planner response degrades to a sane list instead of
crashing arc construction.
"""

from __future__ import annotations

from datetime import date

import pytest

from kokoro_link.domain.entities.story_arc import (
    MAX_SOURCE_SEED_ID_LENGTH,
    MAX_SOURCE_SEED_IDS,
    SCENE_CONFLICT,
    SCENE_ENCOUNTER,
    SCENE_REVELATION,
    ARC_COMPLETED,
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
    TENSION_SETUP,
)


def _arc(**overrides) -> StoryArc:
    """Create a StoryArc with sensible defaults so tests focus on overrides."""
    base: dict = {
        "character_id": "char-1",
        "title": "夏日祭典",
        "premise": "她決定今年一定要上台表演。",
        "theme": "ambition",
        "start_date": date(2026, 5, 1),
        "end_date": date(2026, 5, 21),
    }
    base.update(overrides)
    return StoryArc.create(**base)


class TestSourceSeedIdsDefaults:
    def test_create_without_source_seed_ids_defaults_to_empty_tuple(self) -> None:
        arc = _arc()
        assert arc.source_seed_ids == ()


class TestSourceSeedIdsNormalisation:
    def test_create_round_trips_source_seed_ids(self) -> None:
        arc = _arc(source_seed_ids=["seed-a", "seed-b"])
        assert arc.source_seed_ids == ("seed-a", "seed-b")

    def test_create_strips_and_dedupes_preserving_order(self) -> None:
        arc = _arc(
            source_seed_ids=["  seed-a  ", "seed-b", "seed-a", "", "   "],
        )
        assert arc.source_seed_ids == ("seed-a", "seed-b")

    def test_create_truncates_each_id_to_max_length(self) -> None:
        long_id = "x" * (MAX_SOURCE_SEED_ID_LENGTH + 50)
        arc = _arc(source_seed_ids=[long_id])
        assert arc.source_seed_ids == (long_id[:MAX_SOURCE_SEED_ID_LENGTH],)
        assert len(arc.source_seed_ids[0]) == MAX_SOURCE_SEED_ID_LENGTH

    def test_create_caps_at_max_entries(self) -> None:
        many = [f"seed-{i}" for i in range(MAX_SOURCE_SEED_IDS + 10)]
        arc = _arc(source_seed_ids=many)
        assert len(arc.source_seed_ids) == MAX_SOURCE_SEED_IDS
        assert arc.source_seed_ids == tuple(many[:MAX_SOURCE_SEED_IDS])

    def test_create_drops_non_string_entries_defensively(self) -> None:
        arc = _arc(
            source_seed_ids=["seed-a", 123, None, {"id": "seed-b"}],  # type: ignore[list-item]
        )
        assert arc.source_seed_ids == ("seed-a",)

    def test_direct_construction_also_normalises(self) -> None:
        # __post_init__ re-applies normalisation regardless of entry
        # point, so a direct constructor call (bypassing create()) still
        # cannot smuggle in an oversized / duplicated list.
        arc = StoryArc(
            id="arc-1",
            character_id="char-1",
            title="t",
            premise="p",
            theme="custom",
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 21),
            source_seed_ids=("seed-a", "seed-a", "  seed-b  "),
        )
        assert arc.source_seed_ids == ("seed-a", "seed-b")


class TestSourceSeedIdsPreservedAcrossWithHelpers:
    def test_with_beats_preserves_source_seed_ids(self) -> None:
        arc = _arc(source_seed_ids=["seed-a", "seed-b"])
        beat = StoryArcBeat.create(
            arc_id=arc.id,
            sequence=0,
            scheduled_date=date(2026, 5, 1),
            title="beat",
            summary="summary",
            tension=TENSION_SETUP,
        )
        updated = arc.with_beats([beat])
        assert updated.source_seed_ids == ("seed-a", "seed-b")

    def test_with_status_preserves_source_seed_ids(self) -> None:
        arc = _arc(source_seed_ids=["seed-a"])
        updated = arc.with_status(ARC_COMPLETED)
        assert updated.source_seed_ids == ("seed-a",)

    def test_with_title_premise_preserves_source_seed_ids(self) -> None:
        arc = _arc(source_seed_ids=["seed-a", "seed-b"])
        updated = arc.with_title_premise(title="新標題")
        assert updated.source_seed_ids == ("seed-a", "seed-b")


def _beat(**overrides) -> StoryArcBeat:
    """Create a beat with sensible defaults so tests focus on overrides."""
    base: dict = {
        "arc_id": "arc-1",
        "sequence": 0,
        "scheduled_date": date(2026, 5, 1),
        "title": "公告張貼",
        "summary": "週一早上她發現公告欄釘了一張新的試鏡海報。",
    }
    base.update(overrides)
    return StoryArcBeat.create(**base)


class TestSceneFieldDefaults:
    """`create()` without scene fields → sensible defaults so existing
    callers (LLM planner pre-Phase-1, hand-rolled tests) keep working."""

    def test_create_without_scene_fields_uses_defaults(self) -> None:
        beat = _beat()
        assert beat.scene_characters == ()
        assert beat.location is None
        assert beat.dramatic_question is None
        assert beat.scene_type == SCENE_ENCOUNTER
        assert beat.required is True


class TestSceneFieldNormalisation:
    def test_create_dedupes_and_strips_scene_characters(self) -> None:
        beat = _beat(
            scene_characters=["  夏目  ", "夏目", "凜", "", "  "],
        )
        # Stripped, deduped (case-sensitive), empty entries dropped.
        assert beat.scene_characters == ("夏目", "凜")

    def test_create_normalises_blank_strings_to_none(self) -> None:
        beat = _beat(
            location="   ",
            dramatic_question="   ",
        )
        assert beat.location is None
        assert beat.dramatic_question is None

    def test_create_strips_location_and_question(self) -> None:
        beat = _beat(
            location="  學校頂樓  ",
            dramatic_question="  她敢不敢承認？  ",
        )
        assert beat.location == "學校頂樓"
        assert beat.dramatic_question == "她敢不敢承認？"

    def test_create_blank_scene_type_falls_back_to_encounter(self) -> None:
        beat = _beat(scene_type="   ")
        assert beat.scene_type == SCENE_ENCOUNTER

    def test_create_accepts_unknown_scene_type_for_planner_robustness(
        self,
    ) -> None:
        # Permissive on purpose — prompt builder degrades unknown
        # values to encounter semantics. Planner can introduce shades
        # like "inner_monologue" without a domain code change.
        beat = _beat(scene_type="inner_monologue")
        assert beat.scene_type == "inner_monologue"

    def test_create_coerces_required_to_bool(self) -> None:
        # JSON-deserialised truthy values (1, "yes") shouldn't slip
        # through as non-bool — `required` is consumed downstream as
        # a strict bool gate ("required → must play today").
        beat = _beat(required=1)  # type: ignore[arg-type]
        assert beat.required is True
        beat_zero = _beat(required=0)  # type: ignore[arg-type]
        assert beat_zero.required is False


class TestSceneFieldValidation:
    def test_post_init_rejects_empty_scene_type(self) -> None:
        with pytest.raises(ValueError, match="scene_type"):
            StoryArcBeat(
                id="b1",
                arc_id="arc-1",
                sequence=0,
                scheduled_date=date(2026, 5, 1),
                title="t",
                summary="s",
                scene_type="",
            )

    def test_post_init_rejects_non_string_scene_character_entry(self) -> None:
        with pytest.raises(ValueError, match="scene_characters"):
            StoryArcBeat(
                id="b1",
                arc_id="arc-1",
                sequence=0,
                scheduled_date=date(2026, 5, 1),
                title="t",
                summary="s",
                scene_characters=(123,),  # type: ignore[arg-type]
            )

    def test_post_init_rejects_blank_scene_character_entry(self) -> None:
        with pytest.raises(ValueError, match="scene_characters"):
            StoryArcBeat(
                id="b1",
                arc_id="arc-1",
                sequence=0,
                scheduled_date=date(2026, 5, 1),
                title="t",
                summary="s",
                scene_characters=("夏目", "  "),
            )


class TestWithFieldsScenePatching:
    def test_with_fields_overrides_only_given_keys(self) -> None:
        beat = _beat(
            scene_characters=["夏目"],
            location="教室",
            scene_type=SCENE_ENCOUNTER,
            required=True,
        )
        patched = beat.with_fields(
            location="頂樓",
            scene_type=SCENE_REVELATION,
        )
        assert patched.location == "頂樓"
        assert patched.scene_type == SCENE_REVELATION
        # Untouched fields preserved.
        assert patched.scene_characters == ("夏目",)
        assert patched.required is True
        assert patched.title == beat.title

    def test_with_fields_replaces_scene_characters_completely(self) -> None:
        beat = _beat(scene_characters=["夏目", "凜"])
        patched = beat.with_fields(scene_characters=["佐藤"])
        # Replaces — does NOT merge. with_fields is a setter, not a patch.
        assert patched.scene_characters == ("佐藤",)

    def test_with_fields_blank_location_becomes_none(self) -> None:
        beat = _beat(location="教室")
        patched = beat.with_fields(location="   ")
        assert patched.location is None

    def test_with_fields_required_false_persists(self) -> None:
        beat = _beat(required=True)
        patched = beat.with_fields(required=False)
        assert patched.required is False

    def test_with_fields_preserves_existing_scene_type_when_blank(self) -> None:
        beat = _beat(scene_type=SCENE_CONFLICT)
        patched = beat.with_fields(scene_type="   ")
        # Falls back to existing value, not the encounter default.
        assert patched.scene_type == SCENE_CONFLICT

    def test_existing_with_fields_signature_still_works(self) -> None:
        # Backwards compat: callers passing only the original keyword
        # set (scheduled_date / title / summary / tension) continue to
        # work without thinking about scene fields.
        beat = _beat()
        patched = beat.with_fields(
            title="新的場景",
            tension=TENSION_RISING,
        )
        assert patched.title == "新的場景"
        assert patched.tension == TENSION_RISING
        # Defaults untouched.
        assert patched.scene_characters == ()
        assert patched.scene_type == SCENE_ENCOUNTER
