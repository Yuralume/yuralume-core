"""Port for character-authored replies to user comments on feed posts.

Phase B of LumeGram. Where ``FeedComposerPort`` produces an
autobiographical post + image prompt, this port produces a single
short comment that the character writes back at the user — closer in
shape to a chat reply than to a post body. Kept as its own port so
the prompt template, output schema, and adapter cost can evolve
independently from the post composer (different feature key for LLM
routing, different output JSON, no image generation).

Adapters must be fail-soft: any error returns an empty body. The
service treats an empty body as "skip this round" — no exception
escapes into the proactive scheduler tick.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.feed_comment import FeedComment
from kokoro_link.domain.entities.feed_post import FeedPost


@dataclass(frozen=True, slots=True)
class FeedCommentReplyInput:
    """Everything the LLM needs to write one reply.

    ``user_comments`` is chronological (oldest first) so the model can
    see the conversational arc — multiple rapid comments from the user
    should fold into a single coherent reply, not a per-comment echo.
    ``recent_chat_lines`` is optional context (last few user/character
    messages) so the reply tone matches whatever's been going on
    elsewhere; pre-trimmed by the service.
    """

    character: Character
    post: FeedPost
    user_comments: tuple[FeedComment, ...]
    recent_chat_lines: tuple[str, ...] = ()
    busy_hint: str = ""
    """Short string the service crafts about the character's current
    state (e.g. "剛下班、有點累", "正在專注工作"). Helps the model decide
    tone/length without re-deriving from raw activity rows."""
    operator_primary_language: str = "zh-TW"
    """BCP 47 tag of the character owner's pinned content language
    (FRONTEND_I18N_PLAN). Pinned via the same fact line that chat /
    proactive / feed-composer use so a comment reply doesn't drift to
    a different language than the post it's attached to."""


@dataclass(frozen=True, slots=True)
class FeedCommentReplyOutput:
    """Result of one reply call.

    Empty ``content_text`` signals "model declined / failed". The
    service treats this as a skip — it does not retry within the same
    tick, and does not advance any watermark, so the next tick gets a
    fresh attempt.
    """

    content_text: str


class FeedCommentReplyComposerPort(ABC):
    """Compose one character-authored reply for a batch of user comments."""

    @abstractmethod
    async def compose(
        self, payload: FeedCommentReplyInput,
    ) -> FeedCommentReplyOutput: ...
