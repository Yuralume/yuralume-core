"""LLM-backed adapter for character → user feed-comment replies.

Sister of :mod:`infrastructure.feed.llm_composer` (the post composer)
but with a much simpler output shape: one short string, no image
prompt, no JSON schema. The reply is meant to read like a real IG
comment — terse, in-character, naturally responding to whatever the
user said. Parsing is lenient: we strip optional markdown / quote
fences and trim to a hard ceiling so a runaway model doesn't post a
five-paragraph comment.

A ``NullFeedCommentReplyComposer`` is also exported here so the
container can wire in a no-op when the deployment runs on the fake
provider — keeps the integration surface uniform with the post-side
``NullFeedComposer``.
"""

from __future__ import annotations

import logging
import re

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.feed_comment_reply import (
    FeedCommentReplyComposerPort,
    FeedCommentReplyInput,
    FeedCommentReplyOutput,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_REPLY_CHARS = 180
"""Cap on the rendered reply. IG comments are short; anything past
this almost always reads as a paragraph the model wandered into. The
service treats over-cap output as "trim to cap" not "reject" — if the
model produced something coherent we'd rather keep the gist than skip
the whole tick."""

_MAX_USER_COMMENT_CHARS_PER_LINE = 160
_MAX_POST_BODY_CHARS = 200


class LLMFeedCommentReplyComposer(FeedCommentReplyComposerPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def compose(
        self, payload: FeedCommentReplyInput,
    ) -> FeedCommentReplyOutput:
        if await self._resolver.is_fake(character=payload.character):
            return FeedCommentReplyOutput(content_text="")
        prompt = _build_prompt(payload)
        try:
            raw = await self._resolver.generate(
                prompt, character=payload.character,
            )
        except Exception:
            _LOGGER.exception(
                "feed reply composer LLM call failed character=%s post=%s",
                payload.character.id, payload.post.id,
            )
            return FeedCommentReplyOutput(content_text="")
        body = _normalize(raw)
        return FeedCommentReplyOutput(content_text=body)


class NullFeedCommentReplyComposer(FeedCommentReplyComposerPort):
    """Always returns an empty body.

    Used when the active provider is the fake one (tests / dev with no
    LLM wired). Service treats empty as "skip this tick", so the whole
    reply pipeline degrades to a no-op without any conditional in the
    container."""

    async def compose(
        self, payload: FeedCommentReplyInput,
    ) -> FeedCommentReplyOutput:
        return FeedCommentReplyOutput(content_text="")


# ----------------------------------------------------------------------
# Prompt rendering
# ----------------------------------------------------------------------


def _build_prompt(payload: FeedCommentReplyInput) -> str:
    character = payload.character
    persona = _persona_block(character)
    post_body = _shorten(payload.post.content_text, _MAX_POST_BODY_CHARS)
    user_lines = "\n".join(
        f"- 使用者：「{_shorten(c.content_text, _MAX_USER_COMMENT_CHARS_PER_LINE)}」"
        for c in payload.user_comments
    )
    busy_clause = (
        f"當下狀態：{payload.busy_hint}。回覆語氣請貼合這個狀態。"
        if payload.busy_hint else
        "當下沒有特別的疲勞或情緒線索，照角色平常的語氣回。"
    )
    body = get_default_loader().render(
        "feed/comment_reply",
        max_reply_chars=_MAX_REPLY_CHARS,
        persona_block="\n".join(persona),
        busy_clause=busy_clause,
        post_body=post_body,
        user_lines=user_lines,
    )
    language_hint = render_operator_language_hint(
        payload.operator_primary_language,
    )
    if language_hint:
        body = f"{language_hint}\n\n{body}"
    return body


def _persona_block(character: Character) -> list[str]:
    lines = [f"- 名稱：{character.name}"]
    lines.extend(render_character_identity_lines(character))
    if character.summary:
        lines.append(f"- 簡介：{character.summary[:160]}")
    if character.personality:
        lines.append("- 性格：" + "、".join(character.personality[:6]))
    if character.speaking_style:
        lines.append(f"- 說話風格：{character.speaking_style[:120]}")
    state = character.state
    lines.append(
        "- 當前狀態：情緒 "
        f"{state.emotion}/好感 {state.affection}/疲勞 {state.fatigue}/"
        f"信任 {state.trust}/能量 {state.energy}",
    )
    return lines


# ----------------------------------------------------------------------
# Output cleaning
# ----------------------------------------------------------------------


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_LEADING_QUOTE_RE = re.compile(r'^[\s"「『]+')
_TRAILING_QUOTE_RE = re.compile(r'[\s"」』]+$')


def _normalize(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = _FENCE_RE.sub("", text).strip()
    # Strip a leading "角色：" / "角色名：" style label that some models
    # add even when told not to. Heuristic, not strict — only the
    # first label is removed and only when followed by a colon.
    label_match = re.match(r"^[\u4e00-\u9fffA-Za-z0-9_·\-]{1,16}[:：]\s*", text)
    if label_match:
        text = text[label_match.end():]
    text = _LEADING_QUOTE_RE.sub("", text)
    text = _TRAILING_QUOTE_RE.sub("", text)
    if len(text) > _MAX_REPLY_CHARS:
        text = text[: _MAX_REPLY_CHARS - 1].rstrip() + "…"
    return text.strip()


def _shorten(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)] + "…"
