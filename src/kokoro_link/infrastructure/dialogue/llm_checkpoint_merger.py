"""LLM-backed cumulative dialogue-checkpoint merger (DH3).

Sibling of :class:`~kokoro_link.infrastructure.dialogue.llm_summarizer.
LLMDialogueSummarizer`, and deliberately not a subclass of it. The two
answer different questions:

``LLMDialogueSummarizer``
    "what has been going on lately?" — stateless, restates a window,
    read by schedule / arc / proactive planners, thrown away after.
this class
    "what does the character remember?" — folds a new window into an
    existing memory and has to decide what has stopped being true.

Sharing an implementation would mean one prompt trying to satisfy both
contracts, and every tuning change to either would silently move the
other. They share a package and, for now, a *routing* key.

**Residual — the routing key.** The container passes
``dialogue_summary``, so an operator who pins a cheap model for planner
blurbs has thereby pinned it for the character's long-term memory of the
relationship too, where a weak model's mistakes compound instead of
washing out within the turn. The two want separate keys; minting one
regenerates ``contracts/feature-key-manifest.json`` and the Cloud User
service's bundled copy, which is a cross-repo contract change DH3 does
not take on.

Failure is always an empty ``DialogueCheckpointMergeResult``. The caller
(the checkpoint updater) reads that as "keep the last-good checkpoint" —
never as "the summary is now empty", and never as a reason to widen the
prompt back out to raw turns (D4 reverses the old failure direction,
which made a flaky provider produce a *longer* prompt).
"""

from __future__ import annotations

import logging
from typing import Final

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.dialogue_checkpoint import (
    DialogueCheckpointMergeResult,
    DialogueCheckpointMergerPort,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.infrastructure.llm.cloud_refusal import (
    log_auxiliary_llm_failure,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

MAX_SUMMARY_CHARS: Final[int] = 1200
"""Hard ceiling on a checkpoint, enforced by truncation as well as asked
for in the prompt.

Twice the one-shot summariser's 600 because this text covers a whole
relationship rather than the last few turns, and still a ceiling because
an unbounded cumulative summary is how "compression" quietly becomes
"the transcript again". The prompt is told the same number so the model
composes to fit instead of being cut mid-sentence.
"""

MAX_TURNS_IN_PROMPT: Final[int] = 120
"""Backlog turns handed to one merge call.

The updater already caps the backlog by token budget; this is the
second, blunter belt for a pathological case (a pair that went months
between merges) so one call can never carry an unbounded transcript.
"""

MAX_CHARS_PER_TURN: Final[int] = 400
"""Per-line clip, matching the one-shot summariser. A turn longer than
this contributes its opening — enough to know what it was about."""

_EMPTY_MARKERS: Final[tuple[str, ...]] = ("（無明顯脈絡）", "(無明顯脈絡)")
"""What the prompt asks for when there is nothing worth recording.

Treated as failure-shaped rather than stored: writing it into the
checkpoint would put the words 無明顯脈絡 into the character's prompt as
if that were the memory.
"""


class LLMDialogueCheckpointMerger(DialogueCheckpointMergerPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
        max_summary_chars: int = MAX_SUMMARY_CHARS,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )
        self._max_summary_chars = max(1, int(max_summary_chars))

    async def merge(
        self,
        *,
        character: Character,
        previous_summary: str,
        messages: list[Message],
    ) -> DialogueCheckpointMergeResult:
        useful = [m for m in messages if (m.content or "").strip()]
        if not useful:
            return DialogueCheckpointMergeResult.failed()
        # ``resolve`` rather than ``generate`` so the model id that
        # actually served the call rides back with the text. The active
        # model can change between turns, and a checkpoint stamped with
        # whatever was configured *later* would be an audit field that
        # lies precisely when someone is trying to find out why a
        # summary reads badly.
        model, model_id = await self._resolver.resolve(character=character)
        if await self._resolver.is_fake(character=character):
            return DialogueCheckpointMergeResult.failed()
        prompt = self._build_prompt(
            character=character,
            previous_summary=previous_summary,
            messages=useful,
        )
        kwargs = {} if model_id is None else {"model": model_id}
        try:
            raw = await model.generate(prompt, **kwargs)
        except Exception as exc:
            log_auxiliary_llm_failure(
                _LOGGER, exc, "Dialogue checkpoint merge LLM call failed",
            )
            return DialogueCheckpointMergeResult.failed()
        summary = (raw or "").strip()
        if not summary or summary in _EMPTY_MARKERS:
            return DialogueCheckpointMergeResult.failed()
        return DialogueCheckpointMergeResult(
            summary=summary[: self._max_summary_chars],
            model=model_id or getattr(model, "provider_id", ""),
        )

    def _build_prompt(
        self,
        *,
        character: Character,
        previous_summary: str,
        messages: list[Message],
    ) -> str:
        tail = messages[-MAX_TURNS_IN_PROMPT:]
        transcript = "\n".join(
            _format_line(character, message) for message in tail
        )
        return get_default_loader().render(
            "dialogue/checkpoint_merge",
            character_name=character.name,
            previous_summary=(previous_summary or "").strip() or "（尚無）",
            transcript=transcript,
            max_chars=str(self._max_summary_chars),
        )


def _format_line(character: Character, message: Message) -> str:
    role_label = (
        "使用者" if message.role is MessageRole.USER else character.name
    )
    text = (message.content or "").strip().replace("\n", " ")
    if len(text) > MAX_CHARS_PER_TURN:
        text = text[:MAX_CHARS_PER_TURN] + "…"
    return f"{role_label}：{text}"


__all__ = ["LLMDialogueCheckpointMerger", "MAX_SUMMARY_CHARS"]
