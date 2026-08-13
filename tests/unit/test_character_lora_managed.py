"""託管角色（IP 合作卡）的 LoRA 寫入保護（EC 系列 F14）。

LoRA 是 `Character` 上受託管保護的欄位之一：`CharacterService.
update_character` 早就拒絕對託管角色寫 `loras`，而遮蔽投影讓玩家根本
看不到這欄。但 `POST/PATCH/DELETE /characters/{id}/loras` 這四條端點
直接改角色列，完全繞過那條 allowlist——結果是玩家可以往一個授權方擁有
的角色塞權重、改出授權方沒授權的畫風，而且因為欄位被遮蔽，連自己寫了
什麼都看不到。

拒絕形狀比照 `AlbumManagedError`：403 而非 404——角色存在、也確實在呼
叫者自己的名單裡，回 404 是說謊。一般角色（origin 為 NULL，self-host
的唯一形態）四條路徑逐位元零變化。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.services.character_lora_service import (
    CharacterLoraService,
    LoraManagedError,
    LoraNotFoundError,
)
from kokoro_link.domain.entities.character import Character, CharacterLora
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)

_CARD_ID = "cloud-card-himari"
_LORA = "himari-style.safetensors"


def _character(*, origin: str | None) -> Character:
    return Character.create(
        name="Himari",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        user_id="op-1",
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        origin_official_card_id=origin,
    )


async def _seed(
    tmp_path: Path, *, origin: str | None,
) -> tuple[CharacterLoraService, InMemoryCharacterRepository, Character]:
    characters = InMemoryCharacterRepository()
    character = _character(origin=origin)
    # Pre-attach one LoRA so ``set_strength`` / ``remove`` reach the managed
    # check instead of short-circuiting on "no such LoRA".
    character = character.with_loras([CharacterLora(name=_LORA, strength=1.0)])
    await characters.save(character)
    lora_dir = tmp_path / "loras"
    service = CharacterLoraService(
        character_repository=characters, lora_dir=lora_dir,
    )
    return service, characters, character


@pytest.mark.asyncio
async def test_upload_refuses_managed_character(tmp_path: Path) -> None:
    service, characters, character = await _seed(tmp_path, origin=_CARD_ID)

    with pytest.raises(LoraManagedError):
        await service.upload(
            character.id,
            data=b"weights",
            original_filename="player-made.safetensors",
            strength=0.8,
        )

    stored = await characters.get(character.id)
    assert [lora.name for lora in stored.loras] == [_LORA]
    # Refused *before* the bytes land: a rejected write leaves no file
    # behind in ComfyUI's shared models directory.
    assert not (tmp_path / "loras" / "player-made.safetensors").exists()


@pytest.mark.asyncio
async def test_attach_existing_refuses_managed_character(tmp_path: Path) -> None:
    service, characters, character = await _seed(tmp_path, origin=_CARD_ID)

    with pytest.raises(LoraManagedError):
        await service.attach_existing(
            character.id, name="other.safetensors", strength=1.0,
        )

    stored = await characters.get(character.id)
    assert [lora.name for lora in stored.loras] == [_LORA]


@pytest.mark.asyncio
async def test_set_strength_refuses_managed_character(tmp_path: Path) -> None:
    service, characters, character = await _seed(tmp_path, origin=_CARD_ID)

    with pytest.raises(LoraManagedError):
        await service.set_strength(character.id, name=_LORA, strength=0.1)

    stored = await characters.get(character.id)
    assert stored.loras[0].strength == 1.0


@pytest.mark.asyncio
async def test_remove_refuses_managed_character(tmp_path: Path) -> None:
    service, characters, character = await _seed(tmp_path, origin=_CARD_ID)

    with pytest.raises(LoraManagedError):
        await service.remove(character.id, name=_LORA)

    stored = await characters.get(character.id)
    assert [lora.name for lora in stored.loras] == [_LORA]


@pytest.mark.asyncio
async def test_ordinary_character_keeps_every_lora_mutation(
    tmp_path: Path,
) -> None:
    """self-host 的唯一形態（origin NULL）零回歸：四條路徑照舊。"""
    service, characters, character = await _seed(tmp_path, origin=None)

    await service.upload(
        character.id,
        data=b"weights",
        original_filename="player-made.safetensors",
        strength=0.8,
    )
    assert (tmp_path / "loras" / "player-made.safetensors").exists()

    await service.attach_existing(
        character.id, name="other.safetensors", strength=0.5,
    )
    await service.set_strength(character.id, name=_LORA, strength=0.25)
    updated = await service.remove(character.id, name="other.safetensors")

    names = {lora.name: lora.strength for lora in updated.loras}
    assert names == {_LORA: 0.25, "player-made.safetensors": 0.8}


@pytest.mark.asyncio
async def test_missing_lora_on_ordinary_character_still_404s(
    tmp_path: Path,
) -> None:
    """The managed gate does not swallow the pre-existing not-found path."""
    service, _, character = await _seed(tmp_path, origin=None)

    with pytest.raises(LoraNotFoundError):
        await service.set_strength(
            character.id, name="never-attached.safetensors", strength=1.0,
        )
