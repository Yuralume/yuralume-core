"""Character LoRA upload / mutation service.

Mirrors ``CharacterImageService`` in shape but targets a different
filesystem tree: uploaded ``.safetensors`` files land in ComfyUI's
``models/loras/`` directory so ComfyUI can discover them by filename
(the workflow references them by name, not path).

DB-wise the character just stores ``CharacterLora(name, strength)``
pairs. Disk and DB are *decoupled*: multiple characters can reference
the same physical file by name; deleting a LoRA from one character
doesn't touch the file. Operator cleanup of orphaned files is a
separate housekeeping concern — doing it automatically would risk
breaking other characters that rely on the same weights.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character, CharacterLora

_LOGGER = logging.getLogger(__name__)

MAX_LORA_BYTES = 512 * 1024 * 1024  # 512 MB — SDXL LoRAs are typically < 300 MB
_ALLOWED_EXTENSIONS = {".safetensors", ".ckpt", ".pt"}


class CharacterLoraError(Exception):
    """Base class — route layer turns these into specific HTTP codes."""


class LoraTooLargeError(CharacterLoraError):
    pass


class UnsupportedLoraTypeError(CharacterLoraError):
    pass


class LoraUploadDisabledError(CharacterLoraError):
    """Raised when the deployment hasn't configured ``lora_dir``."""


class LoraNotFoundError(CharacterLoraError):
    pass


class CharacterLoraService:
    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        lora_dir: Path | str,
    ) -> None:
        self._character_repository = character_repository
        # Empty path → upload disabled but the rest of the API
        # (rename / remove / strength adjust) still works because it
        # only touches the character row.
        self._lora_dir = Path(lora_dir) if lora_dir else None
        if self._lora_dir is not None:
            self._lora_dir.mkdir(parents=True, exist_ok=True)

    async def upload(
        self,
        character_id: str,
        *,
        data: bytes,
        original_filename: str,
        strength: float = 1.0,
    ) -> Character:
        """Write the file to ``lora_dir`` and append it to the character.

        The stored filename is sanitised (only the basename is kept) so
        an operator uploading ``../foo/bar.safetensors`` can't escape
        the target directory. When a file of the same name already
        exists on disk we don't overwrite — operators may have placed
        it there intentionally; we just attach it to this character.
        """
        if self._lora_dir is None:
            raise LoraUploadDisabledError(
                "LoRA upload is disabled — set KOKORO_COMFYUI_LORA_DIR to "
                "ComfyUI's models/loras/ path first.",
            )
        if len(data) > MAX_LORA_BYTES:
            raise LoraTooLargeError(
                f"LoRA exceeds {MAX_LORA_BYTES // (1024 * 1024)} MB limit",
            )
        safe_name = _sanitise_filename(original_filename)
        if not safe_name:
            raise UnsupportedLoraTypeError(
                "LoRA filename must have a supported extension: "
                + ", ".join(sorted(_ALLOWED_EXTENSIONS)),
            )

        target = self._lora_dir / safe_name
        if not target.exists():
            target.write_bytes(data)
        else:
            _LOGGER.info(
                "LoRA %s already present on disk; attaching without overwrite",
                safe_name,
            )

        character = await self._load_character(character_id)
        existing = [lora for lora in character.loras if lora.name != safe_name]
        updated = character.with_loras(
            existing + [CharacterLora(name=safe_name, strength=strength)],
        )
        await self._character_repository.save(updated)
        return updated

    async def attach_existing(
        self,
        character_id: str,
        *,
        name: str,
        strength: float = 1.0,
    ) -> Character:
        """Attach a LoRA that's already on disk (or managed externally).

        Useful when ``lora_dir`` is unset but operator has placed
        files in ComfyUI's directory manually — they still need a way
        to tell the character "use this one".
        """
        safe = _sanitise_filename(name)
        if not safe:
            raise UnsupportedLoraTypeError(
                "LoRA name must include a supported extension",
            )
        character = await self._load_character(character_id)
        existing = [lora for lora in character.loras if lora.name != safe]
        updated = character.with_loras(
            existing + [CharacterLora(name=safe, strength=strength)],
        )
        await self._character_repository.save(updated)
        return updated

    async def set_strength(
        self,
        character_id: str,
        *,
        name: str,
        strength: float,
    ) -> Character:
        character = await self._load_character(character_id)
        target_name = name.strip()
        found = False
        new_loras: list[CharacterLora] = []
        for lora in character.loras:
            if lora.name == target_name:
                new_loras.append(CharacterLora(name=lora.name, strength=strength))
                found = True
            else:
                new_loras.append(lora)
        if not found:
            raise LoraNotFoundError(
                f"Character has no LoRA named {target_name!r}",
            )
        updated = character.with_loras(new_loras)
        await self._character_repository.save(updated)
        return updated

    async def remove(self, character_id: str, *, name: str) -> Character:
        character = await self._load_character(character_id)
        target_name = name.strip()
        remaining = [l for l in character.loras if l.name != target_name]
        if len(remaining) == len(character.loras):
            raise LoraNotFoundError(
                f"Character has no LoRA named {target_name!r}",
            )
        updated = character.with_loras(remaining)
        await self._character_repository.save(updated)
        return updated

    def list_available(self) -> list[str]:
        """Return filenames present in ``lora_dir``.

        Sorted ascending; empty when ``lora_dir`` is unset or missing.
        Used by the UI to show an "attach existing" picker when the
        operator already has their loras curated outside Yuralume.
        """
        if self._lora_dir is None or not self._lora_dir.exists():
            return []
        return sorted(
            p.name for p in self._lora_dir.iterdir()
            if p.is_file() and p.suffix.lower() in _ALLOWED_EXTENSIONS
        )

    async def _load_character(self, character_id: str) -> Character:
        character = await self._character_repository.get(character_id)
        if character is None:
            raise LoraNotFoundError("Character not found")
        return character


def _sanitise_filename(raw: str) -> str | None:
    """Return basename with a supported extension, or ``None``.

    Rejects directory-traversal attempts and extensions outside the
    whitelist. We drop any subdirectory component — ComfyUI LoRAs all
    sit in a flat ``models/loras/`` directory, and nested structure
    wouldn't be referenced correctly from the workflow anyway.
    """
    if not raw:
        return None
    name = Path(raw).name  # strips directories
    if not name or name in {".", ".."}:
        return None
    if Path(name).suffix.lower() not in _ALLOWED_EXTENSIONS:
        return None
    return name
