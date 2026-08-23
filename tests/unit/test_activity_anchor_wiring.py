"""Self-host neutrality of the NF4 foreground-interaction anchor.

``NEW_PLAYER_FUNNEL_FREE_TIER_PLAN.md`` §5 red line: "self-host 行為零變化".

The anchor advances ``CharacterState.last_active_at``, and that column is NOT a
dormancy-only field — the dormancy knob is merely the newest of its readers. On
self-host it is also:

* ``proactive_dispatcher._has_user_started_interaction`` — NULL means "the
  player has never opened their mouth", which is what keeps a freshly created
  character from texting first. A non-NULL value *unlocks* proactive messaging;
* ``proactive_dispatcher._compute_idle_minutes``;
* the feed's silence anchor (``feed_candidates``);
* ``runtime_activity_gate``.

So a self-host player who only ever plays 分歧劇場 / 起幕 / 融合故事 would start
receiving proactive messages they never used to get — with the dormancy knob
still NULL. The gate is therefore the *wiring*, not the knob: the container
builds the anchor in cloud mode only, and self-host injects ``None``.

These tests pin both halves: the container branch, and the behaviour that
branch buys (an anchor-less drama service never writes the column).
"""

from __future__ import annotations

import asyncio

import pytest

from kokoro_link.application.services.branching_drama_service import (
    BranchingDramaService,
)
from kokoro_link.application.services.character_activity_anchor import (
    CharacterActivityAnchor,
)
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings, CloudSettings
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)

from tests.unit.test_branching_drama_service import (  # noqa: F401 — fixtures
    _CharServiceStub,
    _NullBriefBuilder,
    char_a,
    char_b,
    director,
    planner,
    repo,
)


def _self_host_settings() -> AppSettings:
    return AppSettings()


def _cloud_settings() -> AppSettings:
    return AppSettings(
        cloud=CloudSettings(
            enabled=True,
            user_service_url="https://users.example",
            gateway_url="https://gateway.example",
            deployment_token="ykl_deploy",
            llm_model_presets={"chat": "preset-chat"},
        ),
    )


# --------------------------------------------------------------------------- #
# container wiring branch
# --------------------------------------------------------------------------- #


def test_self_host_wires_no_activity_anchor() -> None:
    container = build_container(_self_host_settings())

    # All three paid foreground surfaces exist as before — they are simply not
    # tracking the anchor, which is the pre-NF4 path byte for byte.
    assert container.story_scene_service is not None
    assert container.fusion_story_service is not None
    assert container.branching_drama_service is not None
    assert container.story_scene_service._activity_anchor is None  # noqa: SLF001
    assert container.fusion_story_service._activity_anchor is None  # noqa: SLF001
    assert container.branching_drama_service._activity_anchor is None  # noqa: SLF001


def test_cloud_mode_wires_one_shared_activity_anchor() -> None:
    container = build_container(_cloud_settings())

    anchor = container.story_scene_service._activity_anchor  # noqa: SLF001
    assert isinstance(anchor, CharacterActivityAnchor)
    # One instance, shared — it holds no state, and two of them would be two
    # clocks.
    assert container.fusion_story_service._activity_anchor is anchor  # noqa: SLF001
    assert container.branching_drama_service._activity_anchor is anchor  # noqa: SLF001


# --------------------------------------------------------------------------- #
# what the branch buys: an anchor-less service never moves the column
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_self_host_drama_play_never_advances_last_active_at(
    repo, char_a, char_b, planner, director,  # noqa: F811
) -> None:
    """A whole self-host playthrough leaves the proactive gate closed.

    ``last_active_at`` stays NULL through create → start → interact → advance,
    so ``_has_user_started_interaction`` keeps answering "no" exactly as it did
    before NF4.
    """
    characters = InMemoryCharacterRepository()
    await characters.save(char_a)
    await characters.save(char_b)
    # Pre-condition: the fixtures really do start with an empty anchor, so a
    # green run cannot be a green-because-nothing-was-set accident.
    for character in await characters.list():
        assert character.state.last_active_at is None

    service = BranchingDramaService(
        repository=repo,
        character_service=_CharServiceStub(
            by_id={char_a.id: char_a, char_b.id: char_b},
        ),
        brief_builder=_NullBriefBuilder(),
        planner=planner,
        director=director,
        # Exactly what the container passes on self-host.
        activity_anchor=None,
    )
    try:
        drama = await service.create(
            character_ids=["c-a", "c-b"], prompt="測試", total_segments=2,
        )
        await asyncio.sleep(0.5)
        session, _, _ = await service.start_session(drama.id)
        await service.interact_session(session.id, player_input="你還好嗎？")
        await service.advance_session(session.id)
    finally:
        tasks = list(service._tasks.values())  # noqa: SLF001
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    for character in await characters.list():
        assert character.state.last_active_at is None


@pytest.mark.asyncio
async def test_the_pin_above_would_fail_if_the_anchor_were_wired(
    repo, char_a, char_b, planner, director,  # noqa: F811
) -> None:
    """Counter-test: the same playthrough WITH an anchor does write it.

    Without this, the pin above would still pass if 分歧劇場 quietly stopped
    touching the anchor altogether — it would be pinning nothing.
    """
    characters = InMemoryCharacterRepository()
    await characters.save(char_a)
    await characters.save(char_b)

    service = BranchingDramaService(
        repository=repo,
        character_service=_CharServiceStub(
            by_id={char_a.id: char_a, char_b.id: char_b},
        ),
        brief_builder=_NullBriefBuilder(),
        planner=planner,
        director=director,
        activity_anchor=CharacterActivityAnchor(characters),
    )
    try:
        drama = await service.create(
            character_ids=["c-a", "c-b"], prompt="測試", total_segments=2,
        )
        await asyncio.sleep(0.5)
        session, _, _ = await service.start_session(drama.id)
        await service.interact_session(session.id, player_input="你還好嗎？")
    finally:
        tasks = list(service._tasks.values())  # noqa: SLF001
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    assert all(
        character.state.last_active_at is not None
        for character in await characters.list()
    )
