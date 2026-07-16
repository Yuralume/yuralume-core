"""Shared shapes for adapter-owned live-probe hooks.

The provider-settings live probe (admin "Test" button) is an
orchestrator: it builds the real runtime adapter from the draft
config/secret and asks the adapter to run its own cheap self-test.
Adapters that support this expose an optional async hook — feature
-detected via ``getattr``, mirroring the ``validate_reasoning_effort``
precedent — that owns the request shape end-to-end:

* chat models      → ``probe_chat()``
* embedders        → ``probe_embedding()``
* TTS adapters     → ``probe_tts()``
* image providers  → ``probe_image_generation()``

Because the hook reuses the adapter's own payload builders and
signal-driven retry/memo machinery, a quirk fixed in the adapter is
automatically exercised (and surfaced) by the probe — request shapes
are owned in exactly one place.

Contract:

* A hook returns ``list[ProbeCheck]``; ``action`` must be one of the
  shared probe action enum values (``listed_models`` /
  ``chat_completion`` / ``embedded`` / ``listed_voices`` /
  ``synthesized_speech`` / ``generated_image`` …) so the engine can map
  checks 1:1 onto its pinned ``ProbeReport`` API contract.
* Hooks accept an optional ``transport`` (httpx test/probe seam) and a
  ``timeout_seconds`` budget; both are threaded by the probe engine.
* Hooks should not raise for expected failures — use
  :func:`run_probe_check`, which maps network errors to the same
  wording the engine historically produced. The engine still fail-softs
  anything that escapes.
* Details need not be pre-sanitized: the engine passes every detail
  through its secret scrubbing choke point.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

#: Text bodies shared by the chat / TTS probe checks so every adapter's
#: self-test stays equally cheap (1 token / one short word).
PROBE_CHAT_PROMPT = "ping"
PROBE_TTS_TEXT = "Hi"


@dataclass(frozen=True, slots=True)
class ProbeCheck:
    """One self-test outcome an adapter hook reports to the probe engine."""

    ok: bool
    action: str
    detail: str
    latency_ms: int = 0


async def run_probe_check(
    action: str,
    check: Callable[[], Awaitable[tuple[bool, str]]],
) -> ProbeCheck:
    """Run one timed check, mapping network errors like the probe engine.

    ``check`` returns ``(ok, detail)``; timeouts / transport errors /
    unexpected exceptions become failed checks with the engine's
    historical wording, so hook-based probes read identically to the
    old inline ones.
    """
    start = time.perf_counter()
    try:
        ok, detail = await check()
    except httpx.TimeoutException:
        ok, detail = False, "request timed out"
    except httpx.HTTPError as exc:
        ok, detail = False, f"connection failed: {exc}"
    except Exception as exc:  # a probe hook must never raise
        ok, detail = False, str(exc) or exc.__class__.__name__
    latency_ms = int((time.perf_counter() - start) * 1000)
    return ProbeCheck(ok=ok, action=action, detail=detail, latency_ms=latency_ms)


def probe_http_client(
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.AsyncClient:
    """Build the httpx client a probe hook should use.

    Separate from the adapter's runtime client so the probe budget
    (seconds) and the engine's transport seam apply without touching
    normal generate/synthesize semantics.
    """
    kwargs: dict[str, Any] = {"timeout": timeout_seconds}
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.AsyncClient(**kwargs)


def probe_http_error_detail(response: httpx.Response) -> str:
    """Status + short body snippet, matching the engine's wording."""
    detail = f"HTTP {response.status_code} {response.reason_phrase}".strip()
    snippet = (response.text or "").strip().replace("\n", " ")[:160]
    return f"{detail}: {snippet}" if snippet else detail
