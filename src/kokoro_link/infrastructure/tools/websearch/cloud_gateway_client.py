"""Cloud Gateway search adapter — ``POST {gateway}/v1/search``.

The hosted counterpart of the direct-provider backends in this package. A
self-host operator points ``web_search`` at their own Tavily / SearXNG /
OpenAI key through Admin → provider settings; a hosted deployment has no
provider keys at all (Core's provider-settings API is 403 in cloud mode),
so its search has to go the same way its chat, images and embeddings do:
through the Cloud Gateway, which owns the credential, the routing, the
allowlist and the billing.

Design notes:

* The Gateway route is provider-neutral by construction — it answers
  ``{answer, results[]}``, the exact shape :class:`SearchResponse` already
  carries — so which backend actually served the query (OpenAI Responses
  today) is an operator choice in the control plane and never a Core
  dependency. That is why this is one adapter rather than one per vendor.
* The preset comes from the control-plane routing profile and has **no
  env/default fallback**. Every other cloud capability may fall back to a
  deployment default preset; search may not, because an identity the
  control plane did not route for search would otherwise reach a paid
  upstream nobody priced. No route ⇒ the tool reports itself unavailable
  and the model answers without it.
* Every failure becomes :class:`SearchError`, which
  :class:`~kokoro_link.infrastructure.tools.websearch.tool.WebSearchTool`
  renders as a ``ToolResult.failure``. A dead search must cost the player
  a sentence, never the turn.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Mapping

import httpx

from kokoro_link.application.services.cloud_identity_context import current_cloud_actor
from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudGatewayIdentityResolverPort,
    CloudIdentityUnavailable,
    CloudResourceContext,
)
from kokoro_link.contracts.cloud_routing_profile import CloudRoutingProfilePort
from kokoro_link.contracts.generation_trigger import (
    TRIGGER_HEADER_NAME,
    generation_trigger_header_value,
)
from kokoro_link.infrastructure.tools.websearch.tool import (
    SearchClientPort,
    SearchError,
    SearchResponse,
    SearchResult,
    truncate_answer,
    truncate_snippet,
)

_LOGGER = logging.getLogger(__name__)

_CAPABILITY = "search"
_FEATURE_KEY = "web_search"
# Live fetch plus a synthesis pass upstream, then the Gateway's own hop: the
# read budget has to clear the Gateway's default response timeout (90s) or Core
# would give up on a search the operator is still paying for.
_DEFAULT_TIMEOUT_SECONDS = 100.0

# Hard ceiling on the Gateway response Core will hold in memory. The Gateway is
# first-party and bounded by construction, so this is not a size we expect to
# meet — it is the difference between a broken Gateway costing one tool call and
# a broken Gateway killing the worker with the chat turn still in it.
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Ceiling on how many raw ``results`` entries are examined. Without it a
# response prefixed with a million unusable rows costs a full scan to produce
# the same handful of citations.
_MAX_SCANNED_RESULTS = 200

# Per-field caps. ``truncate_snippet`` already bounds the snippet; a title or a
# URL is rendered into the tool output too, so an unbounded one lands straight
# in the next LLM context.
_MAX_TITLE_CHARS = 200
_MAX_URL_CHARS = 500


class CloudGatewaySearchClient(SearchClientPort):
    """Route ``web_search`` through the hosted Cloud Gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        deployment_token: str,
        identity_resolver: CloudGatewayIdentityResolverPort | None = None,
        identity: CloudGatewayIdentity | None = None,
        routing_profile_port: CloudRoutingProfilePort | None = None,
        deployment_id: str = "hosted-primary",
        audience: str = "yuralume-gateway",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not base_url.strip():
            raise ValueError("cloud gateway search base_url is required")
        if not deployment_token.strip():
            raise ValueError("cloud gateway search deployment_token is required")
        if not deployment_id.strip():
            raise ValueError("cloud gateway search deployment_id is required")
        if not audience.strip():
            raise ValueError("cloud gateway search audience is required")
        self._base_url = base_url.rstrip("/")
        self._deployment_token = deployment_token
        self._identity_resolver = identity_resolver
        self._identity = identity
        self._routing_profile_port = routing_profile_port
        self._deployment_id = deployment_id.strip()
        self._audience = audience.strip()
        self._timeout = httpx.Timeout(
            connect=10.0,
            read=max(_DEFAULT_TIMEOUT_SECONDS, timeout_seconds),
            write=30.0,
            pool=10.0,
        )
        self.last_request_id = ""

    async def search(self, *, query: str, max_results: int) -> SearchResponse:
        identity = await self._resolve_identity()
        preset = await self._resolve_preset(identity)
        payload = {"model": preset, "query": query, "max_results": max_results}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/v1/search",
                    headers=self._headers(identity),
                    json=payload,
                ) as response:
                    body = await _read_bounded(response)
                    status_code = response.status_code
        except httpx.TimeoutException as exc:
            raise SearchError("搜尋逾時") from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"搜尋連線失敗：{exc}") from exc

        code = _error_code(body)
        if status_code >= 400:
            # Status and the envelope's own error code only. A reverse proxy
            # between Core and the Gateway can render its Authorization header
            # into a 4xx/5xx diagnostic page, and this log line would then
            # persist the deployment token wherever Core logs are shipped.
            _LOGGER.warning(
                "cloud search gateway rejected request account=%s status=%s code=%s",
                identity.account_id,
                status_code,
                code or "-",
            )
            raise SearchError(_error_message(status_code, code))

        data = _decode_json(body)
        # A well-formed empty answer is a real "nothing found"; a body that is
        # not an object, or whose ``results`` is not an array, is the Gateway
        # breaking its contract. Reporting that as "no results" would tell the
        # player their search worked.
        if not isinstance(data, Mapping):
            raise SearchError("搜尋服務回應格式不符")
        raw_results = data.get("results")
        if raw_results is not None and not isinstance(raw_results, list):
            raise SearchError("搜尋服務回應格式不符")
        return SearchResponse(
            answer=truncate_answer(str(data.get("answer") or "")),
            results=_results(raw_results, max_results),
        )

    async def _resolve_identity(self) -> CloudGatewayIdentity:
        if self._identity is not None:
            return self._identity
        if self._identity_resolver is None:
            raise SearchError("搜尋需要雲端帳號身分，但此部署未設定")
        actor = current_cloud_actor()
        if actor is None:
            raise SearchError("搜尋需要雲端帳號身分，目前的執行情境沒有帶入")
        context = (
            CloudResourceContext.for_character(actor.character)
            if actor.character is not None
            else CloudResourceContext.for_account(actor.operator_id or "")
        )
        try:
            return await self._identity_resolver.resolve_context(context)
        except CloudIdentityUnavailable as exc:
            raise SearchError("搜尋需要雲端帳號身分，解析失敗") from exc
        except Exception as exc:  # noqa: BLE001 — the port may raise anything
            # The contract this client owes ``WebSearchTool`` is that every
            # failure is a SearchError. A transport error escaping raw would be
            # logged with a stack and rendered into the chat as its own text.
            raise SearchError("搜尋需要雲端帳號身分，解析失敗") from exc

    async def _resolve_preset(self, identity: CloudGatewayIdentity) -> str:
        """The routed search preset, or a hard failure.

        No default: a search preset names a paid upstream, so "the control
        plane did not route search for this identity" must read as *no search*
        rather than as *use whatever the deployment happens to have*."""
        if self._routing_profile_port is None:
            raise SearchError("搜尋未設定：此部署沒有連上控制面路由")
        try:
            profile = await self._routing_profile_port.get_profile(
                tenant_id=identity.tenant_id,
                account_id=identity.account_id,
                user_id=identity.account_id,
                tier=identity.tenant_tier,
            )
        except Exception as exc:
            raise SearchError("搜尋路由設定暫時無法取得") from exc
        if profile.is_disabled(_CAPABILITY, _FEATURE_KEY):
            raise SearchError("此方案未開放上網搜尋")
        preset = profile.preset_for(_CAPABILITY, _FEATURE_KEY)
        if not preset:
            raise SearchError("此方案未開放上網搜尋")
        return preset

    def _headers(self, identity: CloudGatewayIdentity) -> dict[str, str]:
        request_id = f"search-{uuid.uuid4().hex}"
        self.last_request_id = request_id
        return {
            "Authorization": f"Bearer {self._deployment_token}",
            "X-Yuralume-Deployment": self._deployment_id,
            "X-Yuralume-Audience": self._audience,
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
            "X-Yuralume-Tenant": identity.tenant_id,
            "X-Yuralume-Account": identity.account_id,
            "X-Yuralume-Feature": _FEATURE_KEY,
            "X-Yuralume-Character": identity.character_ref,
            TRIGGER_HEADER_NAME: generation_trigger_header_value(),
        }


