from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from uuid import uuid4

import httpx

from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudIdentityUnavailable,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.infrastructure.http_error_logging import (
    log_expected_refusal,
    log_http_error_response,
)
from kokoro_link.infrastructure.llm.cloud_refusal import (
    ExpectedCloudRefusal,
    classify_refusal,
)

_LOGGER = logging.getLogger(__name__)


class CloudGatewayChatModel(ChatModelPort):
    def __init__(
        self,
        *,
        base_url: str,
        deployment_token: str,
        default_model: str,
        feature_key: str,
        identity: CloudGatewayIdentity | None,
        deployment_id: str = "hosted-primary",
        audience: str = "yuralume-gateway",
        provider_id: str = "yuralume_cloud",
        timeout_seconds: float = 300.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("cloud gateway base_url is required")
        if not deployment_token.strip():
            raise ValueError("cloud gateway deployment_token is required")
        if not deployment_id.strip():
            raise ValueError("cloud gateway deployment_id is required")
        if not audience.strip():
            raise ValueError("cloud gateway audience is required")
        if not default_model.strip():
            raise ValueError("cloud gateway default_model is required")
        self.provider_id = provider_id
        self.supports_vision = True
        self._base_url = base_url.rstrip("/")
        self._deployment_token = deployment_token
        self._deployment_id = deployment_id.strip()
        self._audience = audience.strip()
        self._default_model = default_model
        self._feature_key = feature_key.strip() or "chat"
        self._identity = identity
        self._timeout = httpx.Timeout(
            connect=10.0,
            read=max(30.0, timeout_seconds),
            write=30.0,
            pool=10.0,
        )
        self.last_request_id = ""

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        payload = self._build_payload(
            prompt,
            image_urls=image_urls,
            model=model,
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/v1/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            _raise_with_body(response, context=self._refusal_log_context())
            data = response.json()
        return str(data["choices"][0]["message"]["content"])

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        payload = self._build_payload(
            prompt,
            image_urls=image_urls,
            model=model,
            stream=True,
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/v1/chat/completions",
                headers=self._headers(),
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    _raise_stream_error(
                        response, body, context=self._refusal_log_context(),
                    )
                async for chunk in _iter_openai_stream(response):
                    yield chunk

    async def list_models(self) -> list[str]:
        return [self._default_model]

    def _build_payload(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str],
        model: str | None,
        stream: bool = False,
    ) -> dict:
        if image_urls:
            content: list[dict] = [{"type": "text", "text": prompt}]
            for url in image_urls:
                if url:
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": url},
                    })
            user_message: dict = {"role": "user", "content": content}
        else:
            user_message = {"role": "user", "content": prompt}
        payload: dict = {
            "model": (model or "").strip() or self._default_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a roleplay character backend.",
                },
                user_message,
            ],
        }
        if stream:
            payload["stream"] = True
        return payload

    def _headers(self) -> dict[str, str]:
        identity = self._require_identity()
        request_id = f"llm-{uuid4().hex}"
        self.last_request_id = request_id
        return {
            "Authorization": f"Bearer {self._deployment_token}",
            "X-Yuralume-Deployment": self._deployment_id,
            "X-Yuralume-Audience": self._audience,
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
            "X-Yuralume-Tenant": identity.tenant_id,
            "X-Yuralume-Account": identity.account_id,
            "X-Yuralume-Feature": self._feature_key,
            "X-Yuralume-Character": identity.character_ref,
        }

    def _require_identity(self) -> CloudGatewayIdentity:
        if self._identity is None:
            raise CloudIdentityUnavailable(
                "cloud gateway LLM calls require cloud account identity",
            )
        return self._identity

    def _refusal_log_context(self) -> str:
        """Identity tags for a refusal WARNING — which call the cloud refused."""
        identity = self._identity
        if identity is None:
            return f"feature={self._feature_key}"
        return (
            f"feature={self._feature_key} "
            f"character={identity.character_ref} "
            f"account={identity.account_id}"
        )


async def _iter_openai_stream(response: httpx.Response) -> AsyncIterator[str]:
    async for line in response.aiter_lines():
        if not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        try:
            content = chunk["choices"][0].get("delta", {}).get("content", "")
        except (KeyError, IndexError, TypeError):
            continue
        if content:
            yield str(content)


def _raise_with_body(response: httpx.Response, *, context: str = "") -> None:
    if response.status_code < 400:
        return
    body_text = response.text
    _raise_refusal_or_error(
        response, body_text, operation="Cloud Gateway LLM", context=context,
    )


def _raise_stream_error(
    response: httpx.Response, body: bytes, *, context: str = "",
) -> None:
    text = body.decode("utf-8", errors="replace")
    _raise_refusal_or_error(
        response, text, operation="Cloud Gateway LLM stream", context=context,
    )


def _raise_refusal_or_error(
    response: httpx.Response,
    body_text: str,
    *,
    operation: str,
    context: str,
) -> None:
    summary = f"{response.status_code} from {response.request.url}: {body_text[:500]}"
    refusal = classify_refusal(response.status_code, body_text)
    if refusal is not None:
        code, message = refusal
        log_expected_refusal(
            _LOGGER,
            response,
            operation=operation,
            code=code,
            message=message,
            context=context,
        )
        raise ExpectedCloudRefusal(
            summary, request=response.request, response=response, code=code,
        )
    log_http_error_response(
        _LOGGER, response, operation=operation, body_text=body_text,
    )
    raise httpx.HTTPStatusError(
        summary, request=response.request, response=response,
    )
