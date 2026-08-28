"""LLM-backed goal reviewer.

Given the character's active goals and recent conversation, asks the
model to emit a single JSON object containing:

- ``verdicts``: list of ``{id, status, notes}`` for existing active goals
- ``new_goals``: list of ``{content, priority, tags}`` to add

The reviewer is **additive and conservative**: unknown goal ids are
ignored, terminal statuses remain unchanged, and malformed output is
silently dropped. Goal stability is intentional — drift is a greater
failure mode than slow recognition.

Convergence (CF2 / P2b) is a
**prompt-level** discipline on purpose: near-duplicate merging, expiry of
date-bound goals and the active soft cap are semantic judgements, so they
are stated as rules in ``goal/reviewer`` and decided by the model. Nothing
here string-matches goal content. The only thing Python contributes is the
deterministic calendar context the model needs to make those calls — the
local "today" fact and the relative-word → absolute-date anchor table (the
same pair the post-turn writer gets), plus the numeric soft cap.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, tzinfo
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
from kokoro_link.domain.value_objects.timezone import to_timezone
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    render_current_time_fact_lines,
    render_date_anchor_lines,
)
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.llm_output import extract_object_outcome, log_parse_outcome

_LOGGER = logging.getLogger(__name__)

_MAX_NEW_GOALS = 3
_MAX_CONTENT_CHARS = 200
_MAX_NOTES_CHARS = 200
_MAX_TAGS = 5
_MAX_TAG_CHARS = 40

#: Soft ceiling on simultaneously-active goals (plan §2 P2b: "建議 12"). The
#: reviewer is told the number and told what to do when it is reached; the cap
#: is deliberately NOT enforced in Python — deciding *which* goals to merge or
#: retire is exactly the semantic judgement the LLM-first rule reserves for the
#: model. Python only counts.
ACTIVE_GOAL_SOFT_CAP = 12

_ALLOWED_STATUS_VALUES = {s.value for s in CANONICAL_STATUSES}
_REVIEWABLE_STATUS_VALUES = {"active", "paused", "done", "abandoned"}

_ROLE_LABELS: dict[str, str] = {"user": "使用者", "assistant": "角色"}

_TODAY_HEADING = "今天（使用者本地時區）——判斷目標是否過期一律以此為準："


class LLMGoalReviewer(GoalReviewerPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        max_new_goals: int = _MAX_NEW_GOALS,
        feature_key: str | None = None,
        local_tz: tzinfo = timezone.utc,
        active_goal_soft_cap: int = ACTIVE_GOAL_SOFT_CAP,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )
        self._max_new_goals = max_new_goals
        # Site-level fallback timezone; a caller that knows the owning
        # operator's zone passes it per-review (same shape as the post-turn
        # processor). Persistence stays UTC either way.
        self._local_tz = local_tz
        self._active_goal_soft_cap = max(1, active_goal_soft_cap)

    async def review(
        self,
        *,
        character: Character,
        active_goals: list[CharacterGoal],
        recent_messages: list[Message],
        operator_primary_language: str = "zh-TW",
        now: datetime | None = None,
        local_tz: tzinfo | None = None,
    ) -> GoalReviewResult:
        if await self._resolver.is_fake(character=character):
            return GoalReviewResult()
        prompt = _build_prompt(
            character=character,
            active_goals=active_goals,
            recent_messages=recent_messages,
            max_new_goals=self._max_new_goals,
            operator_primary_language=operator_primary_language,
            now=now,
            local_tz=local_tz if local_tz is not None else self._local_tz,
            active_goal_soft_cap=self._active_goal_soft_cap,
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
    now: datetime | None = None,
    local_tz: tzinfo = timezone.utc,
    active_goal_soft_cap: int = ACTIVE_GOAL_SOFT_CAP,
) -> str:
    history_lines = "\n".join(
        f"{_ROLE_LABELS.get(m.role.value, m.role.value)}：{m.content}"
        for m in recent_messages
    )
    # The reviewer must be able to tell a stale goal from a fresh one to apply
    # the soft-cap rule ("優先度最低、最久沒有推進的"), so every line carries the
    # civil day it was created / last advanced on.
    if active_goals:
        goal_lines = "\n".join(
            _render_goal_line(g, local_tz=local_tz) for g in active_goals
        )
    else:
        goal_lines = "（目前沒有中期目標）"
    aspirations = character.aspirations or []
    aspiration_line = "、".join(aspirations) if aspirations else "（未設定）"
    # A reviewer with no clock cannot decide whether 「明早一起出門」 has already
    # passed, so an absent ``now`` still gets the current instant rather than an
    # empty block — a wrong-by-a-timezone today beats no today at all.
    reference_now = now if now is not None else datetime.now(timezone.utc)
    return get_default_loader().render(
        "goal/reviewer",
        # new_goals.content becomes goal.content and notes becomes
        # goal.review_notes — both render in PlayerGoalsPanel.vue, so they
        # must follow the operator's content language (bug B2 class).
        language_hint=render_operator_language_hint(operator_primary_language),
        today_lines="\n".join(
            render_current_time_fact_lines(
                reference_now, local_tz, heading=_TODAY_HEADING, label="現在",
            ),
        ),
        date_anchor_lines="\n".join(
            render_date_anchor_lines(
                reference_now, local_tz, language_tag=operator_primary_language,
            ),
        ),
        character_name=character.name,
        character_summary=character.summary,
        aspirations=aspiration_line,
        goal_lines=goal_lines,
        active_goal_count=len(active_goals),
        active_goal_soft_cap=active_goal_soft_cap,
        history_lines=history_lines,
        status_hint=", ".join(sorted(_ALLOWED_STATUS_VALUES)),
        max_new_goals=max_new_goals,
    )


def _render_goal_line(goal: CharacterGoal, *, local_tz: tzinfo) -> str:
    anchor = goal.last_progressed_at or goal.created_at
    stamp = to_timezone(anchor, local_tz).date().isoformat() if anchor else "未知"
    return (
        f"- id={goal.id} | 優先={goal.priority} | 最後推進={stamp}"
        f" | 內容：{goal.content}"
    )


def _parse_response(
    raw: str,
    *,
    known_goal_ids: set[str],
    max_new_goals: int,
) -> GoalReviewResult:
    outcome = extract_object_outcome(raw)
    log_parse_outcome(_LOGGER, outcome, site="goal.llm_reviewer")
    obj = outcome.value
    if obj is None:
        return GoalReviewResult()

    verdicts = _parse_verdicts(obj.get("verdicts"), known_goal_ids=known_goal_ids)
    new_goals = _parse_new_goals(obj.get("new_goals"), limit=max_new_goals)
    return GoalReviewResult(status_changes=verdicts, new_goals=new_goals)


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
