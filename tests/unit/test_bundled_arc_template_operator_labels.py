"""Shipped arc packs — the player-position labeling rule (OP0-B).

**The rule this file exists to keep alive:** an ``operator_note`` states
*the character's* motive, or the line she has not said yet ("她想問你這是
不是最後一次"). It never states something the player has already said,
decided or done ("你先開口說…", "你隨口問要不要一起去，她笑著點頭答應了").
``summary`` obeys the same rule — the player may be who the character is
reaching toward, never an actor whose line is already spent.

**Why it is a rule and not a taste call:** a beat's note is copied
verbatim into the scene opener prompt (and, via the session, the closer
prompt) *before* the beat is played —
``infrastructure/prompt/operator_scene_position.py`` appends it as
「額外備註」. A note that narrates the player's turn therefore tells the
model the player already spoke, before the player has had the chance;
what the model then writes can be canonised as history. SC1-D's
「收尾不得虛構玩家行動」 red line lives in the writer prompts, so shipped
material that pre-writes the player walks straight past it.
``operator_position`` already says the player is in this scene — the
prose never has to spend the player's turn to prove it.

The pins below are change detectors **by design**: they are the shipped
notes, verbatim. If you are here because you edited a shipped pack, read
the rule above, confirm your new wording obeys it, then update the pin.
The prose lives in
``src/kokoro_link/data/arc_templates/*.yaml`` with the same rule written
at the top of each file.
"""

from __future__ import annotations

from kokoro_link.domain.entities.arc_template import ArcTemplate
from kokoro_link.infrastructure.story.yaml_arc_template_repository import (
    YAMLArcTemplatePackLoader,
)


# pack_id → each beat's ``operator_position``, in sequence order.
_EXPECTED_POSITIONS: dict[str, tuple[str | None, ...]] = {
    # Solo ambition arc: the player is cast nowhere in it.
    "cafe_idol_audition": ("absent",) * 10,
    "quiet_breakup": (
        "central",
        "absent",
        "absent",
        "absent",
        "absent",
        "central",
        "absent",
        "central",
    ),
    "summer_festival_promise": (
        "absent",
        "central",
        "absent",
        "absent",
        "central",
        "central",
        "absent",
        "absent",
        "central",
        "central",
        "central",
        "absent",
    ),
}

# (pack_id, beat sequence) → the shipped ``operator_note``, verbatim.
# Every one of these reads as *her* motive or *her* unsaid line.
_EXPECTED_NOTES: dict[tuple[str, int], str] = {
    ("quiet_breakup", 0): (
        "她想在這頓早餐裡找回平常該有的溫度，卻不知道第一句話該說什麼"
    ),
    ("quiet_breakup", 5): (
        "她想跟你說一件白天的小事，話到嘴邊又吞了回去，還在等一個開口的時機"
    ),
    ("quiet_breakup", 7): "她想把這段關係好好收起來，卻不想由她一個人決定怎麼結束",
    ("summer_festival_promise", 1): "她把邀約講得像隨口一問，正在等你的回答",
    ("summer_festival_promise", 4): (
        "她想問你「這是不是我們最後一次一起去」，又怕先說出口的人是自己"
    ),
    ("summer_festival_promise", 5): "她想把路線排給你看，計畫之外那句話還沒排進去",
    ("summer_festival_promise", 8): (
        "她想在睡前傳一句話給你，打了又刪，只差按下送出"
    ),
    ("summer_festival_promise", 9): "她在人群裡先找到了你，正想著要用什麼表情走過去",
    ("summer_festival_promise", 10): "煙火炸開的瞬間，她偷偷看向你的側臉",
}


def _shipped_packs() -> dict[str, ArcTemplate]:
    """Parse the packs that actually ship, from the real data directory."""
    return {
        pack.pack_id: pack.template
        for pack in YAMLArcTemplatePackLoader().load_all()
    }


def test_shipped_packs_still_load_with_their_player_positions() -> None:
    templates = _shipped_packs()

    assert set(templates) == set(_EXPECTED_POSITIONS)
    for pack_id, expected in _EXPECTED_POSITIONS.items():
        beats = templates[pack_id].beats
        assert tuple(beat.sequence for beat in beats) == tuple(
            range(len(expected)),
        )
        assert tuple(beat.operator_position for beat in beats) == expected


def test_shipped_operator_notes_stay_on_the_character_side() -> None:
    """Pins the shipped notes — read the module docstring before editing.

    A note that narrates the player ("你先開口說…") is the failure this
    pin exists to catch: it reaches the opener/closer prompt before the
    beat is played and hands the model a player turn that never happened.
    """
    templates = _shipped_packs()

    shipped = {
        (pack_id, beat.sequence): beat.operator_note
        for pack_id, template in templates.items()
        for beat in template.beats
        if beat.operator_note is not None
    }

    assert shipped == _EXPECTED_NOTES


def test_shipped_notes_only_hang_off_beats_the_player_is_in() -> None:
    """An ``absent`` beat has nothing to say about the player's place.

    Not a style point: an absent beat's note would still be rendered into
    the opener block, so a note there is a standing invitation to write
    the player into a scene the labeling says they are not in.
    """
    templates = _shipped_packs()

    for template in templates.values():
        for beat in template.beats:
            if beat.operator_position == "absent":
                assert beat.operator_note is None
