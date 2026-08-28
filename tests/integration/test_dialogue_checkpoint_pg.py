"""The dialogue-checkpoint repository against a real PostgreSQL.

The in-memory twin models the compare-and-swap faithfully, which is
exactly why this file has to exist: a model of a CAS proves the *caller*
handles a lost race, not that the SQL does. What is only provable here:

* the predicated ``UPDATE`` really is atomic across two connections —
  the second writer's predicate is re-evaluated after the first commits,
  so it sees ``rowcount == 0`` rather than overwriting;
* the first-write ``INSERT`` collides on the composite primary key
  rather than silently duplicating the pair;
* the composite key, the timezone-aware columns and the ``stale``
  server default round-trip through the ORM at all.

Everything else about the checkpoint is unit-tested next door.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.domain.entities.dialogue_checkpoint import DialogueCheckpoint
from kokoro_link.infrastructure.persistence.models import CharacterRow
from kokoro_link.infrastructure.persistence.sa_dialogue_checkpoint_repository import (
    SADialogueCheckpointRepository,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
OPERATOR_ID = "default"
CHARACTER_ID = "char-dh3"


def _message(content: str, minutes_before: int) -> Message:
    return Message(
        role=MessageRole.USER,
        content=content,
        created_at=datetime(
            2026, 8, 25, 11, 60 - minutes_before % 60, tzinfo=timezone.utc,
        ),
    )


def _checkpoint(summary: str, boundary: Message) -> DialogueCheckpoint:
    return DialogueCheckpoint.create(
        character_id=CHARACTER_ID,
        operator_id=OPERATOR_ID,
        summary_text=summary,
        boundary=boundary,
        now=NOW,
    )


async def _seed_character(session_factory) -> None:
    """A minimal ``characters`` row so the FK has a parent.

    The ORM row rather than the character repository: this suite is
    about one table, and a full ``Character`` aggregate would let an
    unrelated schema change break tests that have nothing to do with it.
    Every other column carries a default.
    """
    async with session_factory() as session:
        session.add(
            CharacterRow(id=CHARACTER_ID, user_id=OPERATOR_ID, name="小悠"),
        )
        await session.commit()


async def test_a_checkpoint_round_trips_through_the_schema(
    session_factory,
) -> None:
    await _seed_character(session_factory)
    repository = SADialogueCheckpointRepository(session_factory)
    boundary = _message("邊界訊息", 30)

    assert await repository.save(
        _checkpoint("第一份累積摘要", boundary), expected_message_key=None,
    )

    stored = await repository.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    assert stored is not None
    assert stored.summary_text == "第一份累積摘要"
    assert stored.covers_until_created_at == boundary.created_at
    assert stored.covers_until_created_at.tzinfo is not None
    assert stored.stale is False
    assert stored.covers(boundary)


async def test_a_second_insert_for_the_same_pair_loses(
    session_factory,
) -> None:
    """The pair *is* the primary key, so "there was no row" can only be
    claimed once. The loser is told, not raised at."""
    await _seed_character(session_factory)
    repository = SADialogueCheckpointRepository(session_factory)
    first = _message("第一則", 30)
    second = _message("第二則", 20)

    assert await repository.save(
        _checkpoint("先到的", first), expected_message_key=None,
    )
    assert not await repository.save(
        _checkpoint("後到的", second), expected_message_key=None,
    )

    stored = await repository.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    assert stored.summary_text == "先到的"


async def test_the_update_only_lands_on_the_cursor_it_read(
    session_factory,
) -> None:
    """The replica race, as SQL sees it.

    Both writers read cursor A. One commits and moves it to B. The
    other's predicate no longer matches, so its ``rowcount`` is zero and
    its summary — computed against a cursor that no longer exists — is
    dropped rather than written over the winner's.
    """
    await _seed_character(session_factory)
    repository = SADialogueCheckpointRepository(session_factory)
    start = _message("起點", 40)
    assert await repository.save(
        _checkpoint("起始摘要", start), expected_message_key=None,
    )

    read = await repository.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    shared_expectation = read.covers_until_message_key

    winner = _checkpoint("勝者摘要", _message("勝者邊界", 30))
    loser = _checkpoint("敗者摘要", _message("敗者邊界", 25))

    assert await repository.save(
        winner, expected_message_key=shared_expectation,
    )
    assert not await repository.save(
        loser, expected_message_key=shared_expectation,
    )

    stored = await repository.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    assert stored.summary_text == "勝者摘要"
    assert stored.covers_until_message_key == winner.covers_until_message_key


async def test_mark_stale_latches_without_a_cas(session_factory) -> None:
    """One-way, towards the safer state: the only writer that clears it
    is the rebuild the flag asks for."""
    await _seed_character(session_factory)
    repository = SADialogueCheckpointRepository(session_factory)
    await repository.save(
        _checkpoint("摘要", _message("邊界", 30)), expected_message_key=None,
    )

    assert await repository.mark_stale(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID, now=NOW,
    )
    stored = await repository.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    assert stored.stale is True
    assert stored.summary_text == "摘要"


async def test_mark_stale_on_a_missing_row_is_false_not_an_error(
    session_factory,
) -> None:
    repository = SADialogueCheckpointRepository(session_factory)
    assert not await repository.mark_stale(
        character_id="nobody", operator_id=OPERATOR_ID, now=NOW,
    )


async def test_two_operators_keep_separate_checkpoints(
    session_factory,
) -> None:
    """The key is the pair. One character talking to two accounts holds
    two summaries, and neither can overwrite the other."""
    await _seed_character(session_factory)
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO operator_profiles "
                "(id, display_name, aliases_json, pronouns, email, "
                "password_hash, is_admin, created_at, updated_at) "
                "VALUES ('other', '另一位', '[]', NULL, NULL, NULL, FALSE, "
                ":now, :now)"
            ),
            {"now": NOW},
        )
        await session.commit()

    repository = SADialogueCheckpointRepository(session_factory)
    mine = _checkpoint("我的摘要", _message("我的邊界", 30))
    theirs = DialogueCheckpoint.create(
        character_id=CHARACTER_ID,
        operator_id="other",
        summary_text="他們的摘要",
        boundary=_message("他們的邊界", 20),
        now=NOW,
    )

    assert await repository.save(mine, expected_message_key=None)
    assert await repository.save(theirs, expected_message_key=None)

    assert (await repository.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )).summary_text == "我的摘要"
    assert (await repository.get(
        character_id=CHARACTER_ID, operator_id="other",
    )).summary_text == "他們的摘要"
    assert len(await repository.list_for_character(CHARACTER_ID)) == 2


async def test_deleting_the_character_takes_its_checkpoints(
    session_factory,
) -> None:
    await _seed_character(session_factory)
    repository = SADialogueCheckpointRepository(session_factory)
    await repository.save(
        _checkpoint("摘要", _message("邊界", 30)), expected_message_key=None,
    )

    assert await repository.delete_for_character(CHARACTER_ID) == 1
    assert await repository.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    ) is None


async def test_the_stale_latch_survives_a_writer_that_never_saw_it(
    session_factory,
) -> None:
    """The half of the predicate the cursor cannot express.

    ``mark_stale`` moves ``stale`` and deliberately leaves the cursor
    alone — it has no new coverage to claim. So a merge that read the row
    *before* the latch went up still holds a matching cursor, and a
    cursor-only ``UPDATE`` would land, write ``stale = false`` from its
    own candidate, and erase the latch on its way past. The material the
    latch was raised about — a turn the player reversed — would be inside
    the summary, permanently, with nothing left to say so.

    Only SQL can settle this one: it is the ``WHERE`` clause that has to
    carry both columns.
    """
    await _seed_character(session_factory)
    repository = SADialogueCheckpointRepository(session_factory)
    start = _message("起點", 40)
    assert await repository.save(
        _checkpoint("原本的摘要", start), expected_message_key=None,
    )
    read = await repository.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    assert read.stale is False

    await repository.mark_stale(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID, now=NOW,
    )

    stale_unaware = _checkpoint("不該落地的摘要", _message("新邊界", 30))
    assert not await repository.save(
        stale_unaware,
        expected_message_key=read.covers_until_message_key,
        expected_stale=False,
    )

    stored = await repository.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    assert stored.stale is True
    assert stored.summary_text == "原本的摘要"


async def test_the_rebuild_that_read_the_latch_clears_it(
    session_factory,
) -> None:
    """The counterpart, and the reason the predicate is ``expected_stale``
    rather than a flat ``stale = false``: the run that answers the latch
    legitimately reads a stale row, and writing a fresh non-stale
    checkpoint is precisely what it is for. Forbid that and the pair is
    frozen in the stale state forever."""
    await _seed_character(session_factory)
    repository = SADialogueCheckpointRepository(session_factory)
    assert await repository.save(
        _checkpoint("原本的摘要", _message("起點", 40)),
        expected_message_key=None,
    )
    await repository.mark_stale(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID, now=NOW,
    )
    read = await repository.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )

    assert await repository.save(
        _checkpoint("重建的摘要", _message("新邊界", 30)),
        expected_message_key=read.covers_until_message_key,
        expected_stale=True,
    )

    stored = await repository.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    assert stored.stale is False
    assert stored.summary_text == "重建的摘要"
