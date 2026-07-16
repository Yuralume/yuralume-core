"""ChatService routes post-turn-observed address changes through the
address-change governance with source=observed (S1 fix), instead of
letting them become a direction-flipped free-text memory."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.contracts.post_turn import AddressChangeSignal


class _FakeNamesService:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def update_names(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(**kwargs)


def _service(names) -> ChatService:
    # Test the routing method in isolation — it only reads
    # self._relationship_names_service.
    svc = object.__new__(ChatService)
    svc._relationship_names_service = names
    return svc


def _run(svc, *, changes, operator=SimpleNamespace(id="op1"),
         character=SimpleNamespace(id="c1", name="周景澄")):
    return asyncio.run(
        svc._apply_observed_address_changes(
            character=character,
            operator=operator,
            changes=changes,
        )
    )


def test_player_direction_maps_to_user_address_name() -> None:
    names = _FakeNamesService()
    _run(_service(names), changes=[
        AddressChangeSignal(direction="player", new_value="森森"),
    ])
    assert names.calls == [{
        "character_id": "c1",
        "operator_id": "op1",
        "source": "observed",
        "user_address_name": "森森",
    }]


def test_character_direction_maps_to_character_address_name() -> None:
    names = _FakeNamesService()
    _run(_service(names), changes=[
        AddressChangeSignal(direction="character", new_value="小美"),
    ])
    assert names.calls == [{
        "character_id": "c1",
        "operator_id": "op1",
        "source": "observed",
        "character_address_name": "小美",
    }]


def test_unknown_direction_and_empty_value_skipped() -> None:
    names = _FakeNamesService()
    _run(_service(names), changes=[
        AddressChangeSignal(direction="both", new_value="x"),
        AddressChangeSignal(direction="player", new_value=""),
    ])
    assert names.calls == []


def test_noop_when_service_or_operator_missing() -> None:
    # No service wired.
    _run(_service(None), changes=[
        AddressChangeSignal(direction="player", new_value="森森"),
    ])
    # No operator resolved.
    names = _FakeNamesService()
    _run(_service(names), changes=[
        AddressChangeSignal(direction="player", new_value="森森"),
    ], operator=None)
    assert names.calls == []


def test_player_direction_naming_character_itself_is_skipped() -> None:
    # The player calling the character by the character's own name (「周景澄」)
    # is a mis-read: the model proposed using the character's name as how the
    # character should address the *player*. Must not reach update_names.
    names = _FakeNamesService()
    _run(_service(names), changes=[
        AddressChangeSignal(direction="player", new_value="周景澄"),
    ])
    assert names.calls == []


def test_player_direction_character_name_match_is_case_and_space_insensitive() -> None:
    # Normalise whitespace/case before equality (English names): a
    # player-direction value equal to the character name after trimming and
    # case-folding is dropped.
    names = _FakeNamesService()
    _run(
        _service(names),
        changes=[AddressChangeSignal(direction="player", new_value=" Alex ")],
        character=SimpleNamespace(id="c1", name="alex"),
    )
    assert names.calls == []


def test_player_direction_non_matching_name_still_applies() -> None:
    # A genuine player-direction rename that is NOT the character's name must
    # still route to update_names — the guard must not over-block.
    names = _FakeNamesService()
    _run(_service(names), changes=[
        AddressChangeSignal(direction="player", new_value="森森"),
    ])
    assert names.calls == [{
        "character_id": "c1",
        "operator_id": "op1",
        "source": "observed",
        "user_address_name": "森森",
    }]


def test_character_direction_equal_to_character_name_still_applies() -> None:
    # The character-direction check is only for the player direction. A
    # character-direction value equal to the character's name is legitimate
    # (the player addressing the character by name) and must pass through.
    names = _FakeNamesService()
    _run(_service(names), changes=[
        AddressChangeSignal(direction="character", new_value="周景澄"),
    ])
    assert names.calls == [{
        "character_id": "c1",
        "operator_id": "op1",
        "source": "observed",
        "character_address_name": "周景澄",
    }]
