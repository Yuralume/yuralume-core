from __future__ import annotations

import copy
import json
import logging
from collections.abc import AsyncIterator, Sequence
from uuid import uuid4

import httpx

from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudGatewayRequestTooLargeError,
    CloudIdentityUnavailable,
)
from kokoro_link.contracts.llm import ChatModelPort, ImageInputRejectedError
from kokoro_link.contracts.generation_trigger import (
    TRIGGER_HEADER_NAME,
    generation_trigger_header_value,
)
from kokoro_link.contracts.interaction_context import (
    covered_call_receipt,
    interaction_headers,
)
from kokoro_link.infrastructure.http_error_logging import (
    log_expected_refusal,
    log_http_error_response,
)
from kokoro_link.infrastructure.llm.cloud_refusal import (
    refusal_from_response,
    refusal_summary,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_MAX_REQUEST_BYTES = 224 * 1024
_IMAGE_REJECTION_STATUSES = frozenset({400, 404, 413, 415, 422})


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
        max_request_bytes: int = _DEFAULT_MAX_REQUEST_BYTES,
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
        if max_request_bytes <= 0:
            raise ValueError("cloud gateway max_request_bytes must be positive")
        self.provider_id = provider_id
        self.supports_vision = True
        self.prefers_public_image_urls = True
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
        self._max_request_bytes = max_request_bytes
        self.last_request_id = ""

    def with_supports_vision(self, value: bool) -> "CloudGatewayChatModel":
        """Return a copy whose vision capability the control plane pinned.

        This one adapter fronts every hosted model, so the constructor's
        ``supports_vision = True`` can only ever be a default — it says
        nothing about the preset a feature actually routes to. When the
        control-plane preset metadata carries an explicit ``supports_vision``,
        ``CloudActiveLLMProvider`` binds it here onto a copy; a preset nobody
        annotated keeps the constructor default and therefore the
        pre-existing behaviour. ``copy.copy`` leaves the instance the factory
        returned untouched, so binding can never mutate an adapter a caller
        already holds a reference to.
        """
        clone = copy.copy(self)
        clone.supports_vision = value
        return clone

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
        body = self._encode_request(payload)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/v1/chat/completions",
                headers=self._headers(),
                content=body,
            )
            _raise_with_body(
                response, payload=payload, context=self._refusal_log_context(),
            )
            # Covered-only, delivery-time: a 200 the Gateway still billed per
            # call (verifier failure, exhausted budget) is not paid for by the
            # action charge, and a body that never parses into content bought
            # nothing either. Both must stay refundable.
            receipt = covered_call_receipt(response.headers)
            data = response.json()
        content = str(data["choices"][0]["message"]["content"])
        receipt.mark_delivered()
        return content

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
        request_body = self._encode_request(payload)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/v1/chat/completions",
                headers=self._headers(),
                content=request_body,
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    _raise_stream_error(
                        response,
                        body,
                        payload=payload,
                        context=self._refusal_log_context(),
                    )
                receipt = covered_call_receipt(response.headers)
                async for chunk in _iter_openai_stream(response):
                    # Marked as the first real chunk is handed over, not after
                    # the ``yield`` resumes: a consumer that closes the stream
                    # right after the first token has still been served, and
                    # the ``GeneratorExit`` raised at the yield would otherwise
                    # skip the mark and turn "send, then stop" into a free
                    # generation. A stream that never produces content (only
                    # ``[DONE]`` / unparseable frames) stays refundable.
                    receipt.mark_delivered()
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

    def _encode_request(self, payload: dict) -> bytes:
        """Encode once, budget that exact body, then hand it to httpx."""
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        actual_bytes = len(body)
        if actual_bytes <= self._max_request_bytes:
            return body
        if _payload_has_image_parts(payload):
            raise ImageInputRejectedError(
                status_code=413,
                body=(
                    "serialized cloud gateway request exceeds the local "
                    f"{self._max_request_bytes}-byte budget"
                ),
            )
        raise CloudGatewayRequestTooLargeError(
            actual_bytes=actual_bytes,
            max_bytes=self._max_request_bytes,
        )

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
            TRIGGER_HEADER_NAME: generation_trigger_header_value(),
            # Read here, not at construction time: one cached adapter serves
            # every turn, and each turn is its own interaction. Empty outside
            # an action scope, which is the safe state — no header means the
            # Gateway keeps billing this call on its own.
            **interaction_headers(),
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


def _raise_with_body(
    response: httpx.Response,
    *,
    payload: object,
    context: str = "",
) -> None:
    if response.status_code < 400:
        return
    body_text = response.text
    _raise_refusal_or_error(
        response,
        body_text,
        payload=payload,
        operation="Cloud Gateway LLM",
        context=context,
    )


def _raise_stream_error(
    response: httpx.Response,
    body: bytes,
    *,
    payload: object,
    context: str = "",
) -> None:
    text = body.decode("utf-8", errors="replace")
    _raise_refusal_or_error(
        response,
        text,
        payload=payload,
        operation="Cloud Gateway LLM stream",
        context=context,
    )


def _raise_refusal_or_error(
    response: httpx.Response,
    body_text: str,
    *,
    payload: object,
    operation: str,
    context: str,
) -> None:
    summary = refusal_summary(response, body_text)
    refusal = refusal_from_response(response, body_text, summary=summary)
    cause = refusal or httpx.HTTPStatusError(
        summary, request=response.request, response=response,
    )
    if (
        response.status_code in _IMAGE_REJECTION_STATUSES
        and _payload_has_image_parts(payload)
        and (refusal is None or refusal.code == "provider_error")
    ):
        raise ImageInputRejectedError(
            status_code=response.status_code,
            body=body_text,
        ) from cause
    if refusal is not None:
        log_expected_refusal(
            _LOGGER,
            response,
            operation=operation,
            code=refusal.code,
            message=refusal.reason,
            context=context,
        )
        raise refusal
    log_http_error_response(
        _LOGGER, response, operation=operation, body_text=body_text,
    )
    raise cause


def _payload_has_image_parts(payload: object) -> bool:
    """Structural signal only; never classify by matching error text."""
    if not isinstance(payload, dict):
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                return True
    return False