def _results(raw: object, max_results: int) -> list[SearchResult]:
    """Parse the Gateway's ``results`` array defensively.

    A malformed entry costs that entry, not the search: the fused ``answer``
    is usually the useful half anyway, and dropping the whole response over
    one bad row would turn a partial upstream hiccup into a dead tool. The scan
    itself is bounded so a long prefix of unusable rows cannot buy an unbounded
    walk, and every rendered field is clamped — all three end up inside the next
    LLM turn's context."""
    if not isinstance(raw, list):
        return []
    results: list[SearchResult] = []
    for item in raw[:_MAX_SCANNED_RESULTS]:
        if len(results) >= max_results:
            break
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url") or "").strip()[:_MAX_URL_CHARS]
        if not url:
            continue
        title = str(item.get("title") or "").strip()[:_MAX_TITLE_CHARS] or url
        results.append(
            SearchResult(
                title=title,
                url=url,
                snippet=truncate_snippet(str(item.get("snippet") or "")),
            ),
        )
    return results


async def _read_bounded(response: httpx.Response) -> bytes:
    """Buffer the response body, refusing to grow past the ceiling."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise SearchError("搜尋服務回應過大")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SearchError("搜尋服務回應不是 JSON") from exc


def _error_code(body: bytes) -> str:
    """The Gateway envelope's ``error.code``, or empty when unreadable."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(payload, Mapping):
        return ""
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return ""
    return str(error.get("code") or "").strip()


def _error_message(status_code: int, code: str) -> str:
    """Turn a Gateway rejection into a sentence the character can say.

    Payment and policy rejections get their own wording because they are not
    faults — they are the deployment's answer — and telling the player
    "search failed" for an exhausted balance would send them debugging
    something that is working as configured. Only the envelope's own ``code``
    reaches the player; the raw body never does."""
    if status_code == 402 or code == "insufficient_credits":
        return "螢火不足，這次沒辦法上網查"
    if status_code == 403:
        return "此方案未開放上網搜尋"
    if status_code == 429:
        return "搜尋暫時排隊中，稍後再試"
    suffix = f"（{code}）" if code else ""
    return f"搜尋服務回應錯誤{suffix}"
