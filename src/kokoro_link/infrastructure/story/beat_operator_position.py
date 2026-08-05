"""Where the player stands in a beat, for the two beat-narrating writers.

OP2-C of ``docs/plans/ARC_PLAYER_POSITION_PLAN.md``. A beat's narrative
prose is written by **two** independent writers — the autonomous beat
scene writer (``story/beat_scene_writer``) and the expander's scene mode
(``story/expander_scene``, used when a beat is realized through
``SceneContext``). Whichever one happens to run, the resulting prose is
persisted as the *same* StoryEvent, so the two must frame the player the
same way; otherwise the same beat reads like two different worlds
depending on which path picked it up. Hence one module, one framing
vocabulary, both writers rendering from it.

What this replaces: before OP0 gave a beat an ``operator_position`` slot,
both writers were handed a *default policy* that amounted to giving up on
the player — «使用者目前不保證在場；…讓 beat 不依賴使用者也能完成» in the
autonomous writer, «不要把使用者寫進這場戲» in the expander. Those were
the honest answer to "the model has no way to know where the player
stands", not a creative choice. With the slot filled the answer comes
from the beat itself, so the policy retires (plan §5.1 OP2-C).

Two rules this module exists to keep straight:

* ``None`` is *unjudged*, not a fourth position. The plan's pick #4 is
  explicit that an unjudged beat is answered by **derivation** — the
  model reads the cast, the dramatic question and the summary and
  decides for itself whether e.g. 「伴侶」 means the player. Writing a
  keyword table for that is exactly what the LLM-first rule in
  ``AGENTS.md`` forbids, so the ``None`` branch hands the judgment back
  to the model instead of guessing in code.
* **No player lines, ever.** Unlike the 起幕 closer (SC1-D), which at
  least has a transcript to check against, these two writers produce
  their prose in a single shot with *no player turn at all*. So
  ``present``/``central`` widen where the camera may point, never what
  may be invented: the red line below is appended to every branch,
  including the unjudged one.

Not to be confused with ``infrastructure/prompt/operator_scene_position``
(OP2-D), which frames the *live* 起幕 opener/closer — there the player is
actually at the keyboard. Same enum, different situation, so the wording
deliberately differs; the shared semantics (central = the scene is about
the player, present = in the room but not the subject, absent = not in
this scene, None = derive it) are the ones OP2-A fixed for the chat
surface.
"""

from __future__ import annotations

from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
)


_FRAMING: dict[str, str] = {
    OPERATOR_POSITION_CENTRAL: (
        "玩家在這場戲的位置：核心。這場戲本來就是關於玩家的——沒有玩家，"
        "這場戲不成立。但這段敘事是在玩家不在現場的情況下寫成的，"
        "所以請只寫角色自己的那一側：她怎麼準備、怎麼等、"
        "這件事在她心裡是什麼重量，或它最後留下了什麼。"
        "不要替玩家把這場戲演完。"
    ),
    OPERATOR_POSITION_PRESENT: (
        "玩家在這場戲的位置：在場，但不是核心。玩家人在這場戲裡——"
        "陪著、旁觀、或剛好被捲入——但戲的重心仍然是角色自己的處境。"
        "可以寫角色注意到玩家、對玩家說話、感覺到他就在旁邊；"
        "不要把玩家寫成這場戲要解決的問題。"
    ),
    OPERATOR_POSITION_ABSENT: (
        "玩家在這場戲的位置：不在場。這是角色自己的戲，"
        "用角色自己的內心、出場人物、companion 或 NPC 把它演完。"
        "不要把玩家寫進場景，也不要把結尾寫成在等玩家。"
    ),
}

_UNJUDGED = (
    "玩家在這場戲的位置：沒有人標記過。請你自己從出場人物、戲劇問題與摘要判斷："
    "若內容明顯只關於角色與其他人物、不含玩家，就當作角色自己的戲；"
    "若內容裡出現可能指玩家的詞（例如「伴侶」「對方」「你們兩人」"
    "這類沒有指名的對象），由你自己判斷那到底是不是在說玩家，"
    "並據此決定玩家在這場戲裡的位置。"
)

# Appended to every branch, including ``central``. See the module
# docstring: widening where the camera points must never widen what may
# be invented.
_NO_INVENTED_PLAYER = (
    "紅線：這段敘事沒有玩家的逐字紀錄——玩家說了什麼、做了什麼、"
    "答應了什麼，一律不得虛構。玩家在場時只寫角色這一側看見與感受到的，"
    "玩家那一側該留白就留白。"
)

_NOTE_PREFIX = "關於玩家在這場戲的位置，額外備註："


def render_beat_operator_position_block(
    position: str | None,
    note: str | None = None,
    *,
    caller_directive: str | None = None,
) -> str:
    """The player-framing paragraph both beat writers embed verbatim.

    ``position`` is a ``VALID_OPERATOR_POSITIONS`` member or ``None``.
    Anything unrecognised is treated as unjudged rather than raising:
    the domain boundary already rejects bad values on write, and a beat
    that somehow carries a stale one should still get *a* scene instead
    of taking down the whole realization path.

    ``caller_directive`` is the operator-supplied override that used to
    be the (now retired) default involvement policy — the simulate route
    still accepts one. It is rendered as an *additional* instruction,
    explicitly subordinate to the red line, so an override can redirect
    the framing but cannot license inventing player lines.
    """
    parts = [_FRAMING.get(position or "", _UNJUDGED)]
    cleaned_note = (note or "").strip()
    if cleaned_note:
        parts.append(f"{_NOTE_PREFIX}{cleaned_note}")
    parts.append(_NO_INVENTED_PLAYER)
    cleaned_directive = (caller_directive or "").strip()
    if cleaned_directive:
        parts.append(
            "呼叫端的額外指示（可以改變上面的框架，但不得放寬紅線）："
            f"{cleaned_directive}",
        )
    return "\n".join(parts)
