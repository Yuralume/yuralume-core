"""Unit tests for :class:`AddressPreferenceObserverService` (§4.2)."""

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
from kokoro_link.domain.entities.operator_address_preference import (
    OperatorAddressPreference,
)
from kokoro_link.infrastructure.repositories.in_memory_address_preferences import (
    InMemoryOperatorAddressPreferenceRepository,
)


class _StubObserver(OperatorAddressObserverPort):
    def __init__(self, candidate: AddressObservationCandidate | None) -> None:
        self.candidate = candidate
        self.calls = 0

    async def observe(
        self,
        *,
        character_id: str,
        operator_id: str,
        recent_user_messages: list[str],
    ):
        self.calls += 1
        return self.candidate


def _settings(*, enabled: bool = True) -> HumanizationSettings:
    return HumanizationSettings(address_preference_enabled=enabled)


@pytest.mark.asyncio
async def test_observation_writes_when_flag_on_and_candidate_present() -> None:
    repo = InMemoryOperatorAddressPreferenceRepository()
    candidate = AddressObservationCandidate(
        salutation="蓁蓁",
        formality_level="low",
        response_length_pref="short",
        evidence_quote="蓁蓁，今天好累喔",
    )
    svc = AddressPreferenceObserverService(
        repository=repo,
        observer=_StubObserver(candidate),
        settings=_settings(),
    )
    result = await svc.observe_pair(
        character_id="c", operator_id="op",
        recent_user_messages=["a", "b", "c"],
    )
    assert result is not None
    stored = await repo.get(character_id="c", operator_id="op")
    assert stored is not None
    assert stored.salutation == "蓁蓁"
    assert stored.formality_level == "low"


@pytest.mark.asyncio
async def test_disabled_flag_blocks_observation() -> None:
    repo = InMemoryOperatorAddressPreferenceRepository()
    observer = _StubObserver(
        AddressObservationCandidate(salutation="alpha"),
    )
    svc = AddressPreferenceObserverService(
        repository=repo,
        observer=observer,
        settings=_settings(enabled=False),
    )
    result = await svc.observe_pair(
        character_id="c", operator_id="op",
        recent_user_messages=["a"],
    )
    assert result is None
    assert observer.calls == 0


@pytest.mark.asyncio
async def test_empty_candidate_preserves_prior() -> None:
    repo = InMemoryOperatorAddressPreferenceRepository()
    await repo.upsert(
        OperatorAddressPreference(
            character_id="c",
            operator_id="op",
            salutation="老闆",
            formality_level="high",
        ),
    )
    svc = AddressPreferenceObserverService(
        repository=repo,
        observer=_StubObserver(None),  # observer says "no signal"
        settings=_settings(),
    )
    result = await svc.observe_pair(
        character_id="c", operator_id="op",
        recent_user_messages=["a"],
    )
    assert result is None
    stored = await repo.get(character_id="c", operator_id="op")
    assert stored is not None
    assert stored.salutation == "老闆"
    assert stored.formality_level == "high"


@pytest.mark.asyncio
async def test_partial_candidate_merges_only_nonempty_fields() -> None:
    repo = InMemoryOperatorAddressPreferenceRepository()
    await repo.upsert(
        OperatorAddressPreference(
            character_id="c",
            operator_id="op",
            salutation="老闆",
            formality_level="high",
            response_length_pref="long",
        ),
    )
    candidate = AddressObservationCandidate(
        # Only salutation changes; bands should stay as-is.
        salutation="阿蓁",
        formality_level="",
        response_length_pref="",
    )
    svc = AddressPreferenceObserverService(
        repository=repo,
        observer=_StubObserver(candidate),
        settings=_settings(),
    )
    await svc.observe_pair(
        character_id="c", operator_id="op",
        recent_user_messages=["a", "b", "c"],
    )
    stored = await repo.get(character_id="c", operator_id="op")
    assert stored is not None
    assert stored.salutation == "阿蓁"
    assert stored.formality_level == "high"
    assert stored.response_length_pref == "long"


@pytest.mark.asyncio
async def test_empty_message_window_short_circuits() -> None:
    observer = _StubObserver(
        AddressObservationCandidate(salutation="alpha"),
    )
    svc = AddressPreferenceObserverService(
        repository=InMemoryOperatorAddressPreferenceRepository(),
        observer=observer,
        settings=_settings(),
    )
    result = await svc.observe_pair(
        character_id="c", operator_id="op", recent_user_messages=[],
    )
    assert result is None
    assert observer.calls == 0
