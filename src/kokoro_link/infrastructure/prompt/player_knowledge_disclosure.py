"""Prompt text for the proactive disclosure judge (KB8).

Code-side rather than a shipped pack template, for the same reason the
outcome-claim judge's text is (see
:mod:`kokoro_link.infrastructure.prompt.outcome_claim_honesty`): this is
a ledger gate, and a gate has to be true of the build that contains it.
A pack template reaches a hosted deployment only through a release, so a
deployment running last week's pack would run this week's flip logic
against last week's wording — and the value being written here is one
the system cannot walk back.

The chat channel asks the same question inside ``post_turn/processor``
(a pack template) because there it is one more field on an extraction
that was already pack-driven, and its output is bounded by the same
candidate allow-list either way. The asymmetry is deliberate: the shared
thing is the *question*, and it is short enough that two phrasings of it
cost less than the coupling of forcing one surface's delivery model onto
the other.

Red line, restated where it is implemented: **no persona, no
relationship, no reasoning trace**. The judge sees the message the player
received and the candidate memories, and nothing that would let it argue
about what she must have meant.
"""

from __future__ import annotations

from typing import Final

from kokoro_link.contracts.player_knowledge_disclosure import (
    DisclosureCandidate,
)

DISCLOSED_FIELD: Final = "disclosed_memory_ids"
"""The one field the judge answers in. Exported so the adapter that
parses the reply and the prompt that asks for it cannot drift."""

_MAX_MESSAGE_CHARS: Final = 4000
"""How much of the delivered message the judge reads. A proactive push
is composer-capped well under this; the bound exists so a fanned-out or
concatenated delivery can't turn one judgement into an expensive call."""

_MAX_CANDIDATES: Final = 8
_MAX_CONTENT_CHARS: Final = 120
"""Enough to recognise which memory a line names. The judge is matching
the message against these, not reasoning from them."""


def render_disclosure_judge_prompt(
    *,
    message_text: str,
    candidates: tuple[DisclosureCandidate, ...],
) -> str:
    """Ask which candidate memories the message actually told the player.

    The candidate list is rendered as ``id｜content`` lines because the
    answer is a subset of those ids; printing the ids is what makes the
    reply checkable against an allow-list rather than interpreted.
    """
    message = (message_text or "").strip()[:_MAX_MESSAGE_CHARS]
    lines = [
        "你是一個嚴格的比對員。下面是一則角色主動傳給對方的訊息，"
        "以及幾則「對方原本不知道的事」。",
        "請判斷：這則訊息裡，角色**真的把哪幾件事講給對方聽了**。",
        "",
        "訊息全文：",
        "-----",
        message,
        "-----",
        "",
        "候選事項（id｜內容）：",
    ]
    for candidate in candidates[:_MAX_CANDIDATES]:
        content = (candidate.content or "").strip()
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS] + "…"
        lines.append(f"- {candidate.memory_id}｜{content}")
    lines.extend([
        "",
        "判準：",
        "- 只看訊息本身講了什麼。訊息裡真的把那件事講出來了（哪怕只是簡短一句、"
        "或換句話說出同一件事），才算講了。",
        "- 以下都**不算**講了：那件事只影響了訊息的語氣或心情、"
        "只是角度相近但講的是別件事、只含糊暗示到對方看不出是什麼事。",
        "- 寧可漏判也不要誤判：漏判只是下次再講一次，誤判會讓角色"
        "把沒說過的事當成雙方共識。",
        "",
        "輸出規則：",
        f'- 只輸出一個 JSON 物件：{{"{DISCLOSED_FIELD}": ["id", ...]}}。',
        "- 陣列元素只能是上面列出的 id；沒有任何一件被講出來就給空陣列 []。",
        "- 不要輸出內容、理由或任何散文，也不要用 code fence。",
    ])
    return "\n".join(lines)


__all__ = ["DISCLOSED_FIELD", "render_disclosure_judge_prompt"]
