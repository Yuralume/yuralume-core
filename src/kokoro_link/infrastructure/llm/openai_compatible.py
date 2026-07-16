import copy
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence

import httpx

from kokoro_link.contracts.llm import (
    ChatModelPort,
    ImageInputRejectedError,
    ReasoningOverrides,
)
from kokoro_link.infrastructure.http_error_logging import log_http_error_response
from kokoro_link.infrastructure.llm.think_tag_filter import (
    strip_think_tags_stream,
    strip_think_tags_text,
)

_LOGGER = logging.getLogger(__name__)

_MODEL_LIST_TTL_SECONDS = 30.0
"""How long to cache the ``/v1/models`` response. Short enough that
loading a new model into LM Studio shows up promptly on the next UI
refresh; long enough that rapid provider-dropdown clicks don't hammer
the endpoint."""

_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
"""Long read budget because LM Studio cold-loads a model on the first
request after a switch — large GGUFs routinely take 60-120s before any
token comes back. Connect stays snappy so an actually-down server fails
fast instead of hanging the whole 5 minutes."""

_IMAGE_REJECTION_STATUSES = frozenset({400, 404, 413, 415, 422})
"""4xx statuses an upstream plausibly returns when it can't accept the
image parts we sent — bad-shape (400), no vision endpoint (404), payload
too large (413), unsupported media type (415), unprocessable (422).

Deliberately excludes 401/403 (auth) and 429 (rate limit): those are
not about the image content, so a drop-images retry would be wrong. 5xx
is likewise excluded — a server error isn't a content rejection. We do
NOT keyword-match the error body (repo forbids keyword special-casing);
the signal is purely "we sent image parts + one of these statuses". A
false positive costs one bounded retry that fails the same way."""


