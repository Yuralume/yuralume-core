"""F5 — one repair per conversation, not one per lie.

HV4 makes every dishonest chat turn owe a durable repair row. That is
right for the one-off case and wrong for the case that actually produces
several in a row: a capability that keeps failing while the player keeps
asking. Three turns overclaim, three rows land, three due times land
minutes apart, and the character delivers a burst of near-identical
apologies — spending precisely the credibility the feature exists to
protect.

So a claim caught while the conversation still has an unreleased repair
open is merged into it. The tests are grouped by the way that can go
wrong:

* the merge itself — a second lie joins the first one's row and **both**
  claims survive, which is the whole difference between merging and the
  de-duplication D6 forbids;
* the races — another audit merging first, a release worker already
  holding the row, a row already due. Each one ends with a second row
  rather than a lost claim;
* the untouched case — a single lie behaves exactly as it did before F5,
  down to the anchor, the due time and the counters.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kokoro_link.application.services.chat_outcome_claim_auditor import (
    REPAIR_STATUS_COALESCED,
    REPAIR_STATUS_QUEUED,
    ChatOutcomeClaimAuditor,
)
from kokoro_link.application.services.outcome_claim_guard import (
    OutcomeClaimGuard,
)
from kokoro_link.contracts.outcome_claim import (
    OutcomeClaimEvidence,
    OutcomeClaimVerdict,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.pending_follow_up import (
    HONESTY_REPAIR_DEFER_REASON,
    SCHEDULED_PROMISE_DEFER_REASON,
    PendingFollowUp,
    PendingFollowUpKind,
    PendingFollowUpStatus,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.prompt.outcome_claim_honesty import (
    REPAIR_INTENT_MAX_CHARS,
    merge_repair_promise_intent,
    render_repair_promise_intent,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)

_CONV_ID = "conv-1"
_CHAR_ID = "char-1"


def _now() -> datetime:
    return datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)


def _character(cid: str = _CHAR_ID) -> Character:
    return replace(
        Character.create(
            name="Aki", summary="", personality=[], interests=[],
            speaking_style="", boundaries=[],
            state=CharacterState(
                emotion="neutral", affection=50, fatigue=20, trust=50,
                energy=70,
            ),
        ),
        id=cid,
        allowed_tools=["fake_image"],
    )


class _ScriptedJudge:
    """Answers with the queued verdicts; the last one repeats."""

    def __init__(self, *verdicts: OutcomeClaimVerdict) -> None:
        self._verdicts = list(verdicts) or [OutcomeClaimVerdict.ok()]
        self.seen: list[OutcomeClaimEvidence] = []

    async def judge(
        self, *, message_text, evidence, character=None,
        operator_primary_language="",
    ):
        self.seen.append(evidence)
        if len(self._verdicts) > 1:
            return self._verdicts.pop(0)
        return self._verdicts[0]


class _RecordingEnqueuer:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def enqueue(self, row, *, now=None) -> bool:
        self.rows.append(row)
        return True


def _auditor(
    judge: _ScriptedJudge, repository,
) -> tuple[ChatOutcomeClaimAuditor, OutcomeClaimGuard]:
    guard = OutcomeClaimGuard(judge=judge)
    return (
        ChatOutcomeClaimAuditor(
            guard=guard, pending_follow_up_repository=repository,
        ),
        guard,
    )


async def _audit(
    auditor: ChatOutcomeClaimAuditor,
    *,
    turn: str = "turn-1",
    now: datetime | None = None,
    conversation_id: str = _CONV_ID,
    character: Character | None = None,
    enqueuer=None,
    turn_started_at: datetime | None = None,
):
    return await auditor.audit(
        character=character or _character(),
        conversation_id=conversation_id,
        turn_record_id=turn,
        assistant_text="拍好囉，照片傳給你了！",
        offered_tools=("fake_image",),
        delivered_attachments=0,
        release_enqueuer=enqueuer,
        turn_started_at=turn_started_at,
        now=now or _now(),
    )


# --- the merge -----------------------------------------------------------


@pytest.mark.asyncio
async def test_second_lie_joins_the_first_repair_and_both_claims_survive(
) -> None:
    """The headline. One failing capability, two turns, ONE apology — and
    the apology names both things, because merging a claim away would be
    the same silent loss as dropping the row."""
    repo = InMemoryPendingFollowUpRepository()
    judge = _ScriptedJudge(
        OutcomeClaimVerdict.blocked(("照片傳給你了",)),
        OutcomeClaimVerdict.blocked(("這張也畫好了",)),
    )
    auditor, guard = _auditor(judge, repo)

    first = await _audit(auditor, turn="turn-1", now=_now())
    second = await _audit(
        auditor, turn="turn-2", now=_now() + timedelta(seconds=30),
    )

    assert first.repair_status == REPAIR_STATUS_QUEUED
    assert second.repair_status == REPAIR_STATUS_COALESCED
    assert second.repaired
    assert second.repair_follow_up_id == first.repair_follow_up_id

    rows = await repo.list_open_for_character(_CHAR_ID)
    assert len(rows) == 1
    assert "照片傳給你了" in rows[0].promise_intent
    assert "這張也畫好了" in rows[0].promise_intent
    assert guard.counters.chat_repair_queued == 1
    assert guard.counters.chat_repair_coalesced == 1
    assert guard.counters.chat_repair_missed == 0


@pytest.mark.asyncio
async def test_merging_does_not_push_the_repair_further_away() -> None:
    """A repair the character already owes must come due when it was
    going to. Otherwise a player who keeps asking keeps postponing the
    apology they are owed — the failure mode dressed as a feature."""
    repo = InMemoryPendingFollowUpRepository()
    auditor, _ = _auditor(
        _ScriptedJudge(
            OutcomeClaimVerdict.blocked(("照片傳給你了",)),
            OutcomeClaimVerdict.blocked(("新聞我查過了",)),
        ),
        repo,
    )

    await _audit(auditor, turn="turn-1", now=_now())
    due_after_first = (await repo.list_open_for_character(_CHAR_ID))[0]
    await _audit(
        auditor, turn="turn-2", now=_now() + timedelta(seconds=30),
    )

    row = (await repo.list_open_for_character(_CHAR_ID))[0]
    assert row.scheduled_for == due_after_first.scheduled_for
    # And the anchor stays the FIRST turn's: undo is last-turn-only, so
    # the row can only be reached by an undo of a turn whose claims it
    # still holds.
    assert row.turn_record_id == "turn-1"


@pytest.mark.asyncio
async def test_merge_mints_no_second_release_job() -> None:
    """The row already has one. A second job for the same row is a second
    delivery attempt for the release path to de-duplicate."""
    repo = InMemoryPendingFollowUpRepository()
    enqueuer = _RecordingEnqueuer()
    auditor, _ = _auditor(
        _ScriptedJudge(
            OutcomeClaimVerdict.blocked(("照片傳給你了",)),
            OutcomeClaimVerdict.blocked(("新聞我查過了",)),
        ),
        repo,
    )

    await _audit(auditor, turn="turn-1", now=_now(), enqueuer=enqueuer)
    await _audit(
        auditor, turn="turn-2", now=_now() + timedelta(seconds=30),
        enqueuer=enqueuer,
    )

    assert len(enqueuer.rows) == 1


@pytest.mark.asyncio
async def test_the_same_sentence_caught_twice_owes_one_line() -> None:
    """Not a skip: the row already quotes this exact claim, so there is
    nothing to add and nothing lost by adding nothing."""
    repo = InMemoryPendingFollowUpRepository()
    auditor, guard = _auditor(
        _ScriptedJudge(OutcomeClaimVerdict.blocked(("照片傳給你了",))), repo,
    )

    await _audit(auditor, turn="turn-1", now=_now())
    result = await _audit(
        auditor, turn="turn-2", now=_now() + timedelta(seconds=30),
    )

    rows = await repo.list_open_for_character(_CHAR_ID)
    assert len(rows) == 1
    assert rows[0].promise_intent.count("照片傳給你了") == 1
    assert result.repair_status == REPAIR_STATUS_COALESCED
    assert guard.counters.chat_repair_coalesced == 1


@pytest.mark.asyncio
async def test_another_conversation_gets_its_own_repair() -> None:
    """Scoped by conversation and by character. A repair is a thing the
    character says in one thread; carrying it to another would apologise
    where nothing was claimed."""
    repo = InMemoryPendingFollowUpRepository()
    auditor, _ = _auditor(
        _ScriptedJudge(OutcomeClaimVerdict.blocked(("照片傳給你了",))), repo,
    )

    await _audit(auditor, turn="turn-1", now=_now())
    await _audit(
        auditor, turn="turn-2", now=_now() + timedelta(seconds=30),
        conversation_id="conv-2",
    )

    rows = await repo.list_open_for_character(_CHAR_ID)
    assert {row.conversation_id for row in rows} == {_CONV_ID, "conv-2"}


# --- the races -----------------------------------------------------------


class _RacingRepository(InMemoryPendingFollowUpRepository):
    """A second auditor lands its merge between our read and our swap.

    The interleaving the compare-and-swap exists for, made deterministic:
    the real thing needs two audit tasks whose awaits happen to line up,
    which no unit test can be trusted to reproduce.
    """

    def __init__(self, interloper_claim: str) -> None:
        super().__init__()
        self._interloper_claim = interloper_claim
        self.raced = False

    async def coalesce_promise_intent(
        self, follow_up_id, *, expected_intent, new_intent, now,
    ) -> bool:
        if not self.raced:
            self.raced = True
            current = await self.get(follow_up_id)
            assert current is not None
            other = merge_repair_promise_intent(
                current.promise_intent, (self._interloper_claim,),
            )
            assert other is not None
            await super().coalesce_promise_intent(
                follow_up_id,
                expected_intent=current.promise_intent,
                new_intent=other,
                now=now,
            )
        return await super().coalesce_promise_intent(
            follow_up_id,
            expected_intent=expected_intent,
            new_intent=new_intent,
            now=now,
        )


@pytest.mark.asyncio
async def test_losing_the_swap_opens_a_row_instead_of_dropping_the_claim(
) -> None:
    """PF's lesson, applied. The loser of a read-modify-write must not
    write over the winner, and must not give up either — so it takes the
    fallback the feature always had: a row of its own."""
    repo = _RacingRepository("我剛剛查過了")
    auditor, guard = _auditor(
        _ScriptedJudge(
            OutcomeClaimVerdict.blocked(("照片傳給你了",)),
            OutcomeClaimVerdict.blocked(("行程我改好了",)),
        ),
        repo,
    )

    await _audit(auditor, turn="turn-1", now=_now())
    second = await _audit(
        auditor, turn="turn-2", now=_now() + timedelta(seconds=30),
    )

    assert repo.raced
    assert second.repair_status == REPAIR_STATUS_QUEUED
    rows = await repo.list_open_for_character(_CHAR_ID)
    assert len(rows) == 2
    owed = "\n".join(row.promise_intent for row in rows)
    # Nobody's claim was overwritten: the winner's, the interloper's and
    # the loser's are all owed by some row.
    assert "照片傳給你了" in owed
    assert "我剛剛查過了" in owed
    assert "行程我改好了" in owed
    assert guard.counters.chat_repair_missed == 0


@pytest.mark.asyncio
async def test_a_repair_being_released_is_not_merged_into() -> None:
    """``resolving`` means a worker is composing from a copy it already
    read. Anything merged in now is written over the moment that compose
    finishes."""
    repo = InMemoryPendingFollowUpRepository()
    auditor, _ = _auditor(
        _ScriptedJudge(
            OutcomeClaimVerdict.blocked(("照片傳給你了",)),
            OutcomeClaimVerdict.blocked(("行程我改好了",)),
        ),
        repo,
    )

    await _audit(auditor, turn="turn-1", now=_now())
    open_row = (await repo.list_open_for_character(_CHAR_ID))[0]
    await repo.save(open_row.marked_resolving(now=_now()))

    second = await _audit(
        auditor, turn="turn-2", now=_now() + timedelta(seconds=30),
    )

    assert second.repair_status == REPAIR_STATUS_QUEUED
    rows = await repo.list_open_for_character(_CHAR_ID)
    assert len(rows) == 2
    assert any("行程我改好了" in row.promise_intent for row in rows)


@pytest.mark.asyncio
async def test_a_repair_already_due_is_not_merged_into() -> None:
    """Still ``queued``, but the release tick may already be holding it —
    and that path writes the whole row back without looking."""
    repo = InMemoryPendingFollowUpRepository()
    auditor, _ = _auditor(
        _ScriptedJudge(
            OutcomeClaimVerdict.blocked(("照片傳給你了",)),
            OutcomeClaimVerdict.blocked(("行程我改好了",)),
        ),
        repo,
    )

    await _audit(auditor, turn="turn-1", now=_now())
    # Well past the row's due time: the dispatcher's next ``list_due``
    # would pick it up.
    second = await _audit(
        auditor, turn="turn-2", now=_now() + timedelta(minutes=10),
    )

    assert second.repair_status == REPAIR_STATUS_QUEUED
    assert len(await repo.list_open_for_character(_CHAR_ID)) == 2


@pytest.mark.asyncio
async def test_a_repair_born_after_this_turn_started_is_not_merged_into(
) -> None:
    """B-3. The merge target has to predate the merging turn.

    Both audits are background tasks behind an unbounded judge call, so
    they can finish out of order: a later turn's audit opens the row
    first, and this (earlier) turn's slow audit arrives to find a repair
    row anchored on a turn that has not happened yet from its point of
    view. Merging into it puts this turn's claim somewhere undo of *that*
    turn deletes wholesale — losing a lie that is still on screen, which
    is the D6 breach the whole feature exists to prevent.

    ``turn_started_at`` is what makes the difference visible: the row is
    newer, so it is not in this turn's pre-turn snapshot, so no undo can
    rewind a merge out of it. Second row instead — the safe fallback.
    """
    repo = InMemoryPendingFollowUpRepository()
    auditor, guard = _auditor(
        _ScriptedJudge(
            OutcomeClaimVerdict.blocked(("這張也畫好了",)),
            OutcomeClaimVerdict.blocked(("照片傳給你了",)),
        ),
        repo,
    )

    # The later turn's audit lands first and opens the row.
    later = await _audit(
        auditor, turn="turn-2", now=_now(),
        turn_started_at=_now() - timedelta(seconds=20),
    )
    # This turn began *before* that row existed; its judge only answers
    # now.
    earlier = await _audit(
        auditor, turn="turn-1", now=_now() + timedelta(seconds=5),
        turn_started_at=_now() - timedelta(minutes=2),
    )

    assert later.repair_status == REPAIR_STATUS_QUEUED
    assert earlier.repair_status == REPAIR_STATUS_QUEUED
    assert earlier.repair_follow_up_id != later.repair_follow_up_id
    rows = await repo.list_open_for_character(_CHAR_ID)
    assert len(rows) == 2
    assert {row.turn_record_id for row in rows} == {"turn-1", "turn-2"}
    # Two rows is the redundancy the fallback accepts, never a miss.
    assert guard.counters.chat_repair_missed == 0


@pytest.mark.asyncio
async def test_without_a_turn_start_the_merge_precondition_stands_down(
) -> None:
    """No journal on this deployment means no undo to stay consistent
    with — so the B-3 filter must not quietly disable F5 there."""
    repo = InMemoryPendingFollowUpRepository()
    auditor, _ = _auditor(
        _ScriptedJudge(
            OutcomeClaimVerdict.blocked(("照片傳給你了",)),
            OutcomeClaimVerdict.blocked(("這張也畫好了",)),
        ),
        repo,
    )

    await _audit(auditor, turn="turn-1", now=_now())
    second = await _audit(
        auditor, turn="turn-2", now=_now() + timedelta(seconds=30),
    )

    assert second.repair_status == REPAIR_STATUS_COALESCED
    assert len(await repo.list_open_for_character(_CHAR_ID)) == 1


@pytest.mark.asyncio
async def test_a_full_repair_row_spills_into_a_second_one() -> None:
    """The row's brief is one field with a length cap. When the next
    claim will not fit, it gets a row — the alternative is a merge that
    reports success while quietly dropping what it could not fit."""
    repo = InMemoryPendingFollowUpRepository()
    auditor, guard = _auditor(
        _ScriptedJudge(
            OutcomeClaimVerdict.blocked(tuple(
                f"我已經把第{i}件事情整個做完了而且成果也一起傳過去給你了喔真的完成了"
                for i in range(6)
            )),
            OutcomeClaimVerdict.blocked(tuple(
                f"還有第{i}件我也一併弄好了成品剛剛就發出去了你應該已經收到了才對"
                for i in range(6)
            )),
        ),
        repo,
    )

    await _audit(auditor, turn="turn-1", now=_now())
    second = await _audit(
        auditor, turn="turn-2", now=_now() + timedelta(seconds=30),
    )

    assert second.repair_status == REPAIR_STATUS_QUEUED
    rows = await repo.list_open_for_character(_CHAR_ID)
    assert len(rows) == 2
    assert any("還有第0件" in row.promise_intent for row in rows)
    assert guard.counters.chat_repair_missed == 0


@pytest.mark.asyncio
async def test_an_ordinary_promise_row_is_never_merged_into() -> None:
    """A promise the character made to the player is not a repair, and
    folding an apology into it would rewrite something the player was
    actually told. Recognised by the stamped mark, not by its prose."""
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(PendingFollowUp.new_promise(
        character_id=_CHAR_ID,
        conversation_id=_CONV_ID,
        promise_intent="十點叫玩家起床",
        scheduled_for=_now() + timedelta(hours=1),
        now=_now(),
    ))
    auditor, _ = _auditor(
        _ScriptedJudge(OutcomeClaimVerdict.blocked(("照片傳給你了",))), repo,
    )

    result = await _audit(auditor, turn="turn-1", now=_now())

    assert result.repair_status == REPAIR_STATUS_QUEUED
    rows = await repo.list_open_for_character(_CHAR_ID)
    assert len(rows) == 2
    wakeup = [r for r in rows if r.defer_reason == SCHEDULED_PROMISE_DEFER_REASON]
    assert wakeup[0].promise_intent == "十點叫玩家起床"


# --- unchanged for a single lie ------------------------------------------


@pytest.mark.asyncio
async def test_a_single_lie_behaves_exactly_as_before() -> None:
    """The regression fence. F5 only ever adds a second path; the first
    one — the only one most turns take — keeps every property HV4 gave
    it."""
    repo = InMemoryPendingFollowUpRepository()
    auditor, guard = _auditor(
        _ScriptedJudge(OutcomeClaimVerdict.blocked(("照片傳給你了",))), repo,
    )

    result = await _audit(auditor, turn="turn-1", now=_now())

    rows = await repo.list_open_for_character(_CHAR_ID)
    assert len(rows) == 1
    row = rows[0]
    assert result.repair_status == REPAIR_STATUS_QUEUED
    assert row.kind == PendingFollowUpKind.SCHEDULED_PROMISE
    assert row.status == PendingFollowUpStatus.QUEUED
    assert row.turn_record_id == "turn-1"
    assert row.scheduled_for > _now()
    assert row.is_honesty_repair
    assert row.defer_reason == HONESTY_REPAIR_DEFER_REASON
    assert guard.counters.chat_repair_queued == 1
    assert guard.counters.chat_repair_coalesced == 0


# --- the renderer's half --------------------------------------------------


def test_merge_appends_the_new_claim_and_keeps_the_old_one() -> None:
    base = render_repair_promise_intent(("照片傳給你了",))

    merged = merge_repair_promise_intent(base, ("新聞我查過了",))

    assert merged is not None
    assert "照片傳給你了" in merged
    assert "新聞我查過了" in merged
    assert len(merged) <= REPAIR_INTENT_MAX_CHARS
    # The instruction lines are still the head, so the closing "say so
    # when the tool fails" sentence can never be the thing a cap eats.
    assert merged.splitlines()[0] == base.splitlines()[0]


def test_merge_of_an_already_quoted_claim_changes_nothing() -> None:
    base = render_repair_promise_intent(("照片傳給你了",))

    assert merge_repair_promise_intent(base, ("照片傳給你了",)) == base


def test_merge_that_would_not_fit_refuses_rather_than_truncating() -> None:
    """``None`` is the caller's cue to open a second row. Truncating
    would drop a claim while looking like it had been recorded."""
    base = render_repair_promise_intent(tuple(
        f"我已經把第{i}件事情整個做完了而且成果也一起傳過去給你了喔真的完成了"
        for i in range(6)
    ))
    assert len(base) <= REPAIR_INTENT_MAX_CHARS

    assert merge_repair_promise_intent(base, tuple(
        f"還有第{i}件我也一併弄好了成品剛剛就發出去了你應該已經收到了才對"
        for i in range(6)
    )) is None


def test_merge_replaces_the_no_claims_placeholder_with_real_sentences(
) -> None:
    """A row opened by a verdict that quoted nothing carries a "go and
    re-read what you said" placeholder. Once there are real sentences,
    the placeholder is strictly worse than they are."""
    base = render_repair_promise_intent(())

    merged = merge_repair_promise_intent(base, ("照片傳給你了",))

    assert merged is not None
    assert "照片傳給你了" in merged
    assert "稽核沒有摘出具體句子" not in merged


def test_merge_into_a_blank_intent_refuses() -> None:
    assert merge_repair_promise_intent("", ("照片傳給你了",)) is None
