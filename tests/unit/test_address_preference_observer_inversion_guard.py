"""#3 — direction-inversion guard on the observed salutation write path.

The address-preference observer writes ``salutation`` (direction B: how
the player addresses the character). A recurring contamination is the
observer mis-reading a direction-A term (how the character addresses the
*player* — the seed ``user_address_name`` or the operator's own
name/display_name) as the salutation, flipping the two directions.

This mirrors the ``update_names`` observed-inversion guard added in the
previous task: it drops only the *observed* value when it structurally
collides with the opposite direction; a player's explicit setting is
never blocked. Normalisation is strip + casefold exact — no fuzzy match.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.address_preference_observer_service import (
    AddressPreferenceObserverService,
)
from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.contracts.operator_address_preference import (
    AddressObservationCandidate,
    OperatorAddressObserverPort,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.repositories.in_memory_address_preferences import (
    InMemoryOperatorAddressPreferenceRepository,
)


_CHAR_ID = "c"
_OP_ID = "op"


class _StubObserver(OperatorAddressObserverPort):
    def __init__(self, candidate: AddressObservationCandidate | None) -> None:
        self.candidate = candidate
        self.calls = 0

    async def observe(self, *, character_id, operator_id, recent_user_messages):
        self.calls += 1
        return self.candidate


class _FakeSeedRepo:
    def __init__(self, seed: CharacterOperatorRelationshipSeed | None) -> None:
        self._seed = seed

    async def get(self, character_id, operator_id):
        return self._seed


class _FakeProfileService:
    def __init__(self, profile: OperatorProfile) -> None:
        self._profile = profile

    async def get_for_user(self, user_id):
        return self._profile


def _settings() -> HumanizationSettings:
    return HumanizationSettings(address_preference_enabled=True)


def _seed(user_address_name: str) -> CharacterOperatorRelationshipSeed:
    return CharacterOperatorRelationshipSeed(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        user_address_name=user_address_name,
        character_address_name="哥哥",
    )


def _build(
    *, candidate, seed=None, profile=None,
) -> tuple[AddressPreferenceObserverService, InMemoryOperatorAddressPreferenceRepository]:
    repo = InMemoryOperatorAddressPreferenceRepository()
    svc = AddressPreferenceObserverService(
        repository=repo,
        observer=_StubObserver(candidate),
        settings=_settings(),
        seed_repository=_FakeSeedRepo(seed) if seed is not None else None,
        operator_profile_service=(
            _FakeProfileService(profile) if profile is not None else None
        ),
    )
    return svc, repo


@pytest.mark.asyncio
async def test_salutation_matching_user_address_name_is_dropped():
    """Observed salutation == seed.user_address_name (direction A) is a
    direction inversion → drop the observation, keep prior/empty."""
    candidate = AddressObservationCandidate(
        salutation="小明",  # == seed.user_address_name below
        formality_level="low",
    )
    svc, repo = _build(
        candidate=candidate,
        seed=_seed(user_address_name="小明"),
    )

    await svc.observe_pair(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        recent_user_messages=["a", "b", "c", "d"],
    )

    stored = await repo.get(character_id=_CHAR_ID, operator_id=_OP_ID)
    # The salutation was dropped; a non-colliding band may still persist,
    # but the contaminated salutation must never be written.
    if stored is not None:
        assert stored.salutation != "小明"


@pytest.mark.asyncio
async def test_salutation_matching_operator_display_name_is_dropped():
    """Observed salutation == operator display_name (the player's own
    name) is a direction inversion → drop it."""
    candidate = AddressObservationCandidate(salutation="艾力")
    svc, repo = _build(
        candidate=candidate,
        seed=_seed(user_address_name="小明"),
        profile=OperatorProfile(id=_OP_ID, display_name="艾力"),
    )

    await svc.observe_pair(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        recent_user_messages=["a", "b", "c", "d"],
    )

    stored = await repo.get(character_id=_CHAR_ID, operator_id=_OP_ID)
    if stored is not None:
        assert stored.salutation != "艾力"


@pytest.mark.asyncio
async def test_case_and_whitespace_insensitive_collision():
    """Normalisation is strip + casefold exact."""
    candidate = AddressObservationCandidate(salutation="  Alex ")
    svc, repo = _build(
        candidate=candidate,
        seed=_seed(user_address_name="alex"),
    )

    await svc.observe_pair(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        recent_user_messages=["a", "b", "c", "d"],
    )

    stored = await repo.get(character_id=_CHAR_ID, operator_id=_OP_ID)
    if stored is not None:
        assert (stored.salutation or "").strip().casefold() != "alex"


@pytest.mark.asyncio
async def test_normal_salutation_written_as_usual():
    """A salutation that does NOT collide with the opposite direction is
    written normally — the guard only drops the inversion case."""
    candidate = AddressObservationCandidate(
        salutation="哥哥", formality_level="low",
    )
    svc, repo = _build(
        candidate=candidate,
        seed=_seed(user_address_name="小明"),
        profile=OperatorProfile(id=_OP_ID, display_name="艾力"),
    )

    await svc.observe_pair(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        recent_user_messages=["a", "b", "c", "d"],
    )

    stored = await repo.get(character_id=_CHAR_ID, operator_id=_OP_ID)
    assert stored is not None
    assert stored.salutation == "哥哥"


@pytest.mark.asyncio
async def test_guard_is_noop_without_seed_or_profile_deps():
    """Backwards compatible: when neither seed repo nor profile service is
    wired, the observer behaves exactly as before (no guard)."""
    candidate = AddressObservationCandidate(salutation="小明")
    repo = InMemoryOperatorAddressPreferenceRepository()
    svc = AddressPreferenceObserverService(
        repository=repo,
        observer=_StubObserver(candidate),
        settings=_settings(),
    )

    await svc.observe_pair(
        character_id=_CHAR_ID, operator_id=_OP_ID,
        recent_user_messages=["a", "b", "c", "d"],
    )

    stored = await repo.get(character_id=_CHAR_ID, operator_id=_OP_ID)
    assert stored is not None
    assert stored.salutation == "小明"
