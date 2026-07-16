"""LLM-backed goal reviewer.

Given the character's active goals and recent conversation, asks the
model to emit a single JSON object containing:

- ``verdicts``: list of ``{id, status, notes}`` for existing active goals
- ``new_goals``: list of ``{content, priority, tags}`` to add

The reviewer is **additive and conservative**: unknown goal ids are
ignored, terminal statuses remain unchanged, and malformed output is
silently dropped. Goal stability is intentional — drift is a greater
failure mode than slow recognition.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.goal_reviewer import (
    GoalReviewerPort,
    GoalReviewResult,
    GoalStatusChange,
    NewGoalProposal,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.value_objects.goal_status import CANONICAL_STATUSES, GoalStatus
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_NEW_GOALS = 3
_MAX_CONTENT_CHARS = 200
_MAX_NOTES_CHARS = 200
_MAX_TAGS = 5
_MAX_TAG_CHARS = 40

_ALLOWED_STATUS_VALUES = {s.value for s in CANONICAL_STATUSES}
_REVIEWABLE_STATUS_VALUES = {"active", "paused", "done", "abandoned"}

_ROLE_LABELS: dict[str, str] = {"user": "使用者", "assistant": "角色"}


class LLMGoalReviewer(GoalReviewerPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        max_new_goals: int = _MAX_NEW_GOALS,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )
        self._max_new_goals = max_new_goals

    async def review(
        self,
        *,
        character: Character,
        active_goals: list[CharacterGoal],
        recent_messages: list[Message],
        operator_primary_language: str = "zh-TW",
    ) -> GoalReviewResult:
        if await self._resolver.is_fake(character=character):
            return GoalReviewResult()
        prompt = _build_prompt(
            character=character,
            active_goals=active_goals,
            recent_messages=recent_messages,
            max_new_goals=self._max_new_goals,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception:
            _LOGGER.exception("Goal reviewer LLM call failed")
            return GoalReviewResult()

        return _parse_response(
            raw,
            known_goal_ids={g.id for g in active_goals},
            max_new_goals=self._max_new_goals,
        )


def _build_prompt(
    *,
    character: Character,
    active_goals: list[CharacterGoal],
    recent_messages: list[Message],
    max_new_goals: int,
    operator_primary_language: str = "zh-TW",
) -> str:
    history_lines = "\n".join(
        f"{_ROLE_LABELS.get(m.role.value, m.role.value)}：{m.content}"
        for m in recent_messages
    )
    if active_goals:
        goal_lines = "\n".join(
            f"- id={g.id} | 優先={g.priority} | 內容：{g.content}"
            for g in active_goals
        )
    else:
        goal_lines = "（目前沒有中期目標）"
    aspirations = character.aspirations or []
    aspiration_line = "、".join(aspirations) if aspirations else "（未設定）"
    return get_default_loader().render(
        "goal/reviewer",
        # new_goals.content becomes goal.content and notes becomes
        # goal.review_notes — both render in PlayerGoalsPanel.vue, so they
        # must follow the operator's content language (bug B2 class).
        language_hint=render_operator_language_hint(operator_primary_language),
        character_name=character.name,
        character_summary=character.summary,
        aspirations=aspiration_line,
        goal_lines=goal_lines,
        history_lines=history_lines,
        status_hint=", ".join(sorted(_ALLOWED_STATUS_VALUES)),
        max_new_goals=max_new_goals,
    )


def _parse_response(
    raw: str,
    *,
    known_goal_ids: set[str],
    max_new_goals: int,
) -> GoalReviewResult:
    obj = _extract_object(raw)
    if obj is None:
        return GoalReviewResult()

    verdicts = _parse_verdicts(obj.get("verdicts"), known_goal_ids=known_goal_ids)
    new_goals = _parse_new_goals(obj.get("new_goals"), limit=max_new_goals)
    return GoalReviewResult(status_changes=verdicts, new_goals=new_goals)


def _extract_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _parse_verdicts(raw: Any, *, known_goal_ids: set[str]) -> list[GoalStatusChange]:
    if not isinstance(raw, list):
        return []
    results: list[GoalStatusChange] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        goal_id = item.get("id")
        status_raw = item.get("status")
        if not isinstance(goal_id, str) or goal_id not in known_goal_ids:
            continue
        if not isinstance(status_raw, str):
            continue
        candidate = status_raw.strip().lower()
        if candidate not in _REVIEWABLE_STATUS_VALUES:
            continue
        notes_raw = item.get("notes")
        notes: str | None = None
        if isinstance(notes_raw, str):
            trimmed = notes_raw.strip()[:_MAX_NOTES_CHARS]
            if trimmed:
                notes = trimmed
        results.append(
            GoalStatusChange(
                goal_id=goal_id,
                new_status=GoalStatus.from_string(candidate),
                notes=notes,
            )
        )
    return results


def _parse_new_goals(raw: Any, *, limit: int) -> list[NewGoalProposal]:
    if not isinstance(raw, list):
        return []
    results: list[NewGoalProposal] = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        content_raw = item.get("content")
        if not isinstance(content_raw, str):
            continue
        content = content_raw.strip()[:_MAX_CONTENT_CHARS]
        if not content:
            continue
        priority = _coerce_priority(item.get("priority"))
        tags = _coerce_tags(item.get("tags"))
        results.append(NewGoalProposal(content=content, priority=priority, tags=tags))
    return results


def _coerce_priority(raw: Any) -> int:
    if isinstance(raw, bool):
        return 3
    if isinstance(raw, (int, float)):
        value = int(raw)
        return max(1, min(5, value))
    return 3


def _coerce_tags(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    cleaned: list[str] = []
    for tag in raw:
        if not isinstance(tag, (str, int, float)):
            continue
        text = str(tag).strip().lower()[:_MAX_TAG_CHARS]
        if text:
            cleaned.append(text)
        if len(cleaned) >= _MAX_TAGS:
            break
    return tuple(cleaned)
