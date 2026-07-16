from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

import httpx

from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudGatewayIdentityResolverPort,
    CloudResourceContext,
)
from kokoro_link.contracts.cloud_routing_profile import CloudRoutingProfilePort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.contracts.tts import (
    TTSError,
    TTSRequest,
    TTSResult,
    TTSUnavailable,
)
from kokoro_link.contracts.tts_catalog import TTSVoice


# Forwarded feature string the upstream/usage ledger sees. Kept as ``tts`` for
# attribution continuity; the control-plane routes the voice default under the
# manifest's routable TTS key (below), which is an internal lookup detail.
_TTS_FEATURE_KEY = "tts"
_TTS_PROFILE_FEATURE_KEY = "tts_synthesis"


class CloudGatewayTTSAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        deployment_token: str,
        default_voice_id: str = "",
        character_repository: CharacterRepositoryPort,
        identity_resolver: CloudGatewayIdentityResolverPort,
        deployment_id: str = "hosted-primary",
        audience: str = "yuralume-gateway",
        routing_profile_port: CloudRoutingProfilePort | None = None,
        timeout_seconds: float = 90.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("cloud gateway TTS base_url is required")
        if not deployment_token.strip():
            raise ValueError("cloud gateway deployment_token is required")
        if not deployment_id.strip():
            raise ValueError("cloud gateway deployment_id is required")
        if not audience.strip():
            raise ValueError("cloud gateway audience is required")
        self._base_url = base_url.rstrip("/")
        self._deployment_token = deployment_token
        self._deployment_id = deployment_id.strip()
        self._audience = audience.strip()
        self._default_voice_id = default_voice_id
        self._characters = character_repository
        self._identity_resolver = identity_resolver
        self._routing_profile_port = routing_profile_port
        self._timeout = timeout_seconds
        self.last_request_id = ""

    async def list_voices(self) -> list[TTSVoice]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._base_url}/v1/voices",
                    headers=self._base_headers(request_prefix="tts"),
                )
            if response.status_code >= 400:
                raise TTSUnavailable(
                    f"cloud TTS voice catalog unavailable: {response.status_code}",
                )
            data = response.json()
        except httpx.HTTPError as exc:
            raise TTSUnavailable("cloud TTS voice catalog unreachable") from exc
        voices = data.get("voices") if isinstance(data, Mapping) else None
        if not isinstance(voices, list):
            return []
        out: list[TTSVoice] = []
        for item in voices:
            if not isinstance(item, Mapping):
                continue
            voice_id = str(item.get("id") or "").strip()
            if not voice_id:
                continue
            out.append(
                TTSVoice(
                    id=voice_id,
                    label=str(item.get("label") or voice_id),
                    prompt_lang=str(item.get("prompt_lang") or ""),
                    is_complete=bool(item.get("is_complete", True)),
                ),
            )
        return out

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        character_id = request.character_id.strip()
        if not character_id:
            raise TTSUnavailable("cloud TTS requires character_id")
        character = await self._characters.get(character_id)
        if character is None:
            raise TTSUnavailable("cloud TTS character not found")
        identity = await self._identity_resolver.resolve_context(
            CloudResourceContext.for_character(character),
        )
        voice_id = (request.voice_id or "").strip()
        if not voice_id:
            voice_id = await self._default_voice(identity)
        payload = {
            "text": request.text,
            "voice_id": voice_id,
            "feature_key": _TTS_FEATURE_KEY,
            "options": {
                "text_lang": request.text_lang,
                "prompt_lang": request.prompt_lang,
                "speed_factor": request.speed_factor,
            },
        }
        if not voice_id:
            payload.pop("voice_id")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/v1/tts/synthesize",
                    headers=self._identity_headers(identity),
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise TTSError("cloud TTS gateway timed out") from exc
        except httpx.HTTPError as exc:
            raise TTSUnavailable("cloud TTS gateway unreachable") from exc
        if response.status_code == 404:
            raise TTSUnavailable("cloud TTS voice not found")
        if response.status_code >= 400:
            raise TTSError(
                f"cloud TTS gateway error {response.status_code}: {response.text}",
            )
        return TTSResult(
            audio=response.content,
            media_type=response.headers.get("content-type") or "audio/wav",
        )

    async def _default_voice(self, identity: CloudGatewayIdentity) -> str:
        """Resolve the default voice from the control-plane profile when wired.

        Unlike LLM/image presets, a missing TTS voice is not a cost/safety
        boundary (the upstream catalog bounds available voices), so a profile miss
        degrades gracefully to the deprecated env voice default rather than failing
        closed.
        """
        if self._routing_profile_port is not None:
            profile = await self._routing_profile_port.get_profile(
                tenant_id=identity.tenant_id,
                account_id=identity.account_id,
                user_id=identity.account_id,
                tier=identity.tenant_tier,
            )
            voice = profile.preset_for("tts", _TTS_PROFILE_FEATURE_KEY)
            if voice:
                return voice
        return self._default_voice_id.strip()

    def _identity_headers(self, identity: CloudGatewayIdentity) -> dict[str, str]:
        headers = self._base_headers(request_prefix="tts")
        headers.update({
            "X-Yuralume-Tenant": identity.tenant_id,
            "X-Yuralume-Account": identity.account_id,
            "X-Yuralume-Feature": _TTS_FEATURE_KEY,
            "X-Yuralume-Character": identity.character_ref,
        })
        return headers

    def _base_headers(self, *, request_prefix: str) -> dict[str, str]:
        request_id = f"{request_prefix}-{uuid4().hex}"
        self.last_request_id = request_id
        return {
            "Authorization": f"Bearer {self._deployment_token}",
            "X-Yuralume-Deployment": self._deployment_id,
            "X-Yuralume-Audience": self._audience,
            "X-Request-Id": request_id,
        }
