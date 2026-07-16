from __future__ import annotations

import base64
import logging
from collections.abc import Mapping, Sequence
from urllib.parse import urljoin
from uuid import uuid4

import httpx

from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentityResolverPort,
    CloudResourceContext,
)
from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
    ImageTokenUsage,
    ImageTimeoutError,
)
from kokoro_link.infrastructure.http_error_logging import log_http_error_response
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_visual_identity_lines,
)
from kokoro_link.infrastructure.prompt.visual_subject import (
    render_character_visual_subject_lines,
)


ASPECT_TO_SIZE: dict[str, str] = {
    "portrait": "1024x1536",
    "landscape": "1536x1024",
    "square": "1024x1024",
}

_LOGGER = logging.getLogger(__name__)


class CloudGatewayImageProvider:
    provider_id = "cloud_gateway"

    def __init__(
        self,
        *,
        base_url: str,
        deployment_token: str,
        preset: str,
        feature_key: str,
        identity_resolver: CloudGatewayIdentityResolverPort,
        deployment_id: str = "hosted-primary",
        audience: str = "yuralume-gateway",
        timeout_seconds: float = 180.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("cloud gateway image base_url is required")
        if not deployment_token.strip():
            raise ValueError("cloud gateway deployment_token is required")
        if not deployment_id.strip():
            raise ValueError("cloud gateway deployment_id is required")
        if not audience.strip():
            raise ValueError("cloud gateway audience is required")
        if not preset.strip():
            raise ValueError("cloud gateway image preset is required")
        self._base_url = base_url.rstrip("/")
        self._deployment_token = deployment_token
        self._deployment_id = deployment_id.strip()
        self._audience = audience.strip()
        self._preset = preset
        self._feature_key = feature_key.strip() or "image"
        self._identity_resolver = identity_resolver
        self._timeout = timeout_seconds
        self.last_request_id = ""
        self.last_provider_id = self.provider_id
        self.last_model_id = preset
        self.last_usage: ImageTokenUsage | None = None
        self.last_cost_amount_usd: float | None = None

    async def generate(
        self,
        *,
        character,
        positive: str,
        aspect: str = "portrait",
        batch: int = 1,
        recent_dialogue: str = "",
        use_runtime_state: bool = True,
        user_attachment_urls: Sequence[str] = (),
    ) -> list[bytes]:
        self.last_provider_id = self.provider_id
        self.last_model_id = self._preset
        self.last_usage = None
        self.last_cost_amount_usd = None
        prompt = _build_prompt(
            character=character,
            positive=positive,
            recent_dialogue=recent_dialogue,
            use_runtime_state=use_runtime_state,
            user_attachment_urls=user_attachment_urls,
        )
        if not prompt.strip():
            raise ImageGenerationError("image prompt is empty")
        payload = {
            "model": self._preset,
            "prompt": prompt,
            "size": ASPECT_TO_SIZE.get(aspect, ASPECT_TO_SIZE["portrait"]),
            "n": max(1, min(int(batch), 4)),
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/v1/images/generations",
                    headers=await self._headers(character),
                    json=payload,
                )
                data = _json_or_raise(response, "image")
                self._capture_usage_metadata(data)
                return await _image_bytes_from_response(
                    data,
                    client=client,
                    base_url=self._base_url,
                )
        except httpx.TimeoutException as exc:
            raise ImageTimeoutError("cloud image gateway timed out") from exc
        except ImageGenerationError:
            raise
        except Exception as exc:
            raise ImageGenerationError(str(exc)) from exc

    def _capture_usage_metadata(self, data: Mapping) -> None:
        self.last_usage = ImageTokenUsage.from_mapping(data.get("usage"))
        model = data.get("model")
        if isinstance(model, str) and model:
            self.last_model_id = model
        yuralume = data.get("yuralume")
        if not isinstance(yuralume, Mapping):
            return
        provider = yuralume.get("provider")
        if isinstance(provider, str) and provider:
            self.last_provider_id = provider
        provider_model = yuralume.get("provider_model")
        if isinstance(provider_model, str) and provider_model:
            self.last_model_id = provider_model
        cost = yuralume.get("cost_estimate")
        if isinstance(cost, Mapping):
            amount = cost.get("total_usd")
            if isinstance(amount, int | float):
                self.last_cost_amount_usd = float(amount)

    async def _headers(self, character) -> dict[str, str]:
        identity = await self._identity_resolver.resolve_context(
            CloudResourceContext.for_character(character),
        )
        request_id = f"img-{uuid4().hex}"
        self.last_request_id = request_id
        return {
            "Authorization": f"Bearer {self._deployment_token}",
            "X-Yuralume-Deployment": self._deployment_id,
            "X-Yuralume-Audience": self._audience,
            "X-Request-Id": request_id,
            "X-Yuralume-Tenant": identity.tenant_id,
            "X-Yuralume-Account": identity.account_id,
            "X-Yuralume-Feature": self._feature_key,
            "X-Yuralume-Character": identity.character_ref,
        }


def _build_prompt(
    *,
    character,
    positive: str,
    recent_dialogue: str,
    use_runtime_state: bool,
    user_attachment_urls: Sequence[str],
) -> str:
    parts = [
        f"Character: {character.name}",
        f"Appearance: {getattr(character, 'appearance', '')}",
        *render_character_visual_identity_lines(character),
        *render_character_visual_subject_lines(character),
    ]
    if use_runtime_state:
        state = getattr(character, "state", None)
        if state is not None:
            parts.append(f"Current emotion: {getattr(state, 'emotion', '')}")
            intent = getattr(state, "current_intent", None)
            if intent:
                parts.append(f"Current intent: {intent}")
    if positive.strip():
        parts.append(f"Scene: {positive.strip()}")
    if recent_dialogue.strip():
        parts.append(f"Recent dialogue context: {recent_dialogue.strip()}")
    if user_attachment_urls:
        parts.append(
            "User visual references: "
            + ", ".join(str(url) for url in user_attachment_urls),
        )
    return "\n".join(part for part in parts if part.strip())


async def _image_bytes_from_response(
    data: Mapping,
    *,
    client: httpx.AsyncClient,
    base_url: str,
) -> list[bytes]:
    out: list[bytes] = []
    items = data.get("data")
    if not isinstance(items, list):
        raise ImageNoOutputError("cloud image gateway returned no data array")
    for item in items:
        if not isinstance(item, Mapping):
            continue
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            out.append(base64.b64decode(b64))
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            out.append(await _download_bytes(client=client, url=url, base_url=base_url))
    if not out:
        raise ImageNoOutputError("cloud image gateway produced no images")
    return out


async def _download_bytes(
    *,
    client: httpx.AsyncClient,
    url: str,
    base_url: str,
) -> bytes:
    resolved = url if url.startswith(("http://", "https://")) else urljoin(
        f"{base_url}/",
        url.lstrip("/"),
    )
    response = await client.get(resolved)
    if response.status_code >= 400:
        log_http_error_response(
            _LOGGER,
            response,
            operation="cloud image artifact download",
        )
        raise ImageGenerationError(
            f"image artifact download failed: {response.status_code}",
        )
    return response.content


def _json_or_raise(response: httpx.Response, label: str) -> Mapping:
    if response.status_code >= 400:
        log_http_error_response(
            _LOGGER,
            response,
            operation=f"cloud {label} gateway",
        )
        raise ImageGenerationError(
            f"{label} gateway error {response.status_code}: {response.text}",
        )
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ImageGenerationError(f"{label} gateway returned non-object JSON")
    return payload
