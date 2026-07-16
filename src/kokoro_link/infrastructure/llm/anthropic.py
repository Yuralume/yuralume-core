"""Anthropic Claude native adapter.

Claude's ``/v1/messages`` is close enough to OpenAI's chat/completions
that we *could* stuff it behind the OpenAI-compatible adapter, but the
cost is that we'd lose access to fields only Claude exposes (extended
thinking, the Claude-shaped tool-use format, etc.) and we'd have to
paper over three hard differences:

1. Auth header: ``x-api-key`` + ``anthropic-version``, not Bearer.
2. System prompt: top-level ``system`` field, not a message role.
3. ``max_tokens`` is mandatory. No default, request fails otherwise.

So we implement the port directly. Streaming reads Anthropic's SSE
event stream (``content_block_delta`` is where text tokens live) and
yields only the text so ChatService's token loop stays shape-agnostic.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence

import httpx

from kokoro_link.contracts.llm import ChatModelPort, ReasoningOverrides
from kokoro_link.contracts.provider_probe import (
    PROBE_CHAT_PROMPT,
    ProbeCheck,
    probe_http_client,
    probe_http_error_detail,
    run_probe_check,
)
from kokoro_link.infrastructure.http_error_logging import log_http_error_response

_LOGGER = logging.getLogger(__name__)

_MODEL_LIST_TTL_SECONDS = 60.0
"""Anthropic's model list changes on release cadence, not hot-reload
like LM Studio. Longer cache is fine."""

_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
"""Long read budget: extended-thinking / long multi-block replies can
run past 60s even on cloud Claude. Connect stays snappy so a down
endpoint fails fast."""

_SYSTEM_PROMPT = "You are a roleplay character backend."


class AnthropicChatModel(ChatModelPort):
    """Adapter for Anthropic's native ``/v1/messages`` API."""

    provider_id: str = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        model: str = "claude-sonnet-4-5",
        anthropic_version: str = "2023-06-01",
        supports_vision: bool = True,
        max_tokens: int = 4096,
        thinking_budget_tokens: int | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("AnthropicChatModel requires an api_key")
        self.supports_vision = supports_vision
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._version = anthropic_version
        self._max_tokens = max_tokens
        # Extended-thinking budget. Opt-in: unset means no ``thinking``
        # block is sent, so behaviour is identical to before. Anthropic
        # requires ``budget_tokens < max_tokens`` and forbids customising
        # some sampling params (e.g. ``temperature``) while thinking is on;
        # this adapter sends neither, so the constraint is only a concern
        # if someone later adds temperature support.
        self._thinking_budget_tokens = thinking_budget_tokens
        self._models_cache: list[str] | None = None
        self._models_cache_at: float = 0.0

    # ---- helpers ------------------------------------------------------

    def with_reasoning_overrides(
        self, overrides: ReasoningOverrides,
    ) -> "AnthropicChatModel":
        """Return a copy bound to a routing-level reasoning posture.

        Whole-trio replacement: the route's override supersedes the
        connection-level ``thinking_budget_tokens``, so an override
        without a budget turns extended thinking OFF for that route. A
        contradictory override (disable + budget) resolves to OFF —
        the conservative reading of operator intent.
        """
        clone = copy.copy(self)
        clone._thinking_budget_tokens = (
            None
            if overrides.disable_reasoning
            else overrides.thinking_budget_tokens
        )
        return clone

    def with_supports_vision(self, value: bool) -> "AnthropicChatModel":
        """Return a copy whose vision capability a routing entry overrides.

        Mirrors ``with_reasoning_overrides``: ``copy.copy`` shares the
        model-list cache with the base adapter and the registry singleton
        is never mutated. Rarely needed for Anthropic (its catalog models
        are multimodal), but supported so a routing entry can force
        text-only on a Claude route symmetrically with the aggregators.
        """
        clone = copy.copy(self)
        clone.supports_vision = value
        return clone

    def _resolve_model(self, override: str | None) -> str:
        if override is not None and override.strip():
            return override.strip()
        return self._model

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._version,
            "content-type": "application/json",
        }

    def _api_url(self, path: str) -> str:
        """Endpoint URL for ``path`` (e.g. ``messages`` / ``models``).

        Operators routinely paste a base URL that already ends in
        ``/v1`` (that's what the OpenAI-compatible fields want), which
        used to pass the probe but 404 at runtime (``/v1/v1/messages``).
        Tolerating the suffix here keeps probe and runtime resolving the
        SAME URL from the same config.
        """
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/{path}"
        return f"{self._base_url}/v1/{path}"

    def _build_user_content(
        self, prompt: str, image_urls: Sequence[str],
    ) -> list[dict] | str:
        """Claude accepts ``content`` as either a string (text-only) or a
        list of content blocks. Images come through two shapes
        depending on how ``ChatService`` encoded them:

        * Regular HTTP(S) URL → ``source = {type: "url", url: ...}``
        * ``data:image/...;base64,...`` inline → ``source = {type:
          "base64", media_type: "image/...", data: "..."}``

        The chat loop's ``_to_vision_url`` prefers inlining local
        ``/uploads/...`` files as ``data:`` URLs (LM Studio friendly),
        so we have to split those back into the Anthropic-native
        base64 shape — sending the raw ``data:`` string in
        ``source.url`` gets rejected with 400 invalid_request.
        """
        if not image_urls or not self.supports_vision:
            return prompt
        blocks: list[dict] = [{"type": "text", "text": prompt}]
        for url in image_urls:
            if not url:
                continue
            source = _image_source_for_anthropic(url)
            if source is None:
                # Malformed data: URL — skip silently; better to lose
                # one image than fail the whole turn.
                _LOGGER.warning(
                    "anthropic: dropping image URL we can't shape "
                    "(head=%r)", url[:60],
                )
                continue
            blocks.append({"type": "image", "source": source})
        return blocks

    def _build_payload(
        self,
        prompt: str,
        *,
        stream: bool = False,
        image_urls: Sequence[str] = (),
        model: str | None = None,
        max_tokens_override: int | None = None,
    ) -> dict:
        payload: dict = {
            "model": self._resolve_model(model),
            "max_tokens": (
                max_tokens_override
                if max_tokens_override is not None
                else self._max_tokens
            ),
            "system": _SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": self._build_user_content(prompt, image_urls),
                },
            ],
        }
        # ``max_tokens_override`` is the probe hook's internal cap (1
        # token). Anthropic requires budget_tokens < max_tokens, which a
        # 1-token cap can never satisfy, so the thinking block is
        # omitted under an override — the probe stays cheap and keeps
        # its historical shape.
        if self._thinking_budget_tokens is not None and max_tokens_override is None:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": self._thinking_budget_tokens,
            }
        if stream:
            payload["stream"] = True
        return payload

    async def probe_chat(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15.0,
    ) -> list[ProbeCheck]:
        """Adapter-owned live self-test for the admin "Test" button.

        Optional hook the probe engine feature-detects. Completes a
        1-token ``/v1/messages`` chat built by THIS adapter's
        ``_build_payload`` and posted to THIS adapter's URL (including
        the pasted-``/v1`` tolerance), so probe and runtime can never
        diverge on the request shape again.
        """

        async def chat_check() -> tuple[bool, str]:
            payload = self._build_payload(PROBE_CHAT_PROMPT, max_tokens_override=1)
            async with probe_http_client(timeout_seconds, transport) as client:
                response = await client.post(
                    self._api_url("messages"),
                    json=payload,
                    headers=self._headers(),
                )
            model = str(payload.get("model") or self._model)
            if response.status_code >= 400:
                return False, f"model {model!r}: {probe_http_error_detail(response)}"
            return True, f"model {model!r} completed a 1-token chat"

        return [await run_probe_check("chat_completion", chat_check)]

    # ---- generate -----------------------------------------------------

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        payload = self._build_payload(
            prompt, image_urls=image_urls, model=model,
        )
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.post(
                self._api_url("messages"),
                json=payload,
                headers=self._headers(),
            )
            _raise_with_body(response)
            data = response.json()
        return _extract_text(data)

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        payload = self._build_payload(
            prompt, stream=True, image_urls=image_urls, model=model,
        )
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            async with client.stream(
                "POST",
                self._api_url("messages"),
                json=payload,
                headers=self._headers(),
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    text = body.decode("utf-8", errors="replace")
                    log_http_error_response(
                        _LOGGER,
                        response,
                        operation="Anthropic LLM stream",
                        body_text=text,
                    )
                    raise httpx.HTTPStatusError(
                        f"{response.status_code} from anthropic: {text[:500]}",
                        request=response.request, response=response,
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if not data_str or data_str == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    # Token deltas arrive as ``content_block_delta``
                    # events with ``delta.type == "text_delta"``. Other
                    # events (``message_start``, ``content_block_start``,
                    # ``ping``) we just skip.
                    if chunk.get("type") != "content_block_delta":
                        continue
                    delta = chunk.get("delta") or {}
                    if delta.get("type") != "text_delta":
                        continue
                    text = delta.get("text", "")
                    if text:
                        yield text

    async def list_models(self) -> list[str]:
        """Fetch model IDs via ``GET /v1/models``.

        Same TTL cache + default-fallback behaviour as the OpenAI-compat
        adapter so the UI never hits an empty dropdown."""
        now = time.monotonic()
        if (
            self._models_cache is not None
            and now - self._models_cache_at < _MODEL_LIST_TTL_SECONDS
        ):
            return list(self._models_cache)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    self._api_url("models"),
                    headers=self._headers(),
                )
            if response.status_code >= 400:
                _LOGGER.warning(
                    "Anthropic list_models returned %s; using default",
                    response.status_code,
                )
                return [self._model]
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            _LOGGER.warning(
                "Anthropic list_models failed: %s; using default", exc,
            )
            return [self._model]

        ids: list[str] = []
        for item in data.get("data", []) or []:
            if not isinstance(item, dict):
                continue
            value = item.get("id")
            if isinstance(value, str) and value.strip():
                ids.append(value.strip())
        if not ids:
            ids = [self._model]
        if self._model and self._model not in ids:
            ids.insert(0, self._model)
        self._models_cache = ids
        self._models_cache_at = now
        return list(ids)


def _extract_text(payload: dict) -> str:
    """Pull concatenated text out of a non-streaming ``/v1/messages``
    response. Claude may return multiple content blocks (e.g. mixed
    text + tool-use); we only care about ``text`` blocks for the chat
    reply path — tool-use is handled at a higher layer via our own
    JSON-in-markdown convention, not Claude's native format."""
    parts: list[str] = []
    for block in payload.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


_DATA_URL_PREFIX = "data:"


def _image_source_for_anthropic(url: str) -> dict | None:
    """Produce an Anthropic ``image.source`` block for ``url``.

    Split the ``data:image/<mime>;base64,<payload>`` form into the
    native base64 shape. Plain HTTP(S) URLs pass through as ``type:
    url``. Returns ``None`` when the string looks like ``data:`` but
    is malformed so the caller can skip instead of sending bad
    payload.
    """
    if not url.startswith(_DATA_URL_PREFIX):
        return {"type": "url", "url": url}
    # Shape: ``data:<media_type>[;params];base64,<data>``
    header, _, payload = url[len(_DATA_URL_PREFIX):].partition(",")
    if not payload:
        return None
    # ``header`` looks like ``image/png`` or ``image/jpeg;base64`` — we
    # only accept the base64 variant (no plain-text images in this
    # pipeline).
    if ";base64" not in header:
        return None
    media_type = header.split(";", 1)[0].strip() or "image/png"
    return {
        "type": "base64",
        "media_type": media_type,
        "data": payload,
    }


def _raise_with_body(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    body = response.text
    log_http_error_response(
        _LOGGER,
        response,
        operation="Anthropic LLM",
        body_text=body,
    )
    raise httpx.HTTPStatusError(
        f"{response.status_code} from {response.request.url}: {body[:500]}",
        request=response.request, response=response,
    )
