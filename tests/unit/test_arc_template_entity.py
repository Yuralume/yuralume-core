"""ArcTemplate domain entity — Phase 2 of SCENE_BEAT_PLAN."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from kokoro_link.domain.entities.arc_template import (
    ARC_TEMPLATE_SCOPE_CHARACTER_BOUND,
    ARC_TEMPLATE_SCOPE_GENERIC,
    ArcTemplate,
    ArcTemplateBeat,
    ArcTemplateBinding,
)
from kokoro_link.domain.entities.story_arc import (
    ARC_ACTIVE,
    BEAT_PENDING,
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
    SCENE_CONFLICT,
    SCENE_ENCOUNTER,
    TENSION_RISING,
)


def _beat(
    *,
    sequence: int = 0,
    day_offset: int = 0,
    title: str = "公告",
    summary: str = "週一的公告欄上有新海報。",
    **kwargs,
) -> ArcTemplateBeat:
    return ArcTemplateBeat.create(
        sequence=sequence,
        day_offset=day_offset,
        title=title,
        summary=summary,
        **kwargs,
    )


class TestArcTemplateBeatNormalisation:
    def test_create_dedupes_scene_characters(self) -> None:
        beat = _beat(scene_characters=["夏目", " 夏目 ", "凜", ""])
        assert beat.scene_characters == ("夏目", "凜")

    def test_create_normalises_blank_strings_to_none(self) -> None:
        beat = _beat(location="   ", dramatic_question="   ")
        assert beat.location is None
        assert beat.dramatic_question is None

    def test_blank_scene_type_falls_back(self) -> None:
        beat = _beat(scene_type="")
        assert beat.scene_type == SCENE_ENCOUNTER

    def test_required_coerces_to_bool(self) -> None:
        truthy = _beat(required=1)  # type: ignore[arg-type]
        falsy = _beat(required=0)  # type: ignore[arg-type]
        assert truthy.required is True
        assert falsy.required is False


class TestArcTemplateBeatValidation:
    def test_negative_sequence_rejected(self) -> None:
        with pytest.raises(ValueError, match="sequence"):
            ArcTemplateBeat(
                sequence=-1, day_offset=0, title="t", summary="s",
            )

    def test_negative_day_offset_rejected(self) -> None:
        with pytest.raises(ValueError, match="day_offset"):
            ArcTemplateBeat(
                sequence=0, day_offset=-1, title="t", summary="s",
            )


class TestArcTemplate:
    def test_create_orders_beats_by_day_offset(self) -> None:
        tpl = ArcTemplate.create(
            id="cafe_idol_audition",
            title="三週的試鏡",
            premise="她報名了一場從沒想過會報的試鏡。",
            theme="ambition",
            duration_days=14,
            beats=[
                _beat(sequence=2, day_offset=10, title="深夜練習"),
                _beat(sequence=0, day_offset=0, title="公告張貼"),
                _beat(sequence=1, day_offset=5, title="撞牆"),
            ],
        )
        # Beats sort by (day_offset, sequence) so authoring order
        # doesn't matter and downstream consumers always see chronology.
        assert [b.title for b in tpl.beats] == ["公告張貼", "撞牆", "深夜練習"]
        assert tpl.beat_count == 3

    def test_empty_beats_rejected(self) -> None:
        with pytest.raises(ValueError, match="beats"):
            ArcTemplate.create(
                id="empty", title="t", premise="p", duration_days=14,
                beats=[],
            )

    def test_blank_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="id"):
            ArcTemplate.create(
                id="   ", title="t", premise="p", duration_days=14,
                beats=[_beat()],
            )


class TestMaterialise:
    def test_materialise_produces_active_arc_with_pending_beats(self) -> None:
        start = date(2026, 5, 1)
        tpl = ArcTemplate.create(
            id="cafe_idol_audition",
            title="三週的試鏡",
            premise="她報名了一場從沒想過會報的試鏡。",
            theme="ambition",
            duration_days=14,
            beats=[
                _beat(
                    sequence=0, day_offset=0, title="公告",
                    location="學校公告欄", scene_type=SCENE_ENCOUNTER,
                ),
                _beat(
                    sequence=1, day_offset=5, title="撞牆",
                    summary="老師看著鏡子裡的自己。",
                    tension=TENSION_RISING, scene_type=SCENE_CONFLICT,
                    location="音樂教室", scene_characters=["指導老師"],
                    dramatic_question="她要承認嗎？", required=True,
                ),
            ],
        )
        arc = tpl.materialise(character_id="char-1", start_date=start)
        assert arc.character_id == "char-1"
        assert arc.title == "三週的試鏡"
        assert arc.status == ARC_ACTIVE
        assert arc.start_date == start
        assert arc.end_date == start + timedelta(days=14)
        assert len(arc.beats) == 2
        first, second = arc.beats
        # day_offset → scheduled_date conversion.
        assert first.scheduled_date == start
        assert second.scheduled_date == start + timedelta(days=5)
        # Scene structure carries over verbatim.
        assert second.location == "音樂教室"
        assert second.scene_characters == ("指導老師",)
        assert second.dramatic_question == "她要承認嗎？"
        assert second.scene_type == SCENE_CONFLICT
        # Materialised beats start in PENDING — service layer flips
        # them as days arrive.
        assert all(b.status == BEAT_PENDING for b in arc.beats)
        # arc_id is shared by all beats post-materialise.
        assert {b.arc_id for b in arc.beats} == {arc.id}

    def test_materialise_caps_offset_past_duration(self) -> None:
        # Author wrote day_offset=30 on a 14-day arc — cap to 14
        # rather than push the beat past arc.end_date.
        start = date(2026, 5, 1)
        tpl = ArcTemplate.create(
            id="capped",
            title="短篇",
            premise="短篇前情。",
            duration_days=14,
            beats=[_beat(day_offset=30, title="末日")],
        )
        arc = tpl.materialise(character_id="char-1", start_date=start)
        assert arc.beats[0].scheduled_date == start + timedelta(days=14)

    def test_materialise_distinct_arcs_for_two_characters(self) -> None:
        # Same template, two characters → two arcs with distinct ids.
        start = date(2026, 5, 1)
        tpl = ArcTemplate.create(
            id="t1", title="t", premise="p", duration_days=10,
            beats=[_beat()],
        )
        a = tpl.materialise(character_id="a", start_date=start)
        b = tpl.materialise(character_id="b", start_date=start)
        assert a.id != b.id
        assert a.character_id == "a"
        assert b.character_id == "b"


class TestBinding:
    def test_default_binding_is_unconstrained(self) -> None:
        tpl = ArcTemplate.create(
            id="t1", title="t", premise="p", duration_days=10,
            beats=[_beat()],
        )
        assert tpl.binding == ArcTemplateBinding()
        assert tpl.binding.world_frames == ()
        assert tpl.binding.required_traits == ()

    def test_binding_carries_through(self) -> None:
        binding = ArcTemplateBinding(
            world_frames=("modern", "school"),
            required_traits=("ambitious",),
        )
        tpl = ArcTemplate.create(
            id="t1", title="t", premise="p", duration_days=10,
            beats=[_beat()], binding=binding,
        )
        assert tpl.binding.world_frames == ("modern", "school")
        assert tpl.binding.required_traits == ("ambitious",)


class TestApplicability:
    def test_generic_template_applies_to_any_character(self) -> None:
        tpl = ArcTemplate.create(
            id="t1", title="t", premise="p", beats=[_beat()],
            applicability_scope=ARC_TEMPLATE_SCOPE_GENERIC,
        )

        assert tpl.is_applicable_to("char-a") is True
        assert tpl.target_character_ids == ()

    def test_character_bound_template_only_applies_to_targets(self) -> None:
        tpl = ArcTemplate.create(
            id="t1",
            title="t",
            premise="p",
            beats=[_beat()],
            applicability_scope=ARC_TEMPLATE_SCOPE_CHARACTER_BOUND,
            target_character_ids=["char-a", " char-a ", "char-b"],
        )

        assert tpl.target_character_ids == ("char-a", "char-b")
        assert tpl.is_applicable_to("char-a") is True
        assert tpl.is_applicable_to("char-c") is False

    def test_invalid_scope_rejected(self) -> None:
        with pytest.raises(ValueError, match="applicability_scope"):
            ArcTemplate.create(
                id="t1", title="t", premise="p", beats=[_beat()],
                applicability_scope="private",
            )


class TestOperatorPositionSymmetry:
    """OP0-A — the blueprint beat carries the same player slot, under the
    same rules, as the runtime beat. Asymmetry here would mean a template
    author and a planner author face two different conventions."""

    def test_defaults_to_unjudged(self) -> None:
        beat = _beat()
        assert beat.operator_position is None
        assert beat.operator_note is None

    def test_accepts_each_legal_position(self) -> None:
        for value in (
            OPERATOR_POSITION_ABSENT,
            OPERATOR_POSITION_PRESENT,
            OPERATOR_POSITION_CENTRAL,
        ):
            assert _beat(operator_position=value).operator_position == value

    def test_position_is_stripped_and_lowercased(self) -> None:
        assert (
            _beat(operator_position="  Central ").operator_position
            == OPERATOR_POSITION_CENTRAL
        )

    def test_blank_position_is_unjudged(self) -> None:
        assert _beat(operator_position="  ").operator_position is None

    def test_unknown_position_rejected_in_create(self) -> None:
        with pytest.raises(ValueError, match="operator_position"):
            _beat(operator_position="protagonist")

    def test_unknown_position_rejected_at_the_constructor(self) -> None:
        with pytest.raises(ValueError, match="operator_position"):
            ArcTemplateBeat(
                sequence=0,
                day_offset=0,
                title="t",
                summary="s",
                operator_position="lead",
            )

    def test_note_is_stripped_prose(self) -> None:
        beat = _beat(operator_note="  她要向你坦白  ")
        assert beat.operator_note == "她要向你坦白"

    def test_blank_note_is_none(self) -> None:
        assert _beat(operator_note="   ").operator_note is None


class TestMaterialisePlayerPosition:
    def test_materialise_carries_the_pair_into_the_runtime_beat(self) -> None:
        tpl = ArcTemplate.create(
            id="quiet_breakup",
            title="安靜的分手",
            premise="一段關係走到了盡頭。",
            duration_days=7,
            beats=[
                _beat(
                    sequence=0,
                    day_offset=0,
                    title="她開口",
                    operator_position=OPERATOR_POSITION_CENTRAL,
                    operator_note="她要向你坦白",
                ),
                _beat(sequence=1, day_offset=2, title="她獨自走回家"),
            ],
        )

        arc = tpl.materialise(character_id="char-1", start_date=date(2026, 5, 1))

        assert arc.beats[0].operator_position == OPERATOR_POSITION_CENTRAL
        assert arc.beats[0].operator_note == "她要向你坦白"
        # An unauthored beat stays unjudged — materialise never invents
        # a position the template did not declare.
        assert arc.beats[1].operator_position is None
        assert arc.beats[1].operator_note is None
