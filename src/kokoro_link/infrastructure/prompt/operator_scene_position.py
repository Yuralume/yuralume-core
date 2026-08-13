"""Shared framing text for where the player stands in a 起幕 scene.

OP0-A gave a beat (and, via ``StorySceneMaterial`` / ``StorySceneSession``,
the scene it opens into) a closed ``operator_position`` slot —
``absent`` / ``present`` / ``central`` / ``None`` (unjudged) — plus a free
``operator_note``. Both the opener (SC1-A) and the closer (SC1-D) need to
turn that slot into an instruction, and they need to turn it into the
*same* instruction: the closer reads the position the opener already
framed the scene from (copied onto the session at open time), so two
diverging vocabularies for the same enum would have the wrap-up
contradict the opening it is wrapping up. One module, one render function
per writer, both keyed off the same three constants.

``None`` is not a fourth position to hand-write copy for — the plan's §2
pick #4 is explicit that an unjudged beat is answered by *derivation*, not
a guess baked into code. So the ``None`` branch here hands the judgment
back to the model from the rest of the material (cast list, dramatic
question, summary) rather than special-casing any keyword in it — writing
a keyword table for "does '伴侶' mean the player" is exactly what the
LLM-first rule forbids.
"""

from __future__ import annotations

from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
)

_OPENER_FRAMING: dict[str, str] = {
    OPERATOR_POSITION_CENTRAL: (
        "玩家在這場戲的位置：核心。這場戲就是關於玩家的——沒有玩家，這場戲演不下去。"
        "把玩家寫進戲的正中央，角色第一句台詞應該是直接對玩家演出來的，"
        "不是說給旁人聽的。"
    ),
    OPERATOR_POSITION_PRESENT: (
        "玩家在這場戲的位置：在場，但不是核心。玩家人在這場戲裡——陪伴、旁觀、"
        "或剛好被捲入——但這場戲的重心是角色自己的處境。讓玩家自然地在場，"
        "不要把玩家寫成這場戲要解決的問題。"
    ),
    OPERATOR_POSITION_ABSENT: (
        "玩家在這場戲的位置：原本不在場。這原本是一段角色自己的戲，玩家不是"
        "計畫內的人物。開場請自然帶出「玩家走進一場本來沒有他的戲」的感覺——"
        "可以是巧遇、路過、被叫住——不要假裝玩家理所當然一直都在場。"
    ),
}

_OPENER_UNJUDGED = (
    "玩家在這場戲的位置：沒有人標記過。請你自己從出場人物、戲劇問題與摘要判斷："
    "如果內容明顯只關於角色與其他人物、不含玩家，就寫成旁觀或「玩家剛好走進來」"
    "的自然引入；如果內容裡有可能指玩家的詞（例如「伴侶」「他」「你們兩人」"
    "這類未指名的對象），由你自己判斷是否就是在說玩家，並據此決定玩家在這場戲的位置。"
)

_CLOSER_FRAMING: dict[str, str] = {
    OPERATOR_POSITION_CENTRAL: (
        "玩家在這場戲的位置：核心。這場戲原本就是關於玩家的，收尾時鏡頭仍然可以"
        "圍繞這段關係怎麼落地——但這不改變下面的紅線：沒有寫在逐字稿裡的玩家行動"
        "或發言，一律當作沒有發生。"
    ),
    OPERATOR_POSITION_PRESENT: (
        "玩家在這場戲的位置：在場，但不是核心。收尾可以提到玩家在場這件事本身"
        "如何影響角色，但這場戲終究是角色自己的處境——同樣不得替玩家補上逐字稿"
        "沒有的行動或發言。"
    ),
    OPERATOR_POSITION_ABSENT: (
        "玩家在這場戲的位置：原本不在場。這原本是一段角色自己的戲，玩家只是這次"
        "剛好在場、旁觀或路過——收尾請保留「這是角色自己的事」這個基調，"
        "不要把它寫成玩家的戲。"
    ),
}

_CLOSER_UNJUDGED = (
    "玩家在這場戲的位置：沒有人標記過。請依逐字稿與這場戲的性質自己判斷玩家"
    "比較像是這場戲的核心、在場的陪伴，還是原本不在場、只是這次剛好參與了——"
    "並據此決定收尾旁白的鏡頭該擺在誰身上。"
)


def render_opener_operator_position_block(
    position: str | None, note: str | None,
) -> str:
    """Framing instruction for the scene opener (SC1-A)."""
    return _render(_OPENER_FRAMING, _OPENER_UNJUDGED, position, note)


def render_closer_operator_position_block(
    position: str | None, note: str | None,
) -> str:
    """Framing instruction for the scene closer (SC1-D).

    Every branch here is written to sit *alongside* the closer's own
    「不得虛構玩家行動」 red line, never to loosen it — even ``central``
    only moves where the camera lingers, not what may be invented.
    """
    return _render(_CLOSER_FRAMING, _CLOSER_UNJUDGED, position, note)


def _render(
    framing: dict[str, str],
    unjudged: str,
    position: str | None,
    note: str | None,
) -> str:
    body = framing.get(position or "", unjudged)
    cleaned_note = (note or "").strip()
    if cleaned_note:
        body = f"{body}\n關於玩家在這場戲的位置，額外備註：{cleaned_note}"
    return body
