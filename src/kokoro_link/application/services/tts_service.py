"""TTS orchestrator — caches synth output in Object Storage by content hash.

Spike-stage: messages don't have stable ids, so we identify a synth
job by ``sha256(character_id + voice_config + text)``. Same character
asked to say the same thing with the same voice config → same object
URL → instant replay. A voice-config change (different ref audio)
yields a different hash and a fresh synth, which is what we want.

Objects land at ``tts/<character_id>/<hash>.<ext>``. Hashing the
voice config means upgrading the env var auto-invalidates the cache
without us needing a manual purge step.

Fail modes propagate as the port's exception types — the route layer
maps them to 503 / 502.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.bootstrap.settings import TTSSettings
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.generation_usage import (
    UsageEventDraft,
    UsageEventRecorderPort,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.contracts.tts import (
    TTSError,
    TTSPort,
    TTSRequest,
    TTSUnavailable,
    TTSWeights,
)
from kokoro_link.contracts.tts_translator import TTSTranslatorPort
from kokoro_link.domain.entities.generation_usage import (
    CAPABILITY_TTS,
    STATUS_CACHED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    UsageQuantity,
)
from kokoro_link.domain.value_objects.voice_profile import VoiceProfile

_LOGGER = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 800
"""Hard ceiling on a single synth request. GPT-SoVITS handles long
text via internal chunking, but very long replies blow up the cache
and the latency. 800 chars covers any realistic chat bubble; longer
inputs are rejected at the service edge."""


@dataclass(frozen=True, slots=True)
class TTSSynthResult:
    """Service-level result: the URL the frontend can play, plus a
    cache-hit flag so the UI can show "已生成" vs. "剛合成"."""

    audio_url: str
    cached: bool


class TTSService:
    def __init__(
        self,
        *,
        port: TTSPort,
        settings: TTSSettings,
        uploads_dir: Path,
        url_prefix: str = "/uploads",
        translator: TTSTranslatorPort | None = None,
        character_repository: CharacterRepositoryPort | None = None,
        object_storage: ObjectStoragePort | None = None,
        usage_recorder: UsageEventRecorderPort | None = None,
        account_runtime_profile_resolver: (
            AccountRuntimeProfileResolverPort | None
        ) = None,
        subscription_access_guard: SubscriptionAccessGuard | None = None,
    ) -> None:
        self._port = port
        self._settings = settings
        _ = uploads_dir, url_prefix
        self._object_storage = object_storage
        # Optional pre-TTS translator. Only invoked when
        # ``settings.translate_target_lang`` is non-empty AND differs
        # from ``text_lang``. Empty translator return falls back to the
        # source text — keeps tests / no-LLM deployments working.
        self._translator = translator
        # Optional. When wired, the service reads the character's
        # ``voice_profile`` and merges it over the global settings so
        # each character can have their own voice. Without it, all
        # characters share the global ``KOKORO_TTS_*`` config.
        self._characters = character_repository
        self._usage_recorder = usage_recorder
        self._account_runtime_profile_resolver = (
            account_runtime_profile_resolver
            or PermissiveAccountRuntimeProfileResolver()
        )
        self._subscription_access_guard = subscription_access_guard

    @property
    def enabled(self) -> bool:
        """Whether the TTS framework is wired enough that *some*
        character could synthesise. Per-character profiles can fill
        in missing global ref/prompt, so the gate is just ``base_url``
        — without that no character can synth."""
        return self._settings.enabled

    def set_runtime_backend(self, *, port: TTSPort, settings: TTSSettings) -> None:
        """Swap the runtime TTS backend after Admin BYOK settings change."""
        self._port = port
        self._settings = settings

    async def synthesize(
        self,
        *,
        character_id: str,
        text: str,
    ) -> TTSSynthResult:
        """Synth ``text`` for ``character_id``, applying the character's
        :class:`VoiceProfile` over the global settings if one exists.
        Cached files replay instantly on repeat calls."""
        cleaned = (text or "").strip()
        if not cleaned:
            raise TTSError("TTS input text is empty")
        await self._ensure_subscription_access(character_id)
        await self._ensure_tts_enabled_by_runtime_profile(character_id)
        if self._object_storage is None:
            raise TTSError("Object storage is not configured")
        if len(cleaned) > _MAX_TEXT_CHARS:
            cleaned = cleaned[:_MAX_TEXT_CHARS]

        # Resolve effective config: per-character profile overlaid on
        # global TTSSettings. ``profile.enabled=False`` short-circuits
        # to "TTS disabled for this character" (route returns 503).
        config = await self._resolve_config(character_id)
        if not config.enabled:
            raise TTSUnavailable(
                "TTS not configured" if not self._settings.enabled
                else "TTS disabled for this character"
            )

        digest = self._source_fingerprint(character_id, cleaned, config)
        cached_url = await self._cache_url(character_id, digest, ext="wav")
        if await self._cache_exists(character_id, digest, ext="wav"):
            await self._record_usage_safely(
                character_id=character_id,
                text=cleaned,
                config=config,
                cached=True,
                status=STATUS_CACHED,
                content_hash=digest,
            )
            return TTSSynthResult(
                audio_url=cached_url,
                cached=True,
            )

        synth_text, synth_lang = await self._maybe_translate(cleaned, config)
        request = self._build_request(
            synth_text,
            synth_lang,
            config,
            character_id=character_id,
        )
        started_at = datetime.now(timezone.utc)
        try:
            result = await self._port.synthesize(request)
        except Exception as exc:
            await self._record_usage_safely(
                character_id=character_id,
                text=synth_text,
                config=config,
                cached=False,
                status=STATUS_FAILED,
                content_hash=digest,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            raise
        ext = _ext_from_media_type(result.media_type)
        try:
            stored = await self._object_storage.put_bytes(
                object_key=self._cache_object_key(
                    character_id, digest, ext=ext,
                ),
                content=result.audio,
                content_type=result.media_type,
                metadata={"character_id": character_id, "kind": "tts"},
            )
            audio_url = stored.url
        except Exception as exc:
            _LOGGER.exception("tts: failed to write cached audio object")
            raise TTSError(f"TTS cache write failed: {exc!s}") from exc
        await self._record_usage_safely(
            character_id=character_id,
            text=synth_text,
            config=config,
            cached=False,
            status=STATUS_SUCCEEDED,
            content_hash=digest,
            output_bytes=len(result.audio),
            started_at=started_at,
            metadata={"media_type": result.media_type},
        )
        return TTSSynthResult(
            audio_url=audio_url,
            cached=False,
        )

    # ------------------------------------------------------------------

    async def _resolve_config(self, character_id: str) -> "_EffectiveConfig":
        """Merge the character's ``VoiceProfile`` over global settings.

        Order of precedence per field:
          1. ``VoiceProfile`` non-empty value
          2. Global ``TTSSettings`` value
        ``enabled`` short-circuits when the profile explicitly disables
        TTS for this character even though the rest is configured.
        """
        cfg = self._settings
        profile: VoiceProfile | None = None
        if self._characters is not None:
            try:
                character = await self._characters.get(character_id)
            except Exception:
                _LOGGER.exception(
                    "tts: character lookup failed for %s; using global",
                    character_id,
                )
                character = None
            if character is not None:
                profile = character.voice_profile

        voice_id = (profile.voice_id if profile else "") or cfg.voice_id
        ref = (profile.ref_audio_path if profile else "") or cfg.ref_audio_path
        prompt = (profile.prompt_text if profile else "") or cfg.prompt_text
        plang = (profile.prompt_lang if profile else "") or cfg.prompt_lang
        # ``-`` is the sentinel for "explicitly disabled even though
        # global has one"; otherwise empty falls through to global.
        if profile and profile.translate_target_lang == "-":
            tlang_target = ""
        else:
            tlang_target = (
                (profile.translate_target_lang if profile else "")
                or cfg.translate_target_lang
            )

        return _EffectiveConfig(
            enabled=bool(cfg.base_url) and (profile.enabled if profile else True),
            voice_id=voice_id,
            ref_audio_path=ref,
            prompt_text=prompt,
            prompt_lang=plang or "zh",
            text_lang=cfg.text_lang or "zh",
            translate_target_lang=tlang_target,
            text_split_method=cfg.text_split_method,
            top_k=cfg.top_k,
            top_p=cfg.top_p,
            temperature=cfg.temperature,
            speed_factor=cfg.speed_factor,
            gpt_weights_path=(profile.gpt_weights_path if profile else ""),
            sovits_weights_path=(profile.sovits_weights_path if profile else ""),
        )

    async def _ensure_subscription_access(self, character_id: str) -> None:
        if self._subscription_access_guard is None:
            return
        if self._characters is None:
            raise TTSUnavailable(
                "TTS subscription guard requires character persistence",
            )
        character = await self._characters.get(character_id)
        if character is None:
            raise TTSUnavailable("Character not found")
        await self._subscription_access_guard.ensure_character_allowed(character)

    async def _ensure_tts_enabled_by_runtime_profile(
        self,
        character_id: str,
    ) -> None:
        if self._characters is None:
            # No character repository → no operator to scope account policy
            # to, so there is nothing to gate. Hosted deployments always wire
            # one; this only happens in minimal/self-host setups that have no
            # runtime-profile gating, where permissive is the correct default.
            return
        try:
            character = await self._characters.get(character_id)
            if character is None:
                raise TTSUnavailable("Character not found")
            profile = await self._account_runtime_profile_resolver.resolve_for_operator(
                character.user_id,
            )
        except TTSUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception(
                "tts runtime profile lookup failed character=%s",
                character_id,
            )
            raise TTSUnavailable(
                "TTS disabled because the account runtime profile cannot be resolved",
            ) from exc
        if not profile.tts_enabled:
            raise TTSUnavailable(
                "TTS disabled for this account runtime profile",
            )

    async def _maybe_translate(
        self, text: str, config: "_EffectiveConfig",
    ) -> tuple[str, str]:
        """Run the pre-TTS translator when configured.

        Returns ``(synth_text, synth_lang)`` — the actual string to
        feed into TTS plus the language tag to declare on the request.
        Translator returning empty (LLM unavailable / failed) falls
        back to source text so the play button still produces audio."""
        target = (config.translate_target_lang or "").strip()
        if not target or target == config.text_lang or self._translator is None:
            return text, config.text_lang
        try:
            translated = await self._translator.translate(
                text=text,
                source_lang=config.text_lang,
                target_lang=target,
            )
        except Exception:
            _LOGGER.exception("tts: translator crashed; falling back to source")
            return text, config.text_lang
        translated = (translated or "").strip()
        if not translated:
            _LOGGER.warning(
                "tts: translator returned empty; synth in source lang %s",
                config.text_lang,
            )
            return text, config.text_lang
        return translated, target

    def _build_request(
        self,
        text: str,
        text_lang: str,
        config: "_EffectiveConfig",
        *,
        character_id: str,
    ) -> TTSRequest:
        return TTSRequest(
            text=text,
            character_id=character_id,
            voice_id=config.voice_id,
            ref_audio_path=config.ref_audio_path,
            prompt_text=config.prompt_text,
            prompt_lang=config.prompt_lang,
            text_lang=text_lang,
            text_split_method=config.text_split_method,
            top_k=config.top_k,
            top_p=config.top_p,
            temperature=config.temperature,
            speed_factor=config.speed_factor,
            weights=TTSWeights(
                gpt_weights_path=config.gpt_weights_path,
                sovits_weights_path=config.sovits_weights_path,
            ),
        )

    def _source_fingerprint(
        self, character_id: str, source_text: str, config: "_EffectiveConfig",
    ) -> str:
        """Cache fingerprint based on **source** text + effective config.

        Includes per-character voice config so different characters with
        different profiles never collide. Translation output is
        non-deterministic so we hash on source text not translated text."""
        h = hashlib.sha256()
        h.update(character_id.encode("utf-8"))
        h.update(b"\0")
        for key, value in (
            ("text", source_text),
            ("voice", config.voice_id),
            ("ref", config.ref_audio_path),
            ("prompt", config.prompt_text),
            ("plang", config.prompt_lang),
            ("tlang", config.text_lang),
            ("ttarget", config.translate_target_lang or ""),
            ("split", config.text_split_method),
            ("topk", str(config.top_k)),
            ("topp", str(config.top_p)),
            ("temp", str(config.temperature)),
            ("speed", str(config.speed_factor)),
            ("gpt", config.gpt_weights_path),
            ("sovits", config.sovits_weights_path),
        ):
            h.update(key.encode("utf-8"))
            h.update(b"=")
            h.update(value.encode("utf-8"))
            h.update(b"\0")
        return h.hexdigest()[:32]

    def _cache_object_key(self, character_id: str, digest: str, *, ext: str) -> str:
        return f"tts/{character_id}/{digest}.{ext}"

    async def _cache_exists(self, character_id: str, digest: str, *, ext: str) -> bool:
        if self._object_storage is None:
            raise TTSError("Object storage is not configured")
        return (
            await self._object_storage.stat(
                object_key=self._cache_object_key(
                    character_id, digest, ext=ext,
                ),
            )
        ) is not None

    async def _cache_url(self, character_id: str, digest: str, *, ext: str) -> str:
        if self._object_storage is None:
            raise TTSError("Object storage is not configured")
        return await self._object_storage.public_url(
            object_key=self._cache_object_key(character_id, digest, ext=ext),
        )

    async def _record_usage_safely(
        self,
        *,
        character_id: str,
        text: str,
        config: "_EffectiveConfig",
        cached: bool,
        status: str,
        content_hash: str,
        output_bytes: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        started_at: datetime | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if self._usage_recorder is None:
            return
        completed_at = datetime.now(timezone.utc)
        latency_ms: int | None = None
        if started_at is not None:
            latency_ms = int((completed_at - started_at).total_seconds() * 1000)
        try:
            await self._usage_recorder.record(UsageEventDraft(
                capability=CAPABILITY_TTS,
                character_id=character_id,
                feature_key="tts_synthesis",
                source_surface="tts",
                upstream_request_id=(
                    ""
                    if cached
                    else str(getattr(self._port, "last_request_id", "") or "")
                ),
                provider_id=str(getattr(self._port, "provider_id", "") or ""),
                voice_id=config.voice_id,
                quantity=UsageQuantity(
                    usage_unit="character",
                    input_quantity=len(text),
                    total_quantity=len(text),
                    billable_quantity=0 if cached else len(text),
                ),
                cached=cached,
                latency_ms=latency_ms,
                status=status,
                error_code=error_code,
                error_message=error_message,
                output_bytes=output_bytes,
                content_hash=content_hash,
                metadata={
                    "text_lang": config.text_lang,
                    "translate_target_lang": config.translate_target_lang,
                    **dict(metadata or {}),
                },
                completed_at=completed_at,
            ))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("tts: usage recorder dispatch failed")


@dataclass(frozen=True, slots=True)
class _EffectiveConfig:
    """The merged voice config used for a single synth pass.

    Resolved fresh each request from
    ``character.voice_profile`` overlaid on global ``TTSSettings``.
    Internal to this module; no other layer should consume the type."""

    enabled: bool
    voice_id: str
    ref_audio_path: str
    prompt_text: str
    prompt_lang: str
    text_lang: str
    translate_target_lang: str
    text_split_method: str
    top_k: int
    top_p: float
    temperature: float
    speed_factor: float
    gpt_weights_path: str
    sovits_weights_path: str


def _ext_from_media_type(media_type: str) -> str:
    mt = (media_type or "").lower()
    if "wav" in mt:
        return "wav"
    if "mpeg" in mt or "mp3" in mt:
        return "mp3"
    if "ogg" in mt:
        return "ogg"
    return "bin"
