import copy
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Sequence

import httpx

from kokoro_link.contracts.llm import (
    ChatModelPort,
    ImageInputRejectedError,
    ReasoningOverrides,
)
from kokoro_link.contracts.provider_probe import (
    PROBE_CHAT_PROMPT,
    ProbeCheck,
    probe_http_client,
    probe_http_error_detail,
    run_probe_check,
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

_SYSTEM_PROMPT = "You are a roleplay character backend."

_MAX_PRESCRIBED_RETRIES = 8
"""Defensive cap on server-prescribed adaptations per call. Every
adaptation is monotone — it either consumes a one-shot memo (non-chat
model, ``max_tokens`` rename, system-role merge) or removes a payload
key it just verified was present — so the real bound is the payload's
key count; the constant only guards against a pathological upstream."""

_STRUCTURAL_PARAMS = frozenset({"model", "messages", "stream"})
"""Keys the drop-and-retry adaptation must never remove: a chat request
without them is nonsense, so an error naming one of these is a
different problem that dropping cannot fix."""

_UNRECOGNIZED_PARAM_MARKERS = (
    "unrecognized request argument",
    "unknown parameter",
    "unknown argument",
    "unsupported parameter",
    "unsupported argument",
    "extra inputs are not permitted",
)
"""Error-body phrases that mark the "your request carries an argument I
don't accept" rejection class: OpenAI lineage says 'Unrecognized request
argument supplied: X' / 'Unknown parameter: X' / 'Unsupported parameter:
X'; pydantic-validated servers (Mistral, vLLM nested) say 'Extra inputs
are not permitted' next to the offending field name. The marker alone
never triggers a retry — the identifier anchored to the marker (or the
pydantic ``loc`` field) must ALSO be a key we actually sent (see
``_named_unrecognized_params``), which keeps this signal-driven rather
than a guess."""


class _LearnedQuirks:
    """Server-taught request adjustments for ONE resolved model id.

    Every teaching signal is model-scoped, not connection-scoped: the
    stream-verification block hits the gpt-5 family while gpt-4o streams
    fine on the same key; a system-role rejection is one model's chat
    template; dropped params differ per model. One aggregator connection
    (OpenRouter) fronts many models, so the adapter keeps a
    ``dict[str, _LearnedQuirks]`` keyed by the payload's resolved model
    id (the same way ``_non_chat_model_overrides`` keys models) — a
    lesson one model teaches never degrades its siblings.

    The DICT object is what ``with_reasoning_overrides`` /
    ``with_supports_vision`` ``copy.copy`` clones share: a lesson any
    clone learns immediately benefits the base adapter and every later
    clone — the same rationale as the shared non-chat-model set.
    Attribute rebinding on a plain bool would be clone-local and re-pay
    the failed round every call."""

    __slots__ = ("dropped_params", "merge_system_into_user", "non_stream_fallback")

    def __init__(self) -> None:
        self.dropped_params: set[str] = set()
        self.merge_system_into_user = False
        self.non_stream_fallback = False


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
        # Newer OpenAI models (gpt-5+/o-series lineage) reject `max_tokens`
        # and prescribe `max_completion_tokens` in the error body. Learned
        # per instance on the first such rejection (signal-driven — no
        # model-name allowlist) so subsequent calls skip the failed round.
        self._max_tokens_param = "max_tokens"
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
        # Signal-driven lessons the upstream teaches us at runtime
        # (dropped params, stream-verification block, system-role
        # merge), keyed by resolved model id so one model's lesson never
        # degrades siblings on the same (aggregator) connection. One
        # shared dict object so per-call clones learn together.
        self._quirks_by_model: dict[str, _LearnedQuirks] = {}

    def with_reasoning_overrides(
        self, overrides: ReasoningOverrides,
    ) -> "OpenAICompatibleChatModel":
        """Return a copy bound to a routing-level reasoning posture.

        The routing layer (feature/group override) replaces the whole
        reasoning pair this adapter understands; connection-level
        ``strip_think_tags`` / ``extra_request_params`` stay untouched
        (endpoint properties, not per-task posture). ``copy.copy``
        shares the model-list cache, the learned non-chat-model set and
        the model-scoped ``_LearnedQuirks`` map with the base adapter so
        per-call copies don't re-pay those probes.
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
        the model-list cache, the learned non-chat-model set and the
        model-scoped ``_LearnedQuirks`` map with the base singleton so
        per-call copies don't re-pay those probes (same sharing rationale
        as ``with_reasoning_overrides``).
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

    def _quirks_for(self, model: str) -> _LearnedQuirks:
        """Learned-quirk memos for one RESOLVED model id.

        Reads and writes go through the shared ``_quirks_by_model`` dict
        (``setdefault`` keeps clone-sharing intact: mutating the entry on
        any per-call clone is visible to the base adapter and every other
        clone)."""
        return self._quirks_by_model.setdefault(model, _LearnedQuirks())

    def _build_payload(
        self,
        prompt: str,
        *,
        stream: bool = False,
        image_urls: Sequence[str] = (),
        model: str | None = None,
        max_tokens_override: int | None = None,
    ) -> dict:
        resolved_model = self._resolve_model(model)
        quirks = self._quirks_for(resolved_model)
        if quirks.merge_system_into_user:
            # This model's chat template rejected the system role
            # (see _is_system_role_rejection) — carry the instruction
            # inside the user turn instead of a system message.
            prompt = f"{_SYSTEM_PROMPT}\n\n{prompt}"
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
        if quirks.merge_system_into_user:
            messages: list[dict] = [user_message]
        else:
            messages = [
                {"role": "system", "content": _SYSTEM_PROMPT},
                user_message,
            ]
        payload: dict = {
            "model": resolved_model,
            "messages": messages,
        }
        if max_tokens_override is not None:
            # Internal knob for the probe hook: cap the completion (1
            # token) without touching normal generate semantics. Uses the
            # same learned param name so the probe exercises the exact
            # rename the runtime would send.
            payload[self._max_tokens_param] = max_tokens_override
        elif self._max_tokens is not None:
            # Lift the server-side default (LM Studio ships ~512) so long
            # tool-call JSON + caption fits without truncation. Unset when
            # the operator leaves the env knob empty, i.e. trust the
            # server to pick something reasonable. The param name adapts to
            # what the endpoint accepts (see _is_max_tokens_param_error).
            payload[self._max_tokens_param] = self._max_tokens
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
        # Params this model already refused as unrecognized (see
        # _named_unrecognized_params) are withheld on every later call.
        for name in quirks.dropped_params:
            payload.pop(name, None)
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

    async def probe_chat(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15.0,
    ) -> list[ProbeCheck]:
        """Adapter-owned live self-test for the admin "Test" button.

        Optional hook the probe engine feature-detects (same precedent
        as ``validate_reasoning_effort``): list models, then complete a
        1-token chat built by THIS adapter's ``_build_payload`` and
        healed by the same signal-driven adaptation loop as
        ``generate()`` — so every quirk the runtime copes with
        (``max_completion_tokens`` rename, system-role merge,
        unrecognized-param drop) is inherited by the probe instead of
        being re-implemented there.
        """

        async def list_models_check() -> tuple[bool, str]:
            async with probe_http_client(timeout_seconds, transport) as client:
                response = await client.get(
                    f"{self._base_url}/models",
                    headers=self._build_headers(),
                )
            if response.status_code >= 400:
                return False, probe_http_error_detail(response)
            try:
                data = response.json()
            except ValueError:
                return False, "models endpoint returned non-JSON response"
            return True, f"{len(_parse_model_ids(data))} models"

        async def chat_check() -> tuple[bool, str]:
            payload = self._build_payload(PROBE_CHAT_PROMPT, max_tokens_override=1)
            async with probe_http_client(timeout_seconds, transport) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=self._build_headers(),
                )
                for _ in range(_MAX_PRESCRIBED_RETRIES):
                    adapted = self._adapted_payload_for_rejection(
                        status_code=response.status_code,
                        body=response.text,
                        payload=payload,
                        prompt=PROBE_CHAT_PROMPT,
                        stream=False,
                        image_urls=(),
                        model=None,
                        max_tokens_override=1,
                    )
                    if adapted is None:
                        break
                    payload = adapted
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers=self._build_headers(),
                    )
            model = str(payload.get("model") or self._model)
            if response.status_code >= 400:
                return False, f"model {model!r}: {probe_http_error_detail(response)}"
            # Surface learned quirks in the success detail so the
            # operator hears about them from the Test button rather than
            # from the first failing (retried) chat.
            quirk = (
                " (requires max_completion_tokens)"
                if self._max_tokens_param == "max_completion_tokens"
                else ""
            )
            return True, f"model {model!r} completed a 1-token chat{quirk}"

        return [
            await run_probe_check("listed_models", list_models_check),
            await run_probe_check("chat_completion", chat_check),
        ]

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
            # Signal-driven adaptation loop: each round reacts to what
            # the error body prescribes (non-chat model → default,
            # max_tokens rename, system-role merge, unrecognized-param
            # drop), remembers the lesson, and retries once per lesson.
            for _ in range(_MAX_PRESCRIBED_RETRIES):
                adapted = self._adapted_payload_for_rejection(
                    status_code=response.status_code,
                    body=response.text,
                    payload=payload,
                    prompt=prompt,
                    stream=False,
                    image_urls=image_urls,
                    model=model,
                )
                if adapted is None:
                    break
                payload = adapted
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
        if self._quirks_for(self._resolve_model(model)).non_stream_fallback:
            # Learned earlier that this model refuses to stream for us
            # (org-verification restriction) — go straight to the
            # non-stream request and serve it as a single chunk.
            # ``content`` may be null upstream (refusal/tool-call-only):
            # never leak a non-str into the ``AsyncIterator[str]``.
            fallback = await self.generate(
                prompt, image_urls=image_urls, model=model,
            )
            if isinstance(fallback, str) and fallback:
                yield fallback
            return
        payload = self._build_payload(
            prompt, stream=True, image_urls=image_urls, model=model,
        )
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            # Same signal-driven adaptation loop as generate(); the
            # stream-verification signal additionally breaks out to the
            # non-stream fallback below. Exhausting the defensive cap
            # falls through to the same fallback, whose generate() then
            # either succeeds or raises with the diagnostic body.
            for _ in range(_MAX_PRESCRIBED_RETRIES):
                async with client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=self._build_headers(),
                ) as response:
                    if response.status_code < 400:
                        async for chunk in _iter_openai_stream(response):
                            yield chunk
                        return
                    # Read the body for diagnostics before raising —
                    # otherwise the stream is closed and the operator
                    # just sees "400 Bad Request" with no clue which
                    # field the server rejected. ``aread`` is safe
                    # inside the stream context.
                    body = await response.aread()
                    text = body.decode("utf-8", errors="replace")
                    if _is_stream_verification_error(
                        status_code=response.status_code,
                        body=text,
                        payload=payload,
                    ):
                        self._remember_non_stream_fallback(
                            str(payload.get("model") or self._model),
                        )
                        break
                    adapted = self._adapted_payload_for_rejection(
                        status_code=response.status_code,
                        body=text,
                        payload=payload,
                        prompt=prompt,
                        stream=True,
                        image_urls=image_urls,
                        model=model,
                    )
                    if adapted is None:
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
                    payload = adapted
        # Stream-open was refused with the verification restriction:
        # degrade to one non-stream completion, yielded as one chunk.
        # Same non-str guard as the memoized fast path above — an
        # upstream ``content: null`` ends the stream with zero chunks.
        fallback = await self.generate(prompt, image_urls=image_urls, model=model)
        if isinstance(fallback, str) and fallback:
            yield fallback

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

    def _adapted_payload_for_rejection(
        self,
        *,
        status_code: int,
        body: str,
        payload: dict,
        prompt: str,
        stream: bool,
        image_urls: Sequence[str],
        model: str | None,
        max_tokens_override: int | None = None,
    ) -> dict | None:
        """Turn a server-prescribed rejection into a rebuilt payload.

        Each branch reacts to an explicit signal in the error body
        (never a model-name allowlist), records what it learned on the
        shared instance state — keyed by the FAILING payload's model id,
        since every signal is a fact about that model — so later calls
        skip the failed round, and returns the payload for one retry.
        ``None`` means the response prescribes nothing we can adapt to —
        the caller surfaces the error as-is. Every adaptation is monotone
        (a memo flips once or a payload key disappears), so chained
        retries terminate; the identical-payload guard at the end catches
        the one degenerate case (an operator's ``extra_request_params``
        re-inserting the offending key on every rebuild) where a memo
        flip cannot actually change the request.
        """
        if status_code < 400:
            return None
        failing_model = str(payload.get("model") or self._model)
        adapted: dict | None = None
        if _is_non_chat_model_error(
            status_code=status_code,
            body=body,
            requested_model=payload.get("model"),
            default_model=self._model,
        ):
            self._remember_non_chat_override(str(payload["model"]))
            adapted = self._build_payload(
                prompt,
                stream=stream,
                image_urls=image_urls,
                model=None,
                max_tokens_override=max_tokens_override,
            )
        elif _is_max_tokens_param_error(
            status_code=status_code, body=body, payload=payload,
        ):
            # The server literally prescribed the fix — rename the
            # parameter, remember it, retry once.
            self._max_tokens_param = "max_completion_tokens"
            adapted = self._build_payload(
                prompt,
                stream=stream,
                image_urls=image_urls,
                model=model,
                max_tokens_override=max_tokens_override,
            )
        elif _is_system_role_rejection(
            status_code=status_code, body=body, payload=payload,
        ):
            self._remember_system_role_merge(failing_model)
            adapted = self._build_payload(
                prompt,
                stream=stream,
                image_urls=image_urls,
                model=model,
                max_tokens_override=max_tokens_override,
            )
        else:
            rejected = _named_unrecognized_params(
                status_code=status_code, body=body, payload=payload,
            )
            if rejected:
                for name in rejected:
                    self._remember_dropped_param(failing_model, name)
                adapted = self._build_payload(
                    prompt,
                    stream=stream,
                    image_urls=image_urls,
                    model=model,
                    max_tokens_override=max_tokens_override,
                )
        if adapted is None:
            return None
        if adapted == payload:
            # The rebuild reproduced the exact payload that just failed
            # (e.g. the offending ``max_tokens`` re-enters via
            # ``extra_request_params`` on every rebuild). Retrying would
            # hammer the identical rejection until the defensive cap —
            # treat it as nothing-prescribed and surface the original
            # error so the operator can fix the escape-hatch config.
            _LOGGER.warning(
                "LLM %s: server-prescribed adaptation for model %r "
                "reproduced the payload that just failed (an "
                "extra_request_params entry likely re-inserts the "
                "rejected key); surfacing the original error instead "
                "of retrying",
                self.provider_id,
                failing_model,
            )
            return None
        return adapted

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

    def _remember_dropped_param(self, model: str, name: str) -> None:
        quirks = self._quirks_for(model)
        if name in quirks.dropped_params:
            return
        quirks.dropped_params.add(name)
        # Param NAME only — never the value (it may embed operator data).
        _LOGGER.warning(
            "LLM %s model %r rejected request param %r as unrecognized; "
            "dropping it from all further requests to this model",
            self.provider_id,
            model,
            name,
        )

    def _remember_system_role_merge(self, model: str) -> None:
        quirks = self._quirks_for(model)
        if quirks.merge_system_into_user:
            return
        quirks.merge_system_into_user = True
        _LOGGER.warning(
            "LLM %s: model %r's chat template rejected the system role; "
            "merging the system prompt into the user turn for this "
            "model",
            self.provider_id,
            model,
        )

    def _remember_non_stream_fallback(self, model: str) -> None:
        quirks = self._quirks_for(model)
        if quirks.non_stream_fallback:
            return
        quirks.non_stream_fallback = True
        _LOGGER.warning(
            "LLM %s: upstream requires organization verification to "
            "stream model %r; degrading to non-streaming completions "
            "(served as a single chunk) for this model",
            self.provider_id,
            model,
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


def _is_max_tokens_param_error(
    *,
    status_code: int,
    body: str,
    payload: dict,
) -> bool:
    """The endpoint rejected ``max_tokens`` and prescribed the rename.

    OpenAI's gpt-5+/o-series models return HTTP 400 with a message telling
    the caller to use ``max_completion_tokens`` instead. Detection is
    signal-driven (the server names the replacement parameter), never a
    model-name allowlist — aggregators and future models inherit it.
    """
    return (
        status_code == 400
        and "max_tokens" in payload
        and "max_completion_tokens" in body
    )


def _is_non_chat_model_error(
    *,
    status_code: int,
    body: str,
    requested_model: object,
    default_model: str,
) -> bool:
    """The endpoint says the requested model can't serve chat/completions.

    Two OpenAI error shapes, both carrying ``param == "model"``: the
    classic "not a chat model" (embedding/image models) and the
    Responses-only restriction "only supported in v1/responses"
    (gpt-5-pro / o*-pro / deep-research lineage). Conservative guard:
    only per-call OVERRIDES fall back to the default model — a
    misconfigured default must fail loudly, not silently loop onto
    itself."""
    if requested_model == default_model or status_code not in {400, 404}:
        return False
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    if error.get("param") != "model":
        return False
    message = str(error.get("message") or "").lower()
    return (
        "not a chat model" in message
        or "only supported in v1/responses" in message
    )


def _is_stream_verification_error(
    *,
    status_code: int,
    body: str,
    payload: dict,
) -> bool:
    """The org must complete OpenAI verification before it may stream.

    Unverified OpenAI orgs get HTTP 400 "Your organization must be
    verified to stream this model" on gpt-5-family / o3+ models while
    the identical non-stream request succeeds. Signal-driven: fires
    only when we actually asked for ``stream: true`` AND the body
    carries the verified-to-stream restriction — a plain 400 or a
    non-stream call can never match."""
    if status_code != 400 or not payload.get("stream"):
        return False
    low = body.lower()
    return "must be verified" in low and "stream" in low


def _is_system_role_rejection(
    *,
    status_code: int,
    body: str,
    payload: dict,
) -> bool:
    """The server's chat template rejected our system-role message.

    Gemma-2-class official templates raise "System role not supported";
    Mistral/Ministral-class templates raise "Only user, assistant and
    tool roles are supported, got system". llama.cpp (--jinja),
    llama-cpp-python and LM Studio surface the template's own
    ``raise_exception`` text as a 400/500 body. Signal-driven: fires
    only when we actually sent a system message AND the body carries
    the template's rejection text, so the coping path (merge the system
    prompt into the user turn) is always applicable."""
    if status_code < 400 or not _payload_has_system_message(payload):
        return False
    low = body.lower()
    if "system role not supported" in low:
        return True
    return "roles are supported" in low and "system" in low


_MARKER_NAMED_PARAM_RE = re.compile(
    r"(?:unrecognized request argument(?:\s+supplied)?"
    r"|unknown\s+(?:parameter|argument)"
    r"|unsupported\s+(?:parameter|argument))"
    r"\s*:?\s*[\"'`]?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
"""Extracts the identifier IMMEDIATELY following an OpenAI-lineage
unrecognized-argument marker ('Unrecognized request argument supplied:
X' / 'Unknown parameter: X' / "Unsupported parameter: 'X'"). Anchoring
to the marker (instead of scanning the whole body for any key we sent)
matters: some servers echo the full request in the error message, and a
greedy scan would prescribe dropping every echoed param."""


def _pydantic_extra_forbidden_locs(body: str) -> list[str]:
    """Field names pydantic-validated servers (Mistral, vLLM) reject.

    Their 422 carries ``detail[]`` entries with ``type: extra_forbidden``
    (msg 'Extra inputs are not permitted') and the offending field as the
    last ``loc`` element — the structured counterpart of the marker
    anchoring above, so a request echo elsewhere in the body can never
    name a field."""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return []
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    if not isinstance(detail, list):
        return []
    names: list[str] = []
    for entry in detail:
        if not isinstance(entry, dict):
            continue
        is_extra_forbidden = entry.get("type") == "extra_forbidden" or (
            "extra inputs are not permitted"
            in str(entry.get("msg") or "").lower()
        )
        if not is_extra_forbidden:
            continue
        loc = entry.get("loc")
        if isinstance(loc, list) and loc and isinstance(loc[-1], str):
            names.append(loc[-1])
    return names


def _named_unrecognized_params(
    *,
    status_code: int,
    body: str,
    payload: dict,
) -> tuple[str, ...]:
    """Top-level payload keys the error body names as unrecognized.

    Fires only when (a) the status is the argument-validation class —
    400 for OpenAI-lineage "unrecognized/unknown/unsupported argument"
    messages, 422 for pydantic-validated servers (Mistral) that say
    "Extra inputs are not permitted" — and (b) the body names a key WE
    sent *at the anchored position*: the identifier immediately after
    the marker phrase, or a pydantic ``extra_forbidden`` ``loc`` entry.
    Anchoring is what keeps a server that echoes our request in its
    error body from getting every echoed param dropped. Requiring the
    named key in our payload keeps this signal-driven: the server tells
    us which argument offended and we verify that claim before dropping
    anything. Structural keys (model/messages/stream) are never dropped
    — an error naming those is a different problem a drop cannot fix."""
    if status_code not in {400, 422}:
        return ()
    low = body.lower()
    if not any(marker in low for marker in _UNRECOGNIZED_PARAM_MARKERS):
        return ()
    candidates = [
        match.group(1) for match in _MARKER_NAMED_PARAM_RE.finditer(body)
    ]
    candidates.extend(_pydantic_extra_forbidden_locs(body))
    named: list[str] = []
    for name in candidates:
        if name in named or name in _STRUCTURAL_PARAMS or name not in payload:
            continue
        named.append(name)
    return tuple(named)


def _payload_has_system_message(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    first = messages[0]
    return isinstance(first, dict) and first.get("role") == "system"


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
