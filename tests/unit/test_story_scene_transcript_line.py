"""The scene transcript line format (`render_scene_line`).

Every scene writer's prompt promises「每行冒號前是說話的人」— one
physical line per entry. The opener now produces multi-paragraph
narration, so the shared renderer must flatten internal newlines, or a
narration's second paragraph becomes an unlabelled transcript line and
the chips writer mis-attributes it (the same speaker-direction bug the
「旁白」 label was introduced to stop).
"""

from __future__ import annotations

from kokoro_link.contracts.story_scene import (
    SCENE_NARRATION_SPEAKER,
    render_scene_line,
)


def test_a_plain_line_renders_speaker_colon_text() -> None:
    assert render_scene_line("Aki", "你來了。") == "Aki：你來了。"


def test_multi_paragraph_text_flattens_to_one_physical_line() -> None:
    line = render_scene_line(
        SCENE_NARRATION_SPEAKER, "第一段。\n\n第二段。\n第三段。",
    )

    assert "\n" not in line
    assert line == "旁白：第一段。 第二段。 第三段。"


def test_runs_of_whitespace_collapse() -> None:
    assert render_scene_line("Aki", "  你\t來了。  ") == "Aki：你 來了。"