class OpenAICompatibleChatModel(ChatModelPort):
    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        api_key: str | None,
        model: str,
        supports_vision: bool = False,
        max_tokens: int | None = None,
        disable_reasoning: bool = False,
        reasoning_effort: str | None = None,
        extra_request_params: dict | None = None,
        strip_think_tags: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.supports_vision = supports_vision
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        # Reasoning controls — all opt-in. When unset the payload is byte
        # -for-byte identical to before, so existing cloud providers are
        # untouched.
        self._disable_reasoning = disable_reasoning
        self._reasoning_effort = reasoning_effort
        self._extra_request_params = extra_request_params
        self._strip_think_tags = strip_think_tags
        self._models_cache: list[str] | None = None
        self._models_cache_at: float = 0.0
        self._non_chat_model_overrides: set[str] = set()

    def with_reasoning_overrides(
        self, overrides: ReasoningOverrides,
    ) -> "OpenAICompatibleChatModel":
        """Return a copy bound to a routing-level reasoning posture.

        The routing layer (feature/group override) replaces the whole
        reasoning pair this adapter understands; connection-level
        ``strip_think_tags`` / ``extra_request_params`` stay untouched
        (endpoint properties, not per-task posture). ``copy.copy``
        shares the model-list cache and the learned non-chat-model set
        with the base adapter so per-call copies don't re-pay those
        probes.
        """
        clone = copy.copy(self)
        clone._disable_reasoning = overrides.disable_reasoning
        clone._reasoning_effort = overrides.reasoning_effort
        return clone

    def with_supports_vision(self, value: bool) -> "OpenAICompatibleChatModel":
        """Return a copy whose vision capability a routing entry overrides.

        One aggregator connection (OpenRouter) fronts both vision and
        text-only models, so the connection-level ``supports_vision``
        flag can't be right for every route; a routing entry
        (feature/group/active_model) pins the correct value and the
        resolver binds it here onto a per-call copy. ``copy.copy`` shares
        the model-list cache and the learned non-chat-model set with the
        base singleton so per-call copies don't re-pay those probes (same
        sharing rationale as ``with_reasoning_overrides``).
        """
        clone = copy.copy(self)
        clone.supports_vision = value
        return clone

    def _resolve_model(self, override: str | None) -> str:
        """Pick which model ID to send on this call.

        Empty-string / whitespace override is treated as "no override" so
        the UI can send an empty string to mean "use default" without
        needing a sentinel."""
        if override is not None and override.strip():
            model = override.strip()
            if model not in self._non_chat_model_overrides:
                return model
        return self._model

    def _build_payload(
        self,
        prompt: str,
        *,
        stream: bool = False,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> dict:
        # When images are offered AND we're told the model supports
        # vision, emit the OpenAI multimodal ``content`` array shape —
        # ``[{"type": "text", "text": ...}, {"type": "image_url", ...}]``.
        # Otherwise fall back to the plain-string shape which every
        # OpenAI-compatible server accepts regardless of capability.
        if image_urls and self.supports_vision:
            user_content: list[dict] = [{"type": "text", "text": prompt}]
            for url in image_urls:
                if not url:
                    continue
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": url},
                })
            user_message: dict = {"role": "user", "content": user_content}
        else:
            user_message = {"role": "user", "content": prompt}
        payload: dict = {
            "model": self._resolve_model(model),
            "messages": [
                {"role": "system", "content": "You are a roleplay character backend."},
                user_message,
            ],
        }
        if self._max_tokens is not None:
            # Lift the server-side default (LM Studio ships ~512) so long
            # tool-call JSON + caption fits without truncation. Unset when
            # the operator leaves the env knob empty, i.e. trust the
            # server to pick something reasonable.
            payload["max_tokens"] = self._max_tokens
        # Reasoning controls — three independent opt-ins. Each key is only
        # emitted when the operator explicitly set it; nothing is sent by
        # default. extra_request_params merges last so an advanced user can
        # deliberately override the shape produced above.
        if self._disable_reasoning:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        if self._extra_request_params:
            payload.update(self._extra_request_params)
        if stream:
            payload["stream"] = True
        return payload

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def validate_reasoning_effort(
        self,
        effort: str,
        *,
        model: str | None = None,
    ) -> None:
        """Probe whether the selected upstream model accepts ``effort``.

        Model documentation and deployed capability rollouts can briefly
        disagree, and OpenAI-compatible providers use different value sets.
        A minimal real request is therefore the only reliable save-time
        validation. It uses the same payload builder and endpoint as runtime
        generation, so success proves this deployed provider/model pair works.
        """
        cleaned = effort.strip()
        if not cleaned:
            raise ValueError("reasoning_effort must be non-empty")
        probe = self.with_reasoning_overrides(
            ReasoningOverrides(reasoning_effort=cleaned),
        )
        payload = probe._build_payload("Reply with exactly OK.", model=model)
        resolved_model = str(payload.get("model") or self._model)
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=self._build_headers(),
                )
        except httpx.HTTPError as exc:
            raise ValueError(
                "reasoning_effort validation request failed for "
                f"{self.provider_id}/{resolved_model}: {type(exc).__name__}",
            ) from exc
        if not response.is_success:
            detail = response.text.strip()[:1000] or f"HTTP {response.status_code}"
            raise ValueError(
                f"reasoning_effort {cleaned!r} rejected by "
                f"{self.provider_id}/{resolved_model}: {detail}",
            )

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
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._build_headers(),
            )
            if _is_non_chat_model_response(response, payload.get("model"), self._model):
                self._remember_non_chat_override(str(payload["model"]))
                payload = self._build_payload(
                    prompt, image_urls=image_urls, model=None,
                )
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=self._build_headers(),
                )
            try:
                _raise_with_body(response)
            except httpx.HTTPStatusError as exc:
                _raise_image_rejection_if_applicable(
                    status_code=response.status_code,
                    body=response.text,
                    payload=payload,
                    cause=exc,
                )
                raise
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        if self._strip_think_tags and isinstance(content, str):
            return strip_think_tags_text(content)
        return content

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        raw = self._raw_generate_stream(
            prompt, image_urls=image_urls, model=model,
        )
        if not self._strip_think_tags:
            # Default path: zero extra buffering, byte-identical to before.
            async for chunk in raw:
                yield chunk
            return
        # Opt-in: a stateful filter drops <think>...</think> even when the
        # markers straddle chunk boundaries.
        async for chunk in strip_think_tags_stream(raw):
            yield chunk

    async def _raw_generate_stream(
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
            retry_payload: dict | None = None
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._build_headers(),
            ) as response:
                if response.status_code >= 400:
                    # Read the body for diagnostics before raising —
                    # otherwise the stream is closed and the operator
                    # just sees "400 Bad Request" with no clue which
                    # field the server rejected. ``aread`` is safe
                    # inside the stream context.
                    body = await response.aread()
                    text = body.decode("utf-8", errors="replace")
                    if _is_non_chat_model_error(
                        status_code=response.status_code,
                        body=text,
                        requested_model=payload.get("model"),
                        default_model=self._model,
                    ):
                        self._remember_non_chat_override(str(payload["model"]))
                        retry_payload = self._build_payload(
                            prompt,
                            stream=True,
                            image_urls=image_urls,
                            model=None,
                        )
                    else:
                        # 4xx surfaces before the first token — classify
                        # image rejection here so the caller can degrade.
                        try:
                            _raise_stream_error(response, text)
                        except httpx.HTTPStatusError as exc:
                            _raise_image_rejection_if_applicable(
                                status_code=response.status_code,
                                body=text,
                                payload=payload,
                                cause=exc,
                            )
                            raise
                else:
                    async for chunk in _iter_openai_stream(response):
                        yield chunk
            if retry_payload is None:
                return
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=retry_payload,
                headers=self._build_headers(),
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    text = body.decode("utf-8", errors="replace")
                    # The non-chat-model retry still carries the images,
                    # so classify image rejection on this attempt too.
                    try:
                        _raise_stream_error(response, text)
                    except httpx.HTTPStatusError as exc:
                        _raise_image_rejection_if_applicable(
                            status_code=response.status_code,
                            body=text,
                            payload=retry_payload,
                            cause=exc,
                        )
                        raise
                async for chunk in _iter_openai_stream(response):
                    yield chunk

    async def list_models(self) -> list[str]:
        """Fetch available model IDs via ``GET /v1/models``.

        Returns the cached list when it's still fresh. On any error we
        fall back to ``[self._model]`` — the UI can still show something
        clickable and chats keep working on the default.
        """
        now = time.monotonic()
        if (
            self._models_cache is not None
            and now - self._models_cache_at < _MODEL_LIST_TTL_SECONDS
        ):
            return list(self._models_cache)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self._base_url}/models",
                    headers=self._build_headers(),
                )
            if response.status_code >= 400:
                _LOGGER.warning(
                    "LLM %s list_models returned %s; falling back to default",
                    self.provider_id, response.status_code,
                )
                return [self._model]
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            _LOGGER.warning(
                "LLM %s list_models failed: %s; falling back to default",
                self.provider_id, exc,
            )
            return [self._model]

        ids = _parse_model_ids(data) or [self._model]
        if self._model:
            ids = [model_id for model_id in ids if model_id != self._model]
            ids.insert(0, self._model)
        self._models_cache = ids
        self._models_cache_at = now
        return list(ids)

    def _remember_non_chat_override(self, model: str) -> None:
        if not model or model == self._model:
            return
        self._non_chat_model_overrides.add(model)
        _LOGGER.warning(
            "LLM %s rejected model %r as non-chat; falling back to %r",
            self.provider_id,
            model,
            self._model,
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
            delta = chunk["choices"][0].get("delta", {})
            content = delta.get("content", "")
            if content:
                yield content
        except (json.JSONDecodeError, KeyError, IndexError):
            continue


def _raise_stream_error(response: httpx.Response, text: str) -> None:
    log_http_error_response(
        _LOGGER,
        response,
        operation="OpenAI-compatible LLM stream",
        body_text=text,
    )
    raise httpx.HTTPStatusError(
        f"{response.status_code} from {response.request.url}: {text[:500]}",
        request=response.request,
        response=response,
    )


def _is_non_chat_model_response(
    response: httpx.Response,
    requested_model: object,
    default_model: str,
) -> bool:
    if response.status_code < 400:
        return False
    return _is_non_chat_model_error(
        status_code=response.status_code,
        body=response.text,
        requested_model=requested_model,
        default_model=default_model,
    )


def _is_non_chat_model_error(
    *,
    status_code: int,
    body: str,
    requested_model: object,
    default_model: str,
) -> bool:
    if requested_model == default_model or status_code not in {400, 404}:
        return False
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    message = str(error.get("message") or "").lower()
    return error.get("param") == "model" and "not a chat model" in message

def _parse_model_ids(data: object) -> list[str]:
    """Extract model IDs from an OpenAI-compatible ``/v1/models`` payload.

    Accepts both ``{"data": [{"id": ...}, ...]}`` (spec) and bare list
    shapes some local servers emit."""
    if isinstance(data, dict):
        data = data.get("data", [])
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for item in data:
        if isinstance(item, dict):
            value = item.get("id")
            if isinstance(value, str) and value.strip():
                ids.append(value.strip())
    return ids


def _payload_has_image_parts(payload: object) -> bool:
    """True when the request payload's user message carried the OpenAI
    multimodal array shape with at least one ``image_url`` part.

    This is the structural signal the image-rejection classifier keys on
    — we only degrade-retry a 4xx when we actually sent images, never by
    keyword-matching the error body. ``_build_payload`` emits the array
    shape exactly when it took the ``image_urls and self.supports_vision``
    branch, so inspecting the payload we're about to send (or just sent)
    is the reliable "images were attached" test."""
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


def _raise_image_rejection_if_applicable(
    *,
    status_code: int,
    body: str,
    payload: object,
    cause: httpx.HTTPStatusError,
) -> None:
    """Reclassify a bare 4xx as ``ImageInputRejectedError`` when it
    plausibly rejects the image parts we sent.

    Fires only when the status is one of ``_IMAGE_REJECTION_STATUSES``
    AND the request payload actually carried image parts. Otherwise
    returns without raising, so the caller re-raises the original
    ``HTTPStatusError`` unchanged. The typed error chains ``__cause__``
    to the original for diagnostics."""
    if status_code not in _IMAGE_REJECTION_STATUSES:
        return
    if not _payload_has_image_parts(payload):
        return
    raise ImageInputRejectedError(status_code=status_code, body=body) from cause


def _raise_with_body(response: httpx.Response) -> None:
    """Like ``raise_for_status`` but logs + includes the response body.

    Default ``raise_for_status`` just shows "400 Bad Request", which
    is useless for diagnosing OpenAI-compatible servers that return a
    JSON ``{"error": {"message": "..."}}`` explaining exactly which
    field they rejected.
    """
    if response.status_code < 400:
        return
    body = response.text
    log_http_error_response(
        _LOGGER,
        response,
        operation="OpenAI-compatible LLM",
        body_text=body,
    )
    raise httpx.HTTPStatusError(
        f"{response.status_code} from {response.request.url}: {body[:500]}",
        request=response.request, response=response,
    )
