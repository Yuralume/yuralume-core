"""Cloud→Core per-card exclusive-freeze semantics (D7 / EC10-A / EC10-B).

Keyed by ``origin_official_card_id`` across every tenant, writing the same
``frozen`` / ``frozen_reason`` chain as ``idle`` / ``manual`` — not the
orthogonal ``subscription_locked`` projection the tenant subscription-freeze
channel uses. Idempotent freeze/unfreeze and non-interference with other
freeze provenance are the load-bearing behaviours pinned here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.exclusive_card_freeze_service import (
    ExclusiveCardFreezeService,
)
from kokoro_link.domain.entities.character import (
    FREEZE_REASON_EXCLUSIVE_CONTRACT_END,
    FREEZE_REASON_MANUAL,
    FREEZE_REASON_SUBSCRIPTION_LAPSE,
    Character,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
_CARD = "hoshino-himari"


class _Clock:
    def now(self) -> datetime:
        return _NOW


def _character(name: str, *, card_id: str | None = _CARD) -> Character:
    return Character.create(
        name=name, summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], user_id=f"op-{name}",
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        origin_official_card_id=card_id,
    )


def _seed() -> tuple[InMemoryCharacterRepository, ExclusiveCardFreezeService]:
    characters = InMemoryCharacterRepository()
    service = ExclusiveCardFreezeService(
        character_repository=characters, clock=_Clock(),
    )
    return characters, service


@pytest.mark.asyncio
async def test_freeze_covers_every_tenant_installed_from_the_card() -> None:
    characters, service = _seed()
    a = _character("A")
    b = _character("B")
    other_card = _character("Other", card_id="another-card")
    self_hosted = _character("SelfHosted", card_id=None)
    for c in (a, b, other_card, self_hosted):
        await characters.save(c)

    result = await service.freeze_card(_CARD)

    assert result.characters == 2
    assert result.frozen == 2
    assert result.failures == 0
    for cid in (a.id, b.id):
        stored = await characters.get(cid)
        assert stored.frozen is True
        assert stored.frozen_reason == FREEZE_REASON_EXCLUSIVE_CONTRACT_END
        assert stored.frozen_at == _NOW
    # Different card / self-hosted rows are untouched.
    assert (await characters.get(other_card.id)).frozen is False
    assert (await characters.get(self_hosted.id)).frozen is False


@pytest.mark.asyncio
async def test_freeze_is_idempotent_on_retry() -> None:
    characters, service = _seed()
    a = _character("A")
    await characters.save(a)

    first = await service.freeze_card(_CARD)
    second = await service.freeze_card(_CARD)

    assert first.frozen == 1
    # Already frozen for this exact reason → skipped, not recounted.
    assert second.frozen == 0
    assert second.failures == 0
    assert (await characters.get(a.id)).frozen_reason == (
        FREEZE_REASON_EXCLUSIVE_CONTRACT_END
    )


@pytest.mark.asyncio
async def test_freeze_leaves_a_prior_unrelated_freeze_reason_alone() -> None:
    """A row already frozen for another reason keeps that provenance.

    Overwriting bought nothing — the character is silent either way — but
    it destroyed the record of *who* froze it, so this channel's own
    ``unfreeze_card`` would later lift an admin's ``manual`` sanction as a
    side effect of a contract being renewed. Freeze is a no-op on any
    already-frozen row."""
    characters, service = _seed()
    manual = _character("Manual")
    await characters.save(manual)
    await characters.set_frozen(
        manual.id, frozen=True, now=_NOW, reason=FREEZE_REASON_MANUAL,
    )

    result = await service.freeze_card(_CARD)

    assert result.characters == 1
    assert result.frozen == 0
    assert result.failures == 0
    stored = await characters.get(manual.id)
    assert stored.frozen is True
    assert stored.frozen_reason == FREEZE_REASON_MANUAL


@pytest.mark.asyncio
async def test_manual_freeze_survives_a_freeze_then_unfreeze_round_trip() -> None:
    """The end-to-end shape F13 is about: contract ends, then is renewed.

    The admin's manual sanction must still be in force afterwards —
    ``unfreeze_card`` only clears rows carrying its own reason, and freeze
    no longer relabels foreign rows into that set."""
    characters, service = _seed()
    manual = _character("Manual")
    await characters.save(manual)
    await characters.set_frozen(
        manual.id, frozen=True, now=_NOW, reason=FREEZE_REASON_MANUAL,
    )

    await service.freeze_card(_CARD)
    await service.unfreeze_card(_CARD)

    stored = await characters.get(manual.id)
    assert stored.frozen is True
    assert stored.frozen_reason == FREEZE_REASON_MANUAL


@pytest.mark.asyncio
async def test_unfreeze_only_clears_rows_it_froze() -> None:
    characters, service = _seed()
    ours = _character("Ours")
    manual = _character("Manual")
    legacy = _character("Legacy")
    for c in (ours, manual, legacy):
        await characters.save(c)
    await characters.set_frozen(
        ours.id, frozen=True, now=_NOW,
        reason=FREEZE_REASON_EXCLUSIVE_CONTRACT_END,
    )
    await characters.set_frozen(
        manual.id, frozen=True, now=_NOW, reason=FREEZE_REASON_MANUAL,
    )
    await characters.set_frozen(
        legacy.id, frozen=True, now=_NOW,
        reason=FREEZE_REASON_SUBSCRIPTION_LAPSE,
    )

    result = await service.unfreeze_card(_CARD)

    assert result.characters == 3
    assert result.unfrozen == 1
    assert result.failures == 0
    assert (await characters.get(ours.id)).frozen is False
    assert (await characters.get(ours.id)).frozen_reason is None
    # Manual / legacy subscription freezes are left exactly as they were —
    # this channel never undoes a freeze it did not set.
    manual_stored = await characters.get(manual.id)
    assert manual_stored.frozen is True
    assert manual_stored.frozen_reason == FREEZE_REASON_MANUAL
    legacy_stored = await characters.get(legacy.id)
    assert legacy_stored.frozen is True
    assert legacy_stored.frozen_reason == FREEZE_REASON_SUBSCRIPTION_LAPSE


@pytest.mark.asyncio
async def test_unfreeze_is_idempotent_on_retry() -> None:
    characters, service = _seed()
    a = _character("A")
    await characters.save(a)
    await characters.set_frozen(
        a.id, frozen=True, now=_NOW,
        reason=FREEZE_REASON_EXCLUSIVE_CONTRACT_END,
    )

    first = await service.unfreeze_card(_CARD)
    second = await service.unfreeze_card(_CARD)

    assert first.unfrozen == 1
    assert second.unfrozen == 0
    assert (await characters.get(a.id)).frozen is False


@pytest.mark.asyncio
async def test_unknown_card_is_a_no_op() -> None:
    characters, service = _seed()
    await characters.save(_character("A"))

    frozen = await service.freeze_card("no-such-card")
    unfrozen = await service.unfreeze_card("no-such-card")

    assert frozen.characters == 0
    assert frozen.frozen == 0
    assert unfrozen.characters == 0
    assert unfrozen.unfrozen == 0


@pytest.mark.asyncio
async def test_freeze_counts_per_character_failures() -> None:
    class _RaisingCharacters(InMemoryCharacterRepository):
        async def set_frozen(self, *args, **kwargs) -> bool:  # type: ignore[override]
            raise RuntimeError("db down")

    characters = _RaisingCharacters()
    service = ExclusiveCardFreezeService(
        character_repository=characters, clock=_Clock(),
    )
    await characters.save(_character("A"))

    result = await service.freeze_card(_CARD)

    assert result.frozen == 0
    assert result.failures == 1
