"""Time-triggered daily goal review (CF2 / COMMITMENT_LIFECYCLE §2 P2a).

Goal review used to hang off chat turn *count* alone
(``ChatService._maybe_schedule_goal_review``, every N user-assistant
exchanges). Proactive messages are not turns, so an account whose character
talks far more than its player never reaches the threshold: the diagnosed
test account accumulated 28 active goals — heavy near-duplicates plus zombie
goals frozen on a date that had long passed — across five days holding only
eight chat turns. A list that is never reviewed cannot converge, and every
one of those goals is injected into the chat / proactive prompt.

This service adds the missing **time** trigger: once per character per civil
day, regardless of conversation volume. The chat cadence stays exactly as it
was; the two coexist.

Three properties are deliberate:

* **The day gate lives in the database.** The claim is a row in the
  ``background_visible_slots`` ledger keyed ``(character, "goal_review",
  local ISO date)`` — the same unique-index CAS the proactive / feed-reply
  passes use. A per-process "last reviewed at" would re-fire once per
  replica and reset on deploy; a DB claim is correct across an arbitrary
  number of API, scheduler and worker processes, is queryable after the
  fact, and needs no schema change.
* **The trigger is idempotent, so any cadence can drive it.** The embedded
  scheduler calls it every tick and the distributed ``goal_review`` due-job
  chain calls it once a day; both funnel through the same claim, so at most
  one review per character per day happens either way. That also means the
  chat path can *record* its review through :meth:`note_reviewed` and
  suppress the day's scheduled pass without either trigger knowing about
  the other.
* **No claim ledger → no daily review.** On the in-memory / no-DB path
  ``visible_slot_port`` is ``None``. Rather than fall back to an in-process
  flag (banned) or run unguarded (an LLM review every 300s tick), the daily
  trigger simply does nothing; the chat cadence is unchanged there.

The service performs no judgement of its own: which goals merge, which have
expired and what to do at the soft cap are all the reviewer LLM's calls (see
``goal/reviewer``). Everything here is scheduling and plumbing.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone, tzinfo
from inspect import signature

from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.goal_reviewer import GoalReviewerPort, GoalReviewResult
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.contracts.visible_slots import (
    SLOT_KIND_GOAL_REVIEW,
    VisibleSlotPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import DEFAULT_PRIMARY_LANGUAGE
from kokoro_link.domain.value_objects.timezone import timezone_for_id

_LOGGER = logging.getLogger(__name__)

#: How much conversation the daily review reads. Matches the chat-triggered
#: path's window so both triggers judge the same kind of evidence. A day with
#: no conversation at all still reviews — merging duplicates and closing goals
#: whose date has passed needs the goal list and today's date, not dialogue.
DEFAULT_RECENT_MESSAGE_LIMIT = 20


def _accepts_keyword(func: object, name: str) -> bool:
    """Whether ``func`` will tolerate being called with keyword ``name``.

    Reviewers that predate a kwarg (the null reviewer, lean test doubles)
    must keep working rather than raise ``TypeError`` into a background tick.
    Unknown signatures are assumed tolerant — the same stance
    ``ChatService`` takes for the identical call."""
    try:
        params = signature(func).parameters
    except (TypeError, ValueError):
        return True
    if name in params:
        return True
    return any(param.kind == param.VAR_KEYWORD for param in params.values())


class DailyGoalReviewService:
    """Runs at most one goal review per character per operator-local day."""

    def __init__(
        self,
        *,
        goal_service,  # noqa: ANN001 - GoalService (avoids an import cycle)
        goal_reviewer: GoalReviewerPort | None,
        conversation_repository: ConversationRepositoryPort | None = None,
        visible_slot_port: VisibleSlotPort | None = None,
        operator_profile_service=None,  # noqa: ANN001 - OperatorProfileService
        local_tz: tzinfo = timezone.utc,
        clock: ClockPort | None = None,
        recent_message_limit: int = DEFAULT_RECENT_MESSAGE_LIMIT,
    ) -> None:
        self._goals = goal_service
        self._reviewer = goal_reviewer
        self._conversations = conversation_repository
        self._slots = visible_slot_port
        self._operator_profiles = operator_profile_service
        self._local_tz = local_tz
        self._clock = clock
        self._recent_message_limit = max(0, recent_message_limit)

    @property
    def enabled(self) -> bool:
        """Whether a daily review can happen at all.

        False when the reviewer, the goal service or the DB-backed claim
        ledger is missing — the caller then skips without touching the DB."""
        return (
            self._goals is not None
            and self._reviewer is not None
            and self._slots is not None
        )

    async def review_if_due(
        self, character: Character, *, now: datetime | None = None,
    ) -> bool:
        """Review ``character``'s goals unless today's slot is already taken.

        Returns whether this call actually ran a review. Never raises: a
        background tick must survive a broken reviewer, a DB blip or an
        operator profile that will not load.
        """
        if not self.enabled:
            return False
        moment = self._resolve_now(now)
        if not await self.note_reviewed(character, now=moment):
            return False
        try:
            await self._run_review(character, now=moment)
        except Exception:
            _LOGGER.exception(
                "daily goal review crashed character=%s", character.id,
            )
            return False
        return True

    async def note_reviewed(
        self, character: Character, *, now: datetime | None = None,
    ) -> bool:
        """Claim today's review slot for ``character``.

        Returns ``True`` when this caller took the claim (nobody had reviewed
        this character today), ``False`` when it was already held or no claim
        ledger is wired. The chat-triggered review calls this purely to record
        that the day is covered, so the scheduled pass does not pay for a
        second review of the same list hours later.
        """
        if self._slots is None:
            return False
        try:
            day = await self._local_day(character, self._resolve_now(now))
            return await self._slots.claim(
                character.id, SLOT_KIND_GOAL_REVIEW, day.isoformat(),
            )
        except Exception:
            # A claim that cannot be recorded must NOT be treated as won —
            # failing closed keeps a DB blip from turning into an unbounded
            # per-tick LLM review.
            _LOGGER.exception(
                "daily goal review: slot claim failed character=%s", character.id,
            )
            return False

    # -- internals ---------------------------------------------------- #

    async def _run_review(self, character: Character, *, now: datetime) -> None:
        active = await self._goals.list_active_goals(character.id)
        if not active:
            # Nothing to converge and no conversation trigger behind this
            # pass — proposing goals out of thin air is the chat path's job.
            return
        recent = await self._recent_messages(character)
        # One operator read serves both the content language and the civil-day
        # timezone — they come from the same profile row.
        operator = await self._operator(character)
        result = await self._call_reviewer(
            character=character,
            active_goals=active,
            recent_messages=recent,
            operator_primary_language=_language_of(operator),
            now=now,
            local_tz=self._timezone_of(operator),
        )
        if not result.status_changes and not result.new_goals:
            return
        await self._goals.apply_review_result(
            character_id=character.id, result=result,
        )

    async def _call_reviewer(
        self,
        *,
        character: Character,
        active_goals,  # noqa: ANN001 - list[CharacterGoal]
        recent_messages,  # noqa: ANN001 - list[Message]
        operator_primary_language: str,
        now: datetime,
        local_tz: tzinfo,
    ) -> GoalReviewResult:
        reviewer = self._reviewer
        assert reviewer is not None
        kwargs = {
            "character": character,
            "active_goals": active_goals,
            "recent_messages": recent_messages,
        }
        for name, value in (
            ("operator_primary_language", operator_primary_language),
            ("now", now),
            ("local_tz", local_tz),
        ):
            if _accepts_keyword(reviewer.review, name):
                kwargs[name] = value
        return await reviewer.review(**kwargs)

    async def _recent_messages(self, character: Character) -> list:
        if self._conversations is None or self._recent_message_limit == 0:
            return []
        try:
            return await self._conversations.recent_messages_for_character(
                character.id,
                limit=self._recent_message_limit,
                exclude_tool_only=True,
            )
        except Exception:
            _LOGGER.exception(
                "daily goal review: history load failed character=%s",
                character.id,
            )
            return []

    async def _operator(self, character: Character):  # noqa: ANN201
        service = self._operator_profiles
        if service is None:
            return None
        user_id = getattr(character, "user_id", None) or "default"
        try:
            return await service.get_for_user(user_id)
        except Exception:
            _LOGGER.exception(
                "daily goal review: operator load failed character=%s",
                character.id,
            )
            return None

    def _timezone_of(self, operator) -> tzinfo:  # noqa: ANN001
        if operator is None:
            return self._local_tz
        try:
            return timezone_for_id(getattr(operator, "timezone_id", None))
        except Exception:
            return self._local_tz

    async def _local_day(self, character: Character, now: datetime) -> date:
        """The character owner's civil day — the review's dedup unit.

        A player in Asia/Taipei and one in America/Los_Angeles must each get
        one review per *their* day, not per UTC day; using UTC would give a
        UTC+8 account two reviews inside one of its own evenings."""
        tz = self._timezone_of(await self._operator(character))
        return now.astimezone(tz).date()

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


def _language_of(operator) -> str:  # noqa: ANN001
    value = (getattr(operator, "primary_language", "") or "").strip()
    return value or DEFAULT_PRIMARY_LANGUAGE
