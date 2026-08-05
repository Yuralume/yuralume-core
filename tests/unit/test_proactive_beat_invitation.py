"""OP3 — a central beat that is waiting invites the player in.

ARC_PLAYER_POSITION_PLAN §3.4. A beat marked ``operator_position=
central`` is a scene *about* the player, so the autonomous scan walks
past it (OP2-B) and it waits. This suite pins four things:

1. the waiting beat reaches both proactive prompts, position and note
   included;
2. the invitation is *material*, not a template — the block hands the
   model facts and explicitly keeps silence on the table;
3. every existing gate still runs first and still blocks (a waiting
   scene buys no exemption from scene-pause, idle, quota or cooldown);
4. **characterization** — while no central beat is *due*, the prompts
   are byte-identical to their pre-OP3 form. Not only for the legacy
   arcs whose fields read back unjudged: judging a beat, or scheduling
   a central one for next week, must not move a single byte either.
   The invitation is material that appears when a scene is actually
   owed, never a standing hint that one is coming.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.story_arc import (
    ARC_ACTIVE,
    BEAT_REALIZED,
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
    TENSION_CLIMAX,
    TENSION_SETUP,
    StoryArc,
    StoryArcBeat,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_LAYER_BEAT,
    StorySceneSession,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import (
    HeuristicProactiveGate,
)
from kokoro_link.infrastructure.proactive.llm_decider import LLMProactiveDecider
from kokoro_link.infrastructure.proactive.llm_intention_judge import (
    LLMProactiveIntentionJudge,
)
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_scene_sessions import (
    InMemoryStorySceneSessionRepository,
)
from tests.unit._messaging_harness import build_messaging_harness, create_character

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 18, 14, 30, tzinfo=timezone.utc)
TODAY = date(2026, 4, 18)

_DECIDER_JSON = (
    '{"should_send": false, "reason": "inspection only", "message": null}'
)
_JUDGE_JSON = (
    '{"should_consume_slot": false, "inner_motive": "", '
    '"conversation_purpose": "", "expected_reply": "", "risk": "", '
    '"best_timing": "later", "reason": "inspection only"}'
)


class _StubModel(ChatModelPort):
    def __init__(self, response: str) -> None:
        self._response = response
        self.captured_prompt: str | None = None

    async def generate(self, prompt: str) -> str:
        self.captured_prompt = prompt
        return self._response

    async def generate_stream(
        self, prompt: str,
    ) -> AsyncIterator[str]:  # pragma: no cover
        yield self._response


# --- fixtures ---------------------------------------------------------


def _beat(
    *,
    sequence: int,
    scheduled: date,
    title: str,
    summary: str,
    tension: str = TENSION_SETUP,
    operator_position: str | None = None,
    operator_note: str | None = None,
    status: str | None = None,
) -> StoryArcBeat:
    beat = StoryArcBeat.create(
        arc_id="arc-1",
        sequence=sequence,
        scheduled_date=scheduled,
        title=title,
        summary=summary,
        tension=tension,
        operator_position=operator_position,
        operator_note=operator_note,
    )
    return beat if status is None else beat.with_status(status)


def _arc(*beats: StoryArcBeat, character_id: str = "char-1") -> StoryArc:
    return StoryArc.create(
        id="arc-1",
        character_id=character_id,
        title="第二次告白",
        premise="她一直沒說出口的那件事，快要瞞不住了。",
        theme="romance",
        start_date=TODAY - timedelta(days=3),
        end_date=TODAY + timedelta(days=4),
        beats=beats,
        status=ARC_ACTIVE,
    )


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="咖啡店打工的大學生。",
        personality=["溫柔", "容易想太多"],
        interests=["吉他", "咖啡"],
        speaking_style="輕柔自然",
        boundaries=[],
        state=CharacterState(
            emotion="有點緊張",
            affection=70,
            fatigue=20,
            trust=70,
            energy=80,
        ),
        proactive_enabled=True,
    )


def _context(
    *,
    active_arc: StoryArc | None = None,
    upcoming_beats: tuple[StoryArcBeat, ...] = (),
    beat_awaiting_player: StoryArcBeat | None = None,
) -> ProactiveContext:
    return ProactiveContext(
        character=_character(),
        trigger=ProactiveTrigger.TICK,
        now=NOW,
        current_activity=None,
        upcoming_activities=[],
        schedule=None,
        idle_minutes=180.0,
        sent_today=0,
        last_proactive_at=None,
        active_arc=active_arc,
        upcoming_beats=upcoming_beats,
        beat_awaiting_player=beat_awaiting_player,
    )


# --- decider prompt ---------------------------------------------------


async def test_decider_prompt_carries_the_waiting_scene_and_its_note() -> None:
    waiting = _beat(
        sequence=2,
        scheduled=TODAY - timedelta(days=2),
        title="頂樓的那句話",
        summary="她把一直藏著的話說出口。",
        tension=TENSION_CLIMAX,
        operator_position=OPERATOR_POSITION_CENTRAL,
        operator_note="她要向你坦白",
    )
    model = _StubModel(_DECIDER_JSON)

    await LLMProactiveDecider(model=model).decide(
        _context(active_arc=_arc(waiting), beat_awaiting_player=waiting),
    )

    prompt = model.captured_prompt or ""
    assert "頂樓的那句話" in prompt
    assert "她把一直藏著的話說出口。" in prompt
    assert "2026-04-16" in prompt
    assert "已經等了 2 天" in prompt
    # Position and note are stated as their own facts, not paraphrased.
    assert (
        "· 對方在這場戲裡的位置：這場戲是關於對方的，沒有他就演不下去"
    ) in prompt
    assert "· 對方的戲份：她要向你坦白" in prompt


async def test_decider_invitation_is_a_motive_not_a_notification() -> None:
    """LLM-first: facts plus an open door, never a canned message.

    The block must (a) ban system-notice phrasing and out-of-world
    vocabulary, (b) forbid playing the scene inside the invite (SC1-D:
    no invented player actions), and (c) leave silence available so a
    waiting beat cannot become a forced push.
    """
    waiting = _beat(
        sequence=0,
        scheduled=TODAY,
        title="那通沒接到的電話",
        summary="她需要你在場才敢面對。",
        operator_position=OPERATOR_POSITION_CENTRAL,
    )
    model = _StubModel(_DECIDER_JSON)

    await LLMProactiveDecider(model=model).decide(
        _context(active_arc=_arc(waiting), beat_awaiting_player=waiting),
    )

    prompt = model.captured_prompt or ""
    assert "寫成系統通知、公告、選單或任務提示" in prompt
    assert "會讓人出戲" in prompt
    assert "不要在這則訊息裡把那場戲演掉" in prompt
    assert "不要替對方說話" in prompt
    assert "保持沉默一樣是好選擇" in prompt
    # Same-day beats read as "today", not as an overdue debt.
    assert "就是今天" in prompt


async def _decider_prompt_for(context: ProactiveContext) -> str:
    model = _StubModel(_DECIDER_JSON)
    await LLMProactiveDecider(model=model).decide(context)
    return model.captured_prompt or ""


async def test_judging_a_beat_does_not_move_one_byte_of_the_decider_prompt() -> None:
    """The acceptance criterion, taken literally.

    "No due central beat ⇒ zero difference from today" has to hold for
    arcs planned *after* OP0 as well, not only for legacy arcs whose
    fields happen to read back empty. So the forward feed carries no
    player position at all: two arcs identical except for the new
    fields must produce the same prompt, byte for byte.
    """
    plain = (
        _beat(
            sequence=0, scheduled=TODAY,
            title="早班的咖啡店", summary="她一個人開店。",
        ),
        _beat(
            sequence=1, scheduled=TODAY + timedelta(days=1),
            title="回家的路上", summary="她繞了遠路。",
        ),
    )
    judged = (
        _beat(
            sequence=0, scheduled=TODAY,
            title="早班的咖啡店", summary="她一個人開店。",
            operator_position=OPERATOR_POSITION_ABSENT,
        ),
        _beat(
            sequence=1, scheduled=TODAY + timedelta(days=1),
            title="回家的路上", summary="她繞了遠路。",
            operator_position=OPERATOR_POSITION_PRESENT,
            operator_note="你只是陪她走一段",
        ),
    )

    plain_prompt = await _decider_prompt_for(
        _context(active_arc=_arc(*plain), upcoming_beats=plain),
    )
    judged_prompt = await _decider_prompt_for(
        _context(active_arc=_arc(*judged), upcoming_beats=judged),
    )

    assert judged_prompt == plain_prompt
    assert "  · 2026-04-18 早班的咖啡店 — 她一個人開店。\n" in judged_prompt
    assert "  · 2026-04-19 回家的路上 — 她繞了遠路。" in judged_prompt


async def test_a_central_beat_that_is_not_due_yet_leaks_nothing() -> None:
    """The failure mode the forward-feed suffix created.

    A central beat next week is *not* waiting — the dispatcher leaves
    ``beat_awaiting_player`` empty for it precisely so no invitation is
    owed. If the forward feed still whispered "she cannot go on without
    you" plus the note, the model could be moved to invite days early,
    which is the added proactive pressure OP3 promised not to create.
    """
    future = _beat(
        sequence=0,
        scheduled=TODAY + timedelta(days=5),
        title="頂樓的那句話",
        summary="她把一直藏著的話說出口。",
        tension=TENSION_CLIMAX,
        operator_position=OPERATOR_POSITION_CENTRAL,
        operator_note="她要向你坦白",
    )

    prompt = await _decider_prompt_for(
        _context(
            active_arc=_arc(future),
            upcoming_beats=(future,),
            beat_awaiting_player=None,
        ),
    )

    # The beat is still ordinary forward-feed colour, in its old shape.
    assert "  · 2026-04-23 頂樓的那句話 — 她把一直藏著的話說出口。" in prompt
    # …and nothing about the player's place in it, nor any invitation.
    assert "沒有他就演不下去" not in prompt
    assert "她要向你坦白" not in prompt
    assert "對方在這場戲裡的位置" not in prompt
    assert "有一場戲在等對方進場" not in prompt


async def test_a_due_central_beat_keeps_the_feed_plain_and_the_invite_full() -> None:
    """Same beat, two roles: colour in the feed, material in the invite.

    A beat that comes due today is in both places at once. The listing
    keeps its pre-OP3 shape; everything OP3 added lives in the
    invitation block, so the player's position is stated exactly once
    and only because a scene is genuinely owed.
    """
    due = _beat(
        sequence=0,
        scheduled=TODAY,
        title="頂樓的那句話",
        summary="她把一直藏著的話說出口。",
        tension=TENSION_CLIMAX,
        operator_position=OPERATOR_POSITION_CENTRAL,
        operator_note="她要向你坦白",
    )

    prompt = await _decider_prompt_for(
        _context(
            active_arc=_arc(due),
            upcoming_beats=(due,),
            beat_awaiting_player=due,
        ),
    )

    assert "  · 2026-04-18 頂樓的那句話 — 她把一直藏著的話說出口。\n" in prompt
    assert "有一場戲在等對方進場" in prompt
    assert (
        "· 對方在這場戲裡的位置：這場戲是關於對方的，沒有他就演不下去"
    ) in prompt
    assert prompt.count("她要向你坦白") == 1
    assert prompt.count("對方在這場戲裡的位置") == 1


async def test_decider_prompt_unchanged_when_nothing_waits() -> None:
    """Characterization: the arc block every existing deployment sees."""
    first = _beat(
        sequence=0, scheduled=TODAY, title="起點", summary="踏出第一步。",
    )
    second = _beat(
        sequence=1,
        scheduled=TODAY + timedelta(days=1),
        title="小插曲",
        summary="意料外的轉折。",
    )
    model = _StubModel(_DECIDER_JSON)

    await LLMProactiveDecider(model=model).decide(
        _context(active_arc=_arc(first, second), upcoming_beats=(first, second)),
    )

    prompt = model.captured_prompt or ""
    assert (
        "你目前在進行的故事線：第二次告白（主題：romance）\n"
        "- 前提：她一直沒說出口的那件事，快要瞞不住了。\n"
        "- 接下來的節拍：\n"
        "  · 2026-04-18 起點 — 踏出第一步。\n"
        "  · 2026-04-19 小插曲 — 意料外的轉折。"
    ) in prompt
    assert "有一場戲在等對方進場" not in prompt
    assert "對方在這場戲裡的位置" not in prompt


# --- intention judge prompt -------------------------------------------


async def test_judge_prompt_offers_the_waiting_scene_as_a_candidate_motive() -> None:
    waiting = _beat(
        sequence=1,
        scheduled=TODAY - timedelta(days=1),
        title="雨停之後",
        summary="她想把那件事講完。",
        operator_position=OPERATOR_POSITION_CENTRAL,
        operator_note="她在等你回答",
    )
    model = _StubModel(_JUDGE_JSON)

    await LLMProactiveIntentionJudge(model=model).judge(
        _context(active_arc=_arc(waiting), beat_awaiting_player=waiting),
    )

    prompt = model.captured_prompt or ""
    assert "雨停之後" in prompt
    assert "  · 對方的戲份：她在等你回答" in prompt
    assert (
        "  · 對方在這場戲裡的位置：這場戲是關於對方的，沒有他就演不下去"
    ) in prompt
    assert "昨天就該發生了，已經等了 1 天" in prompt
    assert "是真實的內在動機，不是刷存在感" in prompt
    # Still a candidate, never a mandate — quota discipline is untouched.
    assert "它不是必發理由" in prompt


async def _judge_prompt_for(context: ProactiveContext) -> str:
    model = _StubModel(_JUDGE_JSON)
    await LLMProactiveIntentionJudge(model=model).judge(context)
    return model.captured_prompt or ""


async def test_judge_arc_block_unchanged_when_nothing_waits() -> None:
    """Characterization: the judge's arc section is title + premise only.

    Byte-identical against the same arc with the new fields set — the
    judge has no beat listing to leak them into, and must not grow one:
    a not-yet-due central beat is not a reason to spend a slot.
    """
    plain = _beat(
        sequence=0, scheduled=TODAY,
        title="早班的咖啡店", summary="她一個人開店。",
    )
    judged = _beat(
        sequence=0, scheduled=TODAY,
        title="早班的咖啡店", summary="她一個人開店。",
        operator_position=OPERATOR_POSITION_PRESENT,
        operator_note="你只是陪著",
    )
    future_central = _beat(
        sequence=1, scheduled=TODAY + timedelta(days=5),
        title="頂樓的那句話", summary="她把一直藏著的話說出口。",
        operator_position=OPERATOR_POSITION_CENTRAL,
        operator_note="她要向你坦白",
    )

    baseline = await _judge_prompt_for(
        _context(active_arc=_arc(plain), upcoming_beats=(plain,)),
    )
    with_positions = await _judge_prompt_for(
        _context(
            active_arc=_arc(judged, future_central),
            upcoming_beats=(judged, future_central),
        ),
    )

    assert with_positions == baseline
    assert (
        "目前故事線：\n- 第二次告白：她一直沒說出口的那件事，快要瞞不住了。"
    ) in baseline
    assert "在等對方進場" not in with_positions
    assert "她要向你坦白" not in with_positions


# --- dispatcher selection ---------------------------------------------


@dataclass
class _FakeArcService:
    arc: StoryArc | None

    async def ensure_active_arc(self, character, *, today=None, auto_start=True):  # noqa: ANN001, ANN201
        return self.arc


class _CapturingDecider(ProactiveDeciderPort):
    def __init__(self) -> None:
        self.contexts: list[ProactiveContext] = []

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.contexts.append(context)
        return ProactiveDecision(False, "inspection only", None)


async def _character_for(harness):  # noqa: ANN001
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=NOW - timedelta(hours=3),
        ),
        proactive_enabled=True,
        accepts_web_proactive=True,
    )
    await harness.character_repository.save(updated)
    return updated


async def _seeds(character_id: str):  # noqa: ANN201
    repository = InMemoryCharacterOperatorRelationshipSeedRepository()
    await repository.save(
        CharacterOperatorRelationshipSeed(
            character_id=character_id,
            operator_id=DEFAULT_OPERATOR_ID,
            relationship_label="剛認識",
            proactive_permission=True,
            proactive_cadence_hint="一天最多一次",
        ),
    )
    return repository


def _dispatcher(harness, *, decider, arc_service, seeds, sessions=None):  # noqa: ANN001
    return ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=decider,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        story_arc_service=arc_service,
        relationship_seed_repository=seeds,
        story_scene_sessions=sessions,
    )


async def _evaluate_with(arc: StoryArc | None, *, sessions=None):  # noqa: ANN001
    harness = build_messaging_harness()
    character = await _character_for(harness)
    decider = _CapturingDecider()
    dispatcher = _dispatcher(
        harness,
        decider=decider,
        arc_service=_FakeArcService(arc=arc),
        seeds=await _seeds(character.id),
        sessions=sessions,
    )
    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=NOW,
    )
    return attempt, decider


async def test_an_overdue_central_beat_surfaces_even_though_it_left_the_forward_feed() -> None:
    """The whole reason the field is not a slice of ``upcoming_beats``.

    ``forward_beats`` is anchored at ``>= today``, so the day after a
    beat comes due it vanishes from the arc block — exactly when it has
    been waiting longest and an invitation matters most.
    """
    waiting = _beat(
        sequence=0,
        scheduled=TODAY - timedelta(days=2),
        title="頂樓的那句話",
        summary="她把一直藏著的話說出口。",
        operator_position=OPERATOR_POSITION_CENTRAL,
    )
    _, decider = await _evaluate_with(_arc(waiting))

    context = decider.contexts[0]
    assert context.beat_awaiting_player is not None
    assert context.beat_awaiting_player.title == "頂樓的那句話"
    assert context.upcoming_beats == ()


async def test_the_earliest_waiting_scene_wins() -> None:
    older = _beat(
        sequence=0,
        scheduled=TODAY - timedelta(days=3),
        title="更早的那場",
        summary="先排的。",
        operator_position=OPERATOR_POSITION_CENTRAL,
    )
    newer = _beat(
        sequence=1,
        scheduled=TODAY - timedelta(days=1),
        title="後來的那場",
        summary="後排的。",
        operator_position=OPERATOR_POSITION_CENTRAL,
    )
    _, decider = await _evaluate_with(_arc(newer, older))

    assert decider.contexts[0].beat_awaiting_player.title == "更早的那場"


async def test_a_future_central_beat_is_not_waiting_yet() -> None:
    """Nothing is owed before the day arrives — inviting early would be
    pure added proactive pressure."""
    future = _beat(
        sequence=0,
        scheduled=TODAY + timedelta(days=2),
        title="還沒到的那場",
        summary="下週的事。",
        operator_position=OPERATOR_POSITION_CENTRAL,
    )
    _, decider = await _evaluate_with(_arc(future))

    context = decider.contexts[0]
    assert context.beat_awaiting_player is None
    # It is still ordinary arc colour in the forward feed.
    assert [b.title for b in context.upcoming_beats] == ["還沒到的那場"]


async def test_a_played_central_beat_is_no_longer_waiting() -> None:
    played = _beat(
        sequence=0,
        scheduled=TODAY - timedelta(days=1),
        title="已經演過的那場",
        summary="演完了。",
        operator_position=OPERATOR_POSITION_CENTRAL,
        status=BEAT_REALIZED,
    )
    _, decider = await _evaluate_with(_arc(played))

    assert decider.contexts[0].beat_awaiting_player is None


async def test_unjudged_and_non_central_beats_never_wait() -> None:
    """Characterization for every arc that exists today: unjudged beats
    (``None``) must not be read as "the player is essential", or every
    stock arc would start inviting on the day this shipped."""
    unjudged = _beat(
        sequence=0, scheduled=TODAY - timedelta(days=1),
        title="未判", summary="舊資料。",
    )
    absent = _beat(
        sequence=1, scheduled=TODAY, title="沒有你的戲", summary="她獨自完成。",
        operator_position=OPERATOR_POSITION_ABSENT,
    )
    present = _beat(
        sequence=2, scheduled=TODAY, title="你在旁邊", summary="你只是陪著。",
        operator_position=OPERATOR_POSITION_PRESENT,
    )
    _, decider = await _evaluate_with(_arc(unjudged, absent, present))

    assert decider.contexts[0].beat_awaiting_player is None


async def test_no_arc_service_leaves_the_field_empty() -> None:
    _, decider = await _evaluate_with(None)
    assert decider.contexts[0].beat_awaiting_player is None


# --- gates are untouched ----------------------------------------------


async def test_a_waiting_scene_does_not_lift_the_scene_pause() -> None:
    """SC1-E still wins: a character inside a scene does not message from
    outside it, waiting beat or not — and the decider is never reached,
    so no LLM budget is spent either."""
    waiting = _beat(
        sequence=0,
        scheduled=TODAY - timedelta(days=1),
        title="等著的那場",
        summary="她在等你。",
        operator_position=OPERATOR_POSITION_CENTRAL,
    )
    harness = build_messaging_harness()
    character = await _character_for(harness)
    sessions = InMemoryStorySceneSessionRepository()
    await sessions.add(
        StorySceneSession.open_scene(
            character_id=character.id,
            conversation_id="conv-1",
            source_layer=SCENE_LAYER_BEAT,
            beat_id="b1",
            title="正在演的那場",
            opened_at=NOW - timedelta(minutes=10),
        ),
    )
    decider = _CapturingDecider()
    dispatcher = _dispatcher(
        harness,
        decider=decider,
        arc_service=_FakeArcService(arc=_arc(waiting)),
        seeds=await _seeds(character.id),
        sessions=sessions,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK, now=NOW,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert attempt.reason == "story scene in progress"
    assert decider.contexts == []


async def test_a_waiting_scene_does_not_lift_the_cheap_gate() -> None:
    """The idle gate blocks mid-conversation pings. A waiting beat buys
    no exemption: gate order and verdicts are exactly as before."""
    waiting = _beat(
        sequence=0,
        scheduled=TODAY - timedelta(days=1),
        title="等著的那場",
        summary="她在等你。",
        operator_position=OPERATOR_POSITION_CENTRAL,
    )
    harness = build_messaging_harness()
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    busy = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            # The user spoke a minute ago: mid-conversation.
            last_active_at=NOW - timedelta(minutes=1),
        ),
        proactive_enabled=True,
        accepts_web_proactive=True,
    )
    await harness.character_repository.save(busy)
    decider = _CapturingDecider()
    dispatcher = _dispatcher(
        harness,
        decider=decider,
        arc_service=_FakeArcService(arc=_arc(waiting)),
        seeds=await _seeds(busy.id),
        sessions=None,
    )

    attempt = await dispatcher.evaluate(
        character_id=busy.id, trigger=ProactiveTrigger.TICK, now=NOW,
    )

    assert attempt.outcome == ProactiveOutcome.GATE_BLOCKED
    assert "idle threshold" in attempt.reason
    assert decider.contexts == []
