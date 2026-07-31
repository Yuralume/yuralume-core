"""Time-triggered daily goal review (CF2 / COMMITMENT_LIFECYCLE §2 P2a).

The defect this covers: goal review only fired every N *chat turns*, so an
account whose character speaks proactively but whose player rarely types
never reached the threshold and piled up 28 unreviewed active goals. These
tests pin the replacement trigger — one review per character per operator-
local day, gated by a DB claim rather than any per-process flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.goal_review_service import (
    DailyGoalReviewService,
)
from kokoro_link.contracts.goal_reviewer import (
    GoalReviewResult,
    GoalStatusChange,
    NewGoalProposal,
)
from kokoro_link.contracts.visible_slots import SLOT_KIND_GOAL_REVIEW
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.goal_status import GoalStatus
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import (
    NullPostTurnProcessor,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

NOW = datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc)


@dataclass(slots=True)
class _Character:
    id: str = "char-1"
    user_id: str = "user-1"


@dataclass(slots=True)
class _Operator:
    primary_language: str = "ja"
    timezone_id: str = "Asia/Taipei"


class _OperatorProfiles:
    def __init__(self, operator: _Operator | None = None) -> None:
        self.operator = operator if operator is not None else _Operator()

    async def get_for_user(self, user_id: str) -> _Operator:  # noqa: ARG002
        return self.operator


class _Slots:
    """In-memory stand-in for the ``background_visible_slots`` unique index."""

    def __init__(self) -> None:
        self.claims: set[tuple[str, str, str]] = set()
        self.attempts: list[tuple[str, str, str]] = []

    async def claim(self, character_id: str, kind: str, slot: str) -> bool:
        key = (character_id, kind, slot)
        self.attempts.append(key)
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    async def release(self, character_id: str, kind: str, slot: str) -> bool:
        self.claims.discard((character_id, kind, slot))
        return True

    async def prune(self, *, now, retention_days: int = 7) -> int:  # noqa: ANN001, ARG002
        return 0


class _BrokenSlots(_Slots):
    async def claim(self, character_id: str, kind: str, slot: str) -> bool:
        raise RuntimeError("db down")


@dataclass
class _Goals:
    active: list[CharacterGoal] = field(default_factory=list)
    applied: list[GoalReviewResult] = field(default_factory=list)

    async def list_active_goals(self, character_id: str):  # noqa: ANN202, ARG002
        return list(self.active)

    async def apply_review_result(self, *, character_id, result) -> None:  # noqa: ANN001, ARG002
        self.applied.append(result)


class _Reviewer:
    def __init__(self, result: GoalReviewResult | None = None) -> None:
        self.result = result if result is not None else GoalReviewResult()
        self.calls: list[dict] = []

    async def review(
        self,
        *,
        character,  # noqa: ANN001
        active_goals,  # noqa: ANN001
        recent_messages,  # noqa: ANN001
        operator_primary_language: str = "zh-TW",
        now=None,  # noqa: ANN001
        local_tz=None,  # noqa: ANN001
    ) -> GoalReviewResult:
        self.calls.append({
            "character": character,
            "active_goals": active_goals,
            "recent_messages": recent_messages,
            "operator_primary_language": operator_primary_language,
            "now": now,
            "local_tz": local_tz,
        })
        return self.result


class _LegacyReviewer:
    """A reviewer predating the language / calendar kwargs."""

    def __init__(self) -> None:
        self.calls = 0

    async def review(self, *, character, active_goals, recent_messages):  # noqa: ANN001
        self.calls += 1
        return GoalReviewResult()


class _Conversations:
    def __init__(self, messages: list[Message] | None = None) -> None:
        self.messages = messages or []
        self.calls: list[int] = []

    async def recent_messages_for_character(
        self, character_id: str, *, limit: int, exclude_tool_only: bool = False,  # noqa: ARG002
    ) -> list[Message]:
        self.calls.append(limit)
        return list(self.messages)


def _goal(goal_id: str = "g1", content: str = "practice") -> CharacterGoal:
    return CharacterGoal(
        id=goal_id,
        character_id="char-1",
        content=content,
        status=GoalStatus.ACTIVE,
        priority=3,
        origin="manual",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )


def _service(
    *,
    goals: _Goals | None = None,
    reviewer=None,  # noqa: ANN001
    slots=None,  # noqa: ANN001
    conversations: _Conversations | None = None,
    operator: _Operator | None = None,
) -> DailyGoalReviewService:
    return DailyGoalReviewService(
        goal_service=goals if goals is not None else _Goals(active=[_goal()]),
        goal_reviewer=reviewer if reviewer is not None else _Reviewer(),
        conversation_repository=(
            conversations if conversations is not None else _Conversations()
        ),
        visible_slot_port=slots if slots is not None else _Slots(),
        operator_profile_service=_OperatorProfiles(operator),
    )


class TestDailyTrigger:
    @pytest.mark.asyncio
    async def test_reviews_with_zero_chat_turns(self) -> None:
        # The whole point of the time trigger: no conversation at all still
        # gets the goal list reviewed.
        reviewer = _Reviewer()
        goals = _Goals(active=[_goal()])
        service = _service(
            goals=goals, reviewer=reviewer, conversations=_Conversations([]),
        )

        assert await service.review_if_due(_Character(), now=NOW) is True
        assert len(reviewer.calls) == 1
        assert reviewer.calls[0]["recent_messages"] == []

    @pytest.mark.asyncio
    async def test_second_call_same_day_is_skipped(self) -> None:
        reviewer = _Reviewer()
        service = _service(reviewer=reviewer)
        character = _Character()

        assert await service.review_if_due(character, now=NOW) is True
        later = NOW + timedelta(hours=6)
        assert await service.review_if_due(character, now=later) is False
        assert len(reviewer.calls) == 1

    @pytest.mark.asyncio
    async def test_next_local_day_reviews_again(self) -> None:
        reviewer = _Reviewer()
        service = _service(reviewer=reviewer)
        character = _Character()

        await service.review_if_due(character, now=NOW)
        await service.review_if_due(character, now=NOW + timedelta(days=1))
        assert len(reviewer.calls) == 2

    @pytest.mark.asyncio
    async def test_dedup_is_per_character(self) -> None:
        reviewer = _Reviewer()
        service = _service(reviewer=reviewer)

        await service.review_if_due(_Character(id="a"), now=NOW)
        await service.review_if_due(_Character(id="b"), now=NOW)
        assert len(reviewer.calls) == 2

    @pytest.mark.asyncio
    async def test_slot_is_the_operator_local_day_not_utc(self) -> None:
        # 2026-07-29 16:30Z is already 2026-07-30 in Asia/Taipei. Bucketing on
        # UTC would give a UTC+8 player two reviews inside one of their days.
        slots = _Slots()
        service = _service(slots=slots)
        character = _Character()

        evening = datetime(2026, 7, 29, 16, 30, tzinfo=timezone.utc)
        await service.review_if_due(character, now=evening)
        assert slots.claims == {
            ("char-1", SLOT_KIND_GOAL_REVIEW, "2026-07-30"),
        }

    @pytest.mark.asyncio
    async def test_claim_ledger_is_the_dedup_not_process_state(self) -> None:
        # A second process (fresh service instance) sharing the same ledger
        # must find the day already taken — the multi-instance guarantee.
        slots = _Slots()
        reviewer_a = _Reviewer()
        reviewer_b = _Reviewer()
        character = _Character()

        assert await _service(
            reviewer=reviewer_a, slots=slots,
        ).review_if_due(character, now=NOW) is True
        assert await _service(
            reviewer=reviewer_b, slots=slots,
        ).review_if_due(character, now=NOW) is False
        assert len(reviewer_a.calls) == 1
        assert reviewer_b.calls == []


class TestReviewBody:
    @pytest.mark.asyncio
    async def test_applies_verdicts_and_new_goals(self) -> None:
        result = GoalReviewResult(
            status_changes=[
                GoalStatusChange(goal_id="g1", new_status=GoalStatus.ABANDONED),
            ],
            new_goals=[NewGoalProposal(content="next")],
        )
        goals = _Goals(active=[_goal()])
        service = _service(goals=goals, reviewer=_Reviewer(result))

        await service.review_if_due(_Character(), now=NOW)
        assert goals.applied == [result]

    @pytest.mark.asyncio
    async def test_no_verdicts_writes_nothing(self) -> None:
        goals = _Goals(active=[_goal()])
        service = _service(goals=goals, reviewer=_Reviewer(GoalReviewResult()))

        await service.review_if_due(_Character(), now=NOW)
        assert goals.applied == []

    @pytest.mark.asyncio
    async def test_no_active_goals_skips_the_llm(self) -> None:
        reviewer = _Reviewer()
        service = _service(goals=_Goals(active=[]), reviewer=reviewer)

        assert await service.review_if_due(_Character(), now=NOW) is True
        assert reviewer.calls == []

    @pytest.mark.asyncio
    async def test_passes_calendar_and_language_context(self) -> None:
        reviewer = _Reviewer()
        service = _service(reviewer=reviewer)

        await service.review_if_due(_Character(), now=NOW)
        call = reviewer.calls[0]
        assert call["now"] == NOW
        assert call["operator_primary_language"] == "ja"
        # Asia/Taipei operator → the reviewer judges expiry on UTC+8.
        assert NOW.astimezone(call["local_tz"]).date().isoformat() == "2026-07-29"

    @pytest.mark.asyncio
    async def test_reads_the_cross_channel_timeline(self) -> None:
        conversations = _Conversations(
            [Message(role=MessageRole.USER, content="hi")],
        )
        reviewer = _Reviewer()
        service = _service(reviewer=reviewer, conversations=conversations)

        await service.review_if_due(_Character(), now=NOW)
        assert conversations.calls == [20]
        assert len(reviewer.calls[0]["recent_messages"]) == 1

    @pytest.mark.asyncio
    async def test_legacy_reviewer_without_new_kwargs_still_runs(self) -> None:
        reviewer = _LegacyReviewer()
        service = _service(reviewer=reviewer)

        assert await service.review_if_due(_Character(), now=NOW) is True
        assert reviewer.calls == 1


class TestFailSoft:
    @pytest.mark.asyncio
    async def test_no_slot_ledger_disables_the_daily_trigger(self) -> None:
        # No DB → no durable dedup. Running unguarded would fire an LLM review
        # on every 300s tick; falling back to a per-process flag is banned.
        reviewer = _Reviewer()
        service = DailyGoalReviewService(
            goal_service=_Goals(active=[_goal()]),
            goal_reviewer=reviewer,
            visible_slot_port=None,
        )
        assert service.enabled is False
        assert await service.review_if_due(_Character(), now=NOW) is False
        assert reviewer.calls == []

    @pytest.mark.asyncio
    async def test_claim_failure_does_not_run_the_review(self) -> None:
        reviewer = _Reviewer()
        service = _service(reviewer=reviewer, slots=_BrokenSlots())

        assert await service.review_if_due(_Character(), now=NOW) is False
        assert reviewer.calls == []

    @pytest.mark.asyncio
    async def test_reviewer_crash_is_contained(self) -> None:
        class _Boom:
            async def review(self, **kwargs):  # noqa: ANN003
                raise RuntimeError("boom")

        service = _service(reviewer=_Boom())
        assert await service.review_if_due(_Character(), now=NOW) is False

    @pytest.mark.asyncio
    async def test_history_load_failure_still_reviews(self) -> None:
        class _BrokenConversations(_Conversations):
            async def recent_messages_for_character(self, *a, **kw):  # noqa: ANN002, ANN003
                raise RuntimeError("db down")

        reviewer = _Reviewer()
        service = _service(reviewer=reviewer, conversations=_BrokenConversations())
        assert await service.review_if_due(_Character(), now=NOW) is True
        assert reviewer.calls[0]["recent_messages"] == []


class TestNoteReviewed:
    @pytest.mark.asyncio
    async def test_recording_a_chat_review_suppresses_the_daily_pass(self) -> None:
        # The chat turn-count trigger stays; it just marks the day covered so
        # the scheduled pass does not pay for a second review hours later.
        reviewer = _Reviewer()
        slots = _Slots()
        service = _service(reviewer=reviewer, slots=slots)
        character = _Character()

        assert await service.note_reviewed(character, now=NOW) is True
        assert await service.review_if_due(character, now=NOW) is False
        assert reviewer.calls == []

    @pytest.mark.asyncio
    async def test_note_reviewed_without_ledger_is_a_noop(self) -> None:
        service = DailyGoalReviewService(
            goal_service=_Goals(), goal_reviewer=_Reviewer(), visible_slot_port=None,
        )
        assert await service.note_reviewed(_Character(), now=NOW) is False


def _chat_service(daily: DailyGoalReviewService, *, goals: _Goals, reviewer):  # noqa: ANN001, ANN202
    """Minimal ChatService wired only for the goal-review path.

    The chat turn-count trigger and the daily trigger are two doors into the
    same review; the bridge below is what stops them from both paying for it
    on the same day, and it is a one-line call inside a broad method — easy
    to lose in a refactor without a test standing on it.
    """
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    return ChatService(
        character_repository=InMemoryCharacterRepository(),
        conversation_repository=InMemoryConversationRepository(),
        memory_repository=InMemoryMemoryRepository(),
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        goal_service=goals,
        goal_reviewer=reviewer,
        daily_goal_review_service=daily,
    )


class TestChatBridge:
    """``ChatService._do_goal_review`` → ``DailyGoalReviewService``."""

    @pytest.mark.asyncio
    async def test_chat_review_claims_todays_slot(self) -> None:
        slots = _Slots()
        goals = _Goals(active=[_goal()])
        reviewer = _Reviewer()
        daily = _service(goals=goals, reviewer=reviewer, slots=slots)
        chat = _chat_service(daily, goals=goals, reviewer=reviewer)
        character = Character.create(
            name="Aki", summary="", personality=[], interests=[],
            speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            ),
        )
        await chat._character_repository.save(character)

        await chat._do_goal_review(
            character_id=character.id,
            conversation=Conversation.start(character_id=character.id),
        )

        assert reviewer.calls, "the chat path must still run its own review"
        assert (
            character.id, SLOT_KIND_GOAL_REVIEW,
        ) in {(cid, kind) for cid, kind, _ in slots.claims}
        # …and the day is now covered, so the scheduled pass skips it.
        assert await daily.review_if_due(character) is False

    @pytest.mark.asyncio
    async def test_chat_review_survives_a_broken_claim_ledger(self) -> None:
        """A DB blip on the bookkeeping call must not lose the review result —
        the claim is an optimisation, the review is the product."""
        goals = _Goals(active=[_goal()])
        reviewer = _Reviewer(
            GoalReviewResult(
                status_changes=[
                    GoalStatusChange(goal_id="g1", new_status=GoalStatus.DONE),
                ],
            ),
        )
        daily = _service(goals=goals, reviewer=reviewer, slots=_BrokenSlots())
        chat = _chat_service(daily, goals=goals, reviewer=reviewer)
        character = Character.create(
            name="Aki", summary="", personality=[], interests=[],
            speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            ),
        )
        await chat._character_repository.save(character)

        await chat._do_goal_review(
            character_id=character.id,
            conversation=Conversation.start(character_id=character.id),
        )

        assert goals.applied, "review result must still be applied"
