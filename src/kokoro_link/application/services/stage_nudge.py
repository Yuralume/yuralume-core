"""示意（「讓{角色}先開口」）—— 同場模式玩家拉動的一輪 (SN1).

The player presses a button instead of typing a line: the character is
asked to speak *first*, optionally after the player declares a fact about
the scene ("我推開門進來"). It is an ordinary chat turn in every other
respect — same lease, same ``ACTION_CHAT`` charge, same post-turn
pipeline — so the only thing this module owns is the *shape of the player
side of the turn*:

- a supplement is persisted as a **normal user message** whose content is
  wrapped in the asterisk-action convention the model already reads as a
  stage direction (see ``infrastructure/prompt/default.py``); no new
  ``MessageKind``, no new frontend bubble type.
- an empty supplement persists **nothing** on the player side. The turn is
  the character's message alone.

Keeping that decision here rather than inline in ``ChatService`` means the
non-streaming and streaming entry points cannot drift apart on it — the
two paths duplicate a lot of turn assembly and this is exactly the kind of
rule that gets fixed in one of them only.
"""

from __future__ import annotations

from dataclasses import dataclass

EMPHASIS_MARK = "*"
"""The asterisk-action convention: ``*推開門走進來*``.

Not a parser — the model is the one that reads it. We only wrap.
"""


def wrap_supplement(text: str) -> str:
    """Frame a nudge supplement as an action line.

    Whitespace-only input yields ``""`` — "the player pressed the button
    and said nothing", which is a first-class case, not an error.

    A supplement that *already* contains an asterisk anywhere is returned
    verbatim: the player wrote their own stage directions (``我推開門
    *小聲地* 說``) and wrapping the whole thing again would nest the
    convention inside itself. Deciding this on "contains any ``*``" rather
    than on a balanced-pair scan is deliberate — the looser test can only
    ever cost us an unwrapped line, while a stricter one starts guessing at
    the player's punctuation.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    if EMPHASIS_MARK in stripped:
        return stripped
    return f"{EMPHASIS_MARK}{stripped}{EMPHASIS_MARK}"


@dataclass(frozen=True, slots=True)
class StageNudgeTurn:
    """What the player contributed to this turn, and whether it is a nudge.

    ``text`` is the *persisted and prompted* player text: the wrapped
    supplement on a nudge turn, the cleaned message on an ordinary one.
    """

    enabled: bool
    text: str

    @classmethod
    def resolve(cls, cleaned_message: str, *, stage_nudge: bool) -> "StageNudgeTurn":
        if not stage_nudge:
            return cls(enabled=False, text=cleaned_message)
        return cls(enabled=True, text=wrap_supplement(cleaned_message))

    @property
    def writes_user_message(self) -> bool:
        """Whether this turn appends a user message at all.

        Only a *silent* nudge does not. An ordinary turn always writes one
        even when its text ends up empty (a bare ``/pic`` trigger strips to
        nothing yet still means "the player sent something"), and changing
        that is not this feature's business.
        """
        return not self.enabled or bool(self.text)


ORDINARY_TURN_SENTINEL = StageNudgeTurn(enabled=False, text="")
"""Convenience for call sites with no request payload (background turns)."""
