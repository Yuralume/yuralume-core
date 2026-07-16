"""Step 8 — player edit of the per-pair relationship address names.

Each changed direction must: persist the seed, write exactly one
rename-log event (carrying ``character_id``), and — for the player
direction only — reconcile the learned persona ``name``. Clearing or a
no-op change must not write a rename-log event (its ``new_value`` would
be empty / unchanged).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from kokoro_link.application.services.relationship_names_service import (
    RelationshipNamesService,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.value_objects.address_change_event import (
    DIRECTION_CHARACTER,
    DIRECTION_PLAYER,
)


class _FakeSeedRepo:
    def __init__(self, seed: CharacterOperatorRelationshipSeed | None) -> None:
        self._seed = seed
        self.saved: list[CharacterOperatorRelationshipSeed] = []

    async def get(self, character_id, operator_id):
        return self._seed

    async def save(self, seed):
        self._seed = seed
        self.saved.append(seed)

    async def delete_for_character(self, character_id):
        return 0


class _FakeChangeLog:
    def __init__(self) -> None:
        self.events: list = []

    async def record(self, event):
        self.events.append(event)
        return event

    async def latest(self, **kwargs):
        return None

    async def list_for_pair(self, **kwargs):
        return []


class _FakePersona:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self._fail = fail

    async def set_explicit_field_for_operator(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise RuntimeError("boom")
        return None


def _svc(seed, *, persona=None, change_log=None):
    return RelationshipNamesService(
        seed_repository=_FakeSeedRepo(seed),
        change_log_repository=change_log or _FakeChangeLog(),
        persona_service=persona,
    )


def _seed(**kw) -> CharacterOperatorRelationshipSeed:
    return CharacterOperatorRelationshipSeed(
        character_id="c1", operator_id="op1", **kw,
    )


def test_change_user_address_logs_player_and_reconciles_persona() -> None:
    change_log = _FakeChangeLog()
    persona = _FakePersona()
    repo = _FakeSeedRepo(_seed(user_address_name="丹尼"))
    svc = RelationshipNamesService(
        seed_repository=repo,
        change_log_repository=change_log,
        persona_service=persona,
    )

    result = asyncio.run(
        svc.update_names(
            character_id="c1",
            operator_id="op1",
            user_address_name="阿丹",
        )
    )

    assert result.user_address_name == "阿丹"
    assert repo.saved  # seed persisted
    assert len(change_log.events) == 1
    ev = change_log.events[0]
    assert ev.direction == DIRECTION_PLAYER
    assert ev.old_value == "丹尼"
    assert ev.new_value == "阿丹"
    assert ev.character_id == "c1"
    # persona name reconciled to the new address name; a settings edit
    # (default source) reconciles as a deliberate (non-observed) write.
    assert persona.calls == [
        {
            "character_id": "c1",
            "operator_id": "op1",
            "field_key": "name",
            "value": "阿丹",
            "observed": False,
            "now": persona.calls[0]["now"],
        }
    ]


def test_change_character_address_logs_character_no_persona() -> None:
    change_log = _FakeChangeLog()
    persona = _FakePersona()
    svc = RelationshipNamesService(
        seed_repository=_FakeSeedRepo(_seed(character_address_name="美緒")),
        change_log_repository=change_log,
        persona_service=persona,
    )

    asyncio.run(
        svc.update_names(
            character_id="c1",
            operator_id="op1",
            character_address_name="美緒姐",
        )
    )

    assert len(change_log.events) == 1
    assert change_log.events[0].direction == DIRECTION_CHARACTER
    assert change_log.events[0].new_value == "美緒姐"
    # character-direction change is about the character's name, not the
    # operator persona — no reconcile.
    assert persona.calls == []


def test_clearing_name_writes_no_rename_log() -> None:
    change_log = _FakeChangeLog()
    repo = _FakeSeedRepo(_seed(user_address_name="丹尼"))
    svc = RelationshipNamesService(
        seed_repository=repo,
        change_log_repository=change_log,
        persona_service=_FakePersona(),
    )

    result = asyncio.run(
        svc.update_names(
            character_id="c1", operator_id="op1", user_address_name="",
        )
    )

    assert result.user_address_name == ""  # cleared
    assert repo.saved  # still persisted
    assert change_log.events == []  # no event for a clear


def test_unchanged_value_writes_no_rename_log() -> None:
    change_log = _FakeChangeLog()
    svc = RelationshipNamesService(
        seed_repository=_FakeSeedRepo(_seed(user_address_name="丹尼")),
        change_log_repository=change_log,
        persona_service=_FakePersona(),
    )
    asyncio.run(
        svc.update_names(
            character_id="c1", operator_id="op1", user_address_name="丹尼",
        )
    )
    assert change_log.events == []


def test_creates_seed_when_absent() -> None:
    change_log = _FakeChangeLog()
    repo = _FakeSeedRepo(None)
    svc = RelationshipNamesService(
        seed_repository=repo,
        change_log_repository=change_log,
        persona_service=_FakePersona(),
    )
    result = asyncio.run(
        svc.update_names(
            character_id="c1", operator_id="op1", user_address_name="阿丹",
        )
    )
    assert result.character_id == "c1"
    assert result.user_address_name == "阿丹"
    assert len(change_log.events) == 1


def test_observed_source_stamps_rename_log() -> None:
    # A chat-observed change (post-turn extractor) is distinguishable
    # from a settings-UI edit via the rename-log source, and reconciles
    # the persona name as an observed (not deliberate) write.
    change_log = _FakeChangeLog()
    persona = _FakePersona()
    svc = RelationshipNamesService(
        seed_repository=_FakeSeedRepo(_seed(user_address_name="丹尼")),
        change_log_repository=change_log,
        persona_service=persona,
    )
    asyncio.run(
        svc.update_names(
            character_id="c1", operator_id="op1",
            user_address_name="森森", source="observed",
        )
    )
    assert len(change_log.events) == 1
    assert change_log.events[0].source == "observed"
    assert change_log.events[0].new_value == "森森"
    assert persona.calls[0]["observed"] is True


def test_invalid_source_falls_back_to_player_edit() -> None:
    change_log = _FakeChangeLog()
    svc = RelationshipNamesService(
        seed_repository=_FakeSeedRepo(_seed(user_address_name="丹尼")),
        change_log_repository=change_log,
        persona_service=_FakePersona(),
    )
    asyncio.run(
        svc.update_names(
            character_id="c1", operator_id="op1",
            user_address_name="森森", source="garbage",
        )
    )
    assert change_log.events[0].source == "player_edit"


def test_persona_reconcile_failure_is_soft() -> None:
    change_log = _FakeChangeLog()
    repo = _FakeSeedRepo(_seed(user_address_name="丹尼"))
    svc = RelationshipNamesService(
        seed_repository=repo,
        change_log_repository=change_log,
        persona_service=_FakePersona(fail=True),
    )
    # A persona reconcile crash must not bubble up or undo the edit.
    result = asyncio.run(
        svc.update_names(
            character_id="c1", operator_id="op1", user_address_name="阿丹",
        )
    )
    assert result.user_address_name == "阿丹"
    assert repo.saved
    assert len(change_log.events) == 1  # rename log still written


# ---------------------------------------------------------------------------
# Direction-inversion guard (structural cross-check).
#
# The post-turn mini model sometimes mis-reads the player addressing the
# *character* (兄妹設定喊「哥哥」、情侶喊「老公」) as an observed
# ``player``-direction change, which would overwrite how the character
# addresses the *player* with the term the player uses for the character —
# flipping the two directions. Guard: an ``observed`` write whose new value
# collides with the *opposite* direction's current seed value is dropped
# field-by-field. A ``player_edit`` (the player's own settings action) is
# never blocked — the player may deliberately set any value.
# ---------------------------------------------------------------------------


def test_observed_player_collides_with_character_address_is_blocked() -> None:
    # seed: char calls player 小菊 (user_address_name);
    #       player calls char 哥哥 (character_address_name).
    # An observed player-direction change proposing 哥哥 (== how the player
    # addresses the character) is a direction inversion — it must be dropped.
    change_log = _FakeChangeLog()
    persona = _FakePersona()
    repo = _FakeSeedRepo(
        _seed(user_address_name="小菊", character_address_name="哥哥"),
    )
    svc = RelationshipNamesService(
        seed_repository=repo,
        change_log_repository=change_log,
        persona_service=persona,
    )
    result = asyncio.run(
        svc.update_names(
            character_id="c1", operator_id="op1",
            user_address_name="哥哥", source="observed",
        )
    )
    # seed unchanged, no rename log, no persona reconcile.
    assert result.user_address_name == "小菊"
    assert change_log.events == []
    assert persona.calls == []


def test_observed_character_collides_with_user_address_is_blocked() -> None:
    # Symmetric: an observed character-direction change proposing the value
    # currently used as user_address_name (how char calls player) is dropped.
    change_log = _FakeChangeLog()
    persona = _FakePersona()
    repo = _FakeSeedRepo(
        _seed(user_address_name="小菊", character_address_name="哥哥"),
    )
    svc = RelationshipNamesService(
        seed_repository=repo,
        change_log_repository=change_log,
        persona_service=persona,
    )
    result = asyncio.run(
        svc.update_names(
            character_id="c1", operator_id="op1",
            character_address_name="小菊", source="observed",
        )
    )
    assert result.character_address_name == "哥哥"
    assert change_log.events == []
    assert persona.calls == []


def test_player_edit_collision_is_not_blocked() -> None:
    # The player's own settings edit may deliberately set user_address_name
    # to the same string used the other direction — never blocked.
    change_log = _FakeChangeLog()
    persona = _FakePersona()
    repo = _FakeSeedRepo(
        _seed(user_address_name="小菊", character_address_name="哥哥"),
    )
    svc = RelationshipNamesService(
        seed_repository=repo,
        change_log_repository=change_log,
        persona_service=persona,
    )
    result = asyncio.run(
        svc.update_names(
            character_id="c1", operator_id="op1",
            user_address_name="哥哥",  # source defaults to player_edit
        )
    )
    assert result.user_address_name == "哥哥"
    assert len(change_log.events) == 1
    assert change_log.events[0].direction == DIRECTION_PLAYER
    assert persona.calls  # persona reconciled as a deliberate edit


def test_observed_non_colliding_value_still_writes() -> None:
    # A legitimate observed rename (new value != the opposite direction's
    # value) must still land — the guard must not over-block.
    change_log = _FakeChangeLog()
    persona = _FakePersona()
    repo = _FakeSeedRepo(
        _seed(user_address_name="小菊", character_address_name="哥哥"),
    )
    svc = RelationshipNamesService(
        seed_repository=repo,
        change_log_repository=change_log,
        persona_service=persona,
    )
    result = asyncio.run(
        svc.update_names(
            character_id="c1", operator_id="op1",
            user_address_name="森森", source="observed",
        )
    )
    assert result.user_address_name == "森森"
    assert len(change_log.events) == 1
    assert change_log.events[0].new_value == "森森"
    assert persona.calls[0]["observed"] is True


def test_observed_one_field_blocked_other_field_still_updates() -> None:
    # When an observed update carries both directions and only one collides,
    # the colliding field is dropped while the legitimate field still lands.
    change_log = _FakeChangeLog()
    persona = _FakePersona()
    repo = _FakeSeedRepo(
        _seed(user_address_name="小菊", character_address_name="哥哥"),
    )
    svc = RelationshipNamesService(
        seed_repository=repo,
        change_log_repository=change_log,
        persona_service=persona,
    )
    result = asyncio.run(
        svc.update_names(
            character_id="c1", operator_id="op1",
            user_address_name="哥哥",   # collides with character_address_name → blocked
            character_address_name="哥",  # legitimate → still updates
            source="observed",
        )
    )
    assert result.user_address_name == "小菊"  # blocked field unchanged
    assert result.character_address_name == "哥"  # legitimate field updated
    assert len(change_log.events) == 1
    assert change_log.events[0].direction == DIRECTION_CHARACTER
    assert change_log.events[0].new_value == "哥"
