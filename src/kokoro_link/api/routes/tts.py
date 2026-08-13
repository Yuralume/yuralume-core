"""TTS HTTP route — on-demand synth for chat bubbles.

Spike-stage surface: one ``POST /api/v1/characters/{id}/tts`` endpoint
that takes the bubble text in the body and returns a URL the browser
can play. The frontend never holds the audio bytes — they live as
files under ``uploads/tts/`` so refreshes / repeat plays are free.

Status codes:

* ``200`` — synth (or cache hit). Body has ``audio_url`` + ``cached``.
* ``404`` — character id doesn't exist.
* ``422`` — empty text.
* ``402`` — hosted tenant is out of credits (structured
  ``{"code": "insufficient_credits"}`` detail). Nothing was synthesised.
* ``503`` — TTS not configured (env vars not set, or backend
  unreachable). UI greys out the play button on this code.
* ``502`` — TTS backend reachable but synth failed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import (
    ensure_owned_character_id,
    get_container,
    get_current_user_id,
)
from kokoro_link.api.routes._cloud_errors import insufficient_credits_guard
from kokoro_link.application.services.tts_service import TTSService
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.tts import TTSError, TTSUnavailable
from kokoro_link.contracts.tts_catalog import TTSVoice

router = APIRouter(tags=["tts"])


class TTSAssetEntry(BaseModel):
    """Legacy shape kept so older frontends can consume empty lists."""

    path: str
    relative: str
    absolute_path: str
    prompt_hint: str | None = None


class TTSVoicePresetEntry(BaseModel):
    """One product-facing voice option returned by the external TTS API."""

    id: str
    label: str
    voice_id: str
    ref_audio_path: str = ""
    prompt_text: str = ""
    prompt_lang: str = ""
    gpt_weights_path: str = ""
    sovits_weights_path: str = ""
    is_complete: bool

    @classmethod
    def from_voice(cls, voice: TTSVoice) -> "TTSVoicePresetEntry":
        return cls(
            id=voice.id,
            label=voice.label,
            voice_id=voice.id,
            prompt_lang=voice.prompt_lang,
            is_complete=voice.is_complete,
        )


class TTSAssetCatalogResponse(BaseModel):
    enabled: bool
    """``False`` means no external TTS voice catalog is configured."""
    install_dir: str | None = None
    ref_audios: list[TTSAssetEntry]
    gpt_weights: list[TTSAssetEntry]
    sovits_weights: list[TTSAssetEntry]
    voice_presets: list[TTSVoicePresetEntry] = []


class TTSSynthRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class TTSSynthResponse(BaseModel):
    audio_url: str
    cached: bool


@router.post(
    "/characters/{character_id}/tts",
    response_model=TTSSynthResponse,
)
async def synthesize_character_tts(
    character_id: str,
    payload: TTSSynthRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
    _owned_character_id: str = Depends(ensure_owned_character_id),
) -> TTSSynthResponse:
    nsfw_mode = getattr(container, "nsfw_mode_service", None)
    if (
        nsfw_mode is not None
        and await nsfw_mode.active_target(user_id=current_user_id) is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="TTS is disabled while NSFW mode is active",
        )
    service: TTSService | None = container.tts_service
    if service is None or not service.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TTS is not configured",
        )
    try:
        with insufficient_credits_guard():
            result = await service.synthesize(
                character_id=character_id, text=payload.text,
            )
    except TTSUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    except TTSError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        )
    return TTSSynthResponse(audio_url=result.audio_url, cached=result.cached)


async def _resolve_owned_character_id(
    *,
    character_id: str | None,
    current_user_id: str,
    container: ServiceContainer,
) -> str | None:
    """Scope an optional ``character_id`` query param to its owner (EC5-C).

    A missing, unknown, or foreign-owned id degrades to ``None`` (no
    character context) rather than raising — this is a catalog-listing
    convenience endpoint, not an authorization boundary, so a stale id
    (e.g. a deleted character) should never fail the whole picker. It
    just loses the one benefit an id would have added: seeing that
    character's own bound exclusive voice.
    """
    if not character_id:
        return None
    repository = getattr(container, "character_repository", None)
    if repository is None:
        return None
    character = await repository.get(character_id)
    if character is None or character.user_id != current_user_id:
        return None
    return character.id


@router.get("/tts/assets", response_model=TTSAssetCatalogResponse)
async def list_tts_assets(
    character_id: str | None = None,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> TTSAssetCatalogResponse:
    """Enumerate external voice options.

    The historical route name is kept for compatibility, but the app no
    longer scans local GPT-SoVITS files. Voices come from the configured
    provider's catalog API.

    ``character_id`` is an optional query param (EC5-C): a voice reserved
    for a cloud-exclusive card is excluded from this listing by default,
    and only included when the request names the character it is bound
    to (ownership-checked below). Self-host and BYOK catalogs ignore the
    parameter entirely — they carry no exclusive-voice concept — so this
    is byte-identical to the pre-EC5-C response for every deployment
    without a cloud-exclusive voice catalog.
    """
    catalog = container.tts_voice_catalog
    if catalog is None:
        return TTSAssetCatalogResponse(
            enabled=False,
            install_dir=None,
            ref_audios=[],
            gpt_weights=[],
            sovits_weights=[],
            voice_presets=[],
        )
    owned_character_id = await _resolve_owned_character_id(
        character_id=character_id,
        current_user_id=current_user_id,
        container=container,
    )
    try:
        voices = await catalog.list_voices(character_id=owned_character_id)
    except TTSUnavailable:
        voices = []
    return TTSAssetCatalogResponse(
        enabled=bool(voices),
        install_dir=None,
        ref_audios=[],
        gpt_weights=[],
        sovits_weights=[],
        voice_presets=[TTSVoicePresetEntry.from_voice(v) for v in voices],
    )


__all__ = ["router"]
