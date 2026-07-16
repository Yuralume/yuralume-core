"""Capacity probe for local OpenAI-compatible LLMs and Core chat.

Two modes are intentionally separated:

* ``raw-vllm`` measures the OpenAI-compatible endpoint directly.
* ``core-chat`` sends real Core chat turns across existing characters.

The Core mode is non-destructive in schema terms, but it does persist chat
messages and turn records. Use test characters or a test database when you
want a clean production history.

Typical usage::

    uv run python scripts/llm_capacity_probe.py raw-vllm \
        --endpoint http://127.0.0.1:8001/v1 \
        --disable-reasoning --concurrency 1,2,4,8 --requests-per-step 16

    uv run python scripts/llm_capacity_probe.py core-chat \
        --core-url http://127.0.0.1:8002 \
        --email admin@example.com --password ... \
        --characters 8 --concurrency 1,2,4,8 --requests-per-step 16
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx


_DEFAULT_VLLM_ENDPOINT = "http://127.0.0.1:8001/v1"
_DEFAULT_CORE_URL = "http://127.0.0.1:8002"
_DEFAULT_CONCURRENCY = "1,2,4,8"
_REASONING_CONTROL_CHAT_TEMPLATE_KWARGS = "chat-template-kwargs"


@dataclass(frozen=True, slots=True)
class ProbeSample:
    ok: bool
    latency_seconds: float
    response_chars: int = 0
    error: str = ""


@dataclass(frozen=True, slots=True)
class StepSummary:
    label: str
    concurrency: int
    total: int
    ok: int
    failed: int
    elapsed_seconds: float
    p50_seconds: float
    p95_seconds: float
    p99_seconds: float
    requests_per_second: float
    chars_per_second: float
    first_errors: tuple[str, ...]

    @property
    def error_rate(self) -> float:
        if self.total <= 0:
            return 0.0
        return self.failed / self.total


async def run_step(
    *,
    label: str,
    concurrency: int,
    total_requests: int,
    request_once: Callable[[int], Awaitable[ProbeSample]],
) -> StepSummary:
    """Run one bounded-concurrency load step."""
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    if total_requests < 1:
        raise ValueError("total_requests must be >= 1")

    next_index = 0
    index_lock = asyncio.Lock()
    samples: list[ProbeSample] = []
    samples_lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal next_index
        while True:
            async with index_lock:
                if next_index >= total_requests:
                    return
                index = next_index
                next_index += 1
            sample = await request_once(index)
            async with samples_lock:
                samples.append(sample)

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = max(time.perf_counter() - started, 0.000001)

    latencies = [sample.latency_seconds for sample in samples]
    ok = sum(1 for sample in samples if sample.ok)
    failed = len(samples) - ok
    response_chars = sum(sample.response_chars for sample in samples if sample.ok)
    errors = tuple(
        dict.fromkeys(sample.error for sample in samples if sample.error)
    )[:5]
    return StepSummary(
        label=label,
        concurrency=concurrency,
        total=len(samples),
        ok=ok,
        failed=failed,
        elapsed_seconds=elapsed,
        p50_seconds=percentile(latencies, 50),
        p95_seconds=percentile(latencies, 95),
        p99_seconds=percentile(latencies, 99),
        requests_per_second=len(samples) / elapsed,
        chars_per_second=response_chars / elapsed,
        first_errors=errors,
    )


def percentile(values: Sequence[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (percentile_value / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def parse_concurrency_levels(raw: str) -> tuple[int, ...]:
    levels: list[int] = []
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        parsed = int(value)
        if parsed < 1:
            raise ValueError("concurrency levels must be positive")
        if parsed not in levels:
            levels.append(parsed)
    if not levels:
        raise ValueError("at least one concurrency level is required")
    return tuple(levels)


def normalize_api_base(core_url: str) -> str:
    base = core_url.rstrip("/")
    if base.endswith("/api/v1"):
        return base
    return f"{base}/api/v1"


def bearer_headers(token: str | None) -> dict[str, str]:
    token = (token or "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def reasoning_disable_payload(control: str) -> dict[str, Any]:
    """Return provider-specific request fields that disable reasoning.

    vLLM forwards ``chat_template_kwargs`` into the tokenizer chat template.
    Thinking-capable templates commonly honor ``enable_thinking=False`` there
    without requiring a server restart.
    """
    if control == _REASONING_CONTROL_CHAT_TEMPLATE_KWARGS:
        return {"chat_template_kwargs": {"enable_thinking": False}}
    raise ValueError(f"unknown reasoning control: {control}")


class RawVLLMProbe:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: str,
        model: str,
        api_key: str,
        message: str,
        max_tokens: int | None,
        disable_reasoning: bool = False,
        reasoning_control: str = _REASONING_CONTROL_CHAT_TEMPLATE_KWARGS,
    ) -> None:
        self._client = client
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._message = message
        self._max_tokens = max_tokens
        self._disable_reasoning = disable_reasoning
        self._reasoning_control = reasoning_control

    def _build_payload(self, index: int) -> dict[str, Any]:
        prompt = self._message.format(i=index)
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": "You are a concise test assistant."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        if self._disable_reasoning:
            payload.update(reasoning_disable_payload(self._reasoning_control))
        return payload

    async def request_once(self, index: int) -> ProbeSample:
        payload = self._build_payload(index)
        headers = bearer_headers(self._api_key)
        started = time.perf_counter()
        try:
            response = await self._client.post(
                f"{self._endpoint}/chat/completions",
                json=payload,
                headers=headers,
            )
            latency = time.perf_counter() - started
            if response.status_code >= 400:
                return ProbeSample(
                    ok=False,
                    latency_seconds=latency,
                    error=f"HTTP {response.status_code}: {response.text[:240]}",
                )
            data = response.json()
            content = str(data["choices"][0]["message"].get("content") or "")
            return ProbeSample(
                ok=True,
                latency_seconds=latency,
                response_chars=len(content),
            )
        except Exception as exc:  # noqa: BLE001 - probe must report all failures
            return ProbeSample(
                ok=False,
                latency_seconds=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
            )


class CoreChatProbe:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_base: str,
        token: str,
        character_ids: Sequence[str],
        message_template: str,
        operator_persona_enabled: bool,
    ) -> None:
        self._client = client
        self._api_base = api_base.rstrip("/")
        self._headers = bearer_headers(token)
        self._character_ids = tuple(character_ids)
        self._message_template = message_template
        self._operator_persona_enabled = operator_persona_enabled
        if not self._character_ids:
            raise ValueError("at least one character id is required")

    async def request_once(self, index: int) -> ProbeSample:
        character_id = self._character_ids[index % len(self._character_ids)]
        message = self._message_template.format(i=index, character_id=character_id)
        payload = {
            "character_id": character_id,
            "message": message,
            "operator_persona_enabled": self._operator_persona_enabled,
        }
        started = time.perf_counter()
        try:
            response = await self._client.post(
                f"{self._api_base}/chat/messages",
                json=payload,
                headers=self._headers,
            )
            latency = time.perf_counter() - started
            if response.status_code >= 400:
                return ProbeSample(
                    ok=False,
                    latency_seconds=latency,
                    error=f"HTTP {response.status_code}: {response.text[:240]}",
                )
            data = response.json()
            assistant = data.get("assistant_message") or {}
            content = str(assistant.get("content") or "")
            return ProbeSample(
                ok=True,
                latency_seconds=latency,
                response_chars=len(content),
            )
        except Exception as exc:  # noqa: BLE001 - probe must report all failures
            return ProbeSample(
                ok=False,
                latency_seconds=time.perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
            )


async def discover_first_model(
    *,
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
) -> str:
    response = await client.get(
        f"{endpoint.rstrip('/')}/models",
        headers=bearer_headers(api_key),
    )
    response.raise_for_status()
    data = response.json()
    rows = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("/models response is not a list-like OpenAI payload")
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]:
            return row["id"]
    raise ValueError("/models returned no usable model id")


async def login_for_token(
    *,
    client: httpx.AsyncClient,
    api_base: str,
    email: str,
    password: str,
) -> str:
    response = await client.post(
        f"{api_base.rstrip('/')}/auth/login",
        json={"email": email, "password": password},
    )
    response.raise_for_status()
    token = response.json().get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("login response did not include token")
    return token


async def list_character_ids(
    *,
    client: httpx.AsyncClient,
    api_base: str,
    token: str,
    limit: int,
) -> tuple[str, ...]:
    if limit < 1:
        raise ValueError("--characters must be >= 1")
    response = await client.get(
        f"{api_base.rstrip('/')}/characters",
        headers=bearer_headers(token),
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("/characters response is not a list")
    ids = tuple(
        str(row.get("id"))
        for row in rows[:limit]
        if isinstance(row, dict) and row.get("id")
    )
    if not ids:
        raise ValueError("no characters available; create test characters first")
    return ids


def print_summary(summary: StepSummary) -> None:
    print(
        "  "
        f"c={summary.concurrency:<3} "
        f"ok={summary.ok:<4} fail={summary.failed:<4} "
        f"err={summary.error_rate * 100:5.1f}% "
        f"rps={summary.requests_per_second:6.2f} "
        f"p50={summary.p50_seconds:6.2f}s "
        f"p95={summary.p95_seconds:6.2f}s "
        f"p99={summary.p99_seconds:6.2f}s "
        f"chars/s={summary.chars_per_second:8.1f}"
    )
    for error in summary.first_errors:
        print(f"      error: {error}")


async def run_raw_vllm(args: argparse.Namespace) -> int:
    levels = parse_concurrency_levels(args.concurrency)
    timeout = httpx.Timeout(
        connect=args.connect_timeout,
        read=args.read_timeout,
        write=args.write_timeout,
        pool=args.pool_timeout,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        model = args.model or await discover_first_model(
            client=client,
            endpoint=args.endpoint,
            api_key=args.api_key,
        )
        reasoning_label = (
            f"off/{args.reasoning_control}" if args.disable_reasoning else "default"
        )
        print(
            "raw-vllm "
            f"endpoint={args.endpoint.rstrip('/')} model={model} "
            f"reasoning={reasoning_label}"
        )
        probe = RawVLLMProbe(
            client=client,
            endpoint=args.endpoint,
            model=model,
            api_key=args.api_key,
            message=args.message,
            max_tokens=args.max_tokens,
            disable_reasoning=args.disable_reasoning,
            reasoning_control=args.reasoning_control,
        )
        return await run_levels(
            levels=levels,
            requests_per_step=args.requests_per_step,
            request_once=probe.request_once,
            fail_error_rate=args.fail_error_rate,
            fail_p95_seconds=args.fail_p95_seconds,
        )


async def run_core_chat(args: argparse.Namespace) -> int:
    levels = parse_concurrency_levels(args.concurrency)
    api_base = normalize_api_base(args.core_url)
    timeout = httpx.Timeout(
        connect=args.connect_timeout,
        read=args.read_timeout,
        write=args.write_timeout,
        pool=args.pool_timeout,
    )
    token = args.token or os.getenv("YURALUME_AUTH_TOKEN", "")
    async with httpx.AsyncClient(timeout=timeout) as client:
        if not token and args.email and args.password:
            token = await login_for_token(
                client=client,
                api_base=api_base,
                email=args.email,
                password=args.password,
            )
        character_ids = tuple(args.character_id or ())
        if args.characters:
            character_ids = await list_character_ids(
                client=client,
                api_base=api_base,
                token=token,
                limit=args.characters,
            )
        print(
            "core-chat "
            f"api={api_base} characters={len(character_ids)} "
            f"operator_persona={not args.disable_operator_persona}"
        )
        probe = CoreChatProbe(
            client=client,
            api_base=api_base,
            token=token,
            character_ids=character_ids,
            message_template=args.message,
            operator_persona_enabled=not args.disable_operator_persona,
        )
        return await run_levels(
            levels=levels,
            requests_per_step=args.requests_per_step,
            request_once=probe.request_once,
            fail_error_rate=args.fail_error_rate,
            fail_p95_seconds=args.fail_p95_seconds,
        )


async def run_levels(
    *,
    levels: Sequence[int],
    requests_per_step: int,
    request_once: Callable[[int], Awaitable[ProbeSample]],
    fail_error_rate: float | None,
    fail_p95_seconds: float | None,
) -> int:
    exit_code = 0
    for concurrency in levels:
        summary = await run_step(
            label=f"c={concurrency}",
            concurrency=concurrency,
            total_requests=requests_per_step,
            request_once=request_once,
        )
        print_summary(summary)
        if fail_error_rate is not None and summary.error_rate > fail_error_rate:
            exit_code = 2
        if fail_p95_seconds is not None and summary.p95_seconds > fail_p95_seconds:
            exit_code = 2
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe local LLM or Core chat capacity with concurrency ramps.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    raw = subparsers.add_parser("raw-vllm", help="Probe OpenAI-compatible endpoint")
    add_common_options(raw)
    raw.add_argument("--endpoint", default=_DEFAULT_VLLM_ENDPOINT)
    raw.add_argument("--model", default="")
    raw.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY", ""))
    raw.add_argument("--max-tokens", type=int, default=256)
    raw.add_argument(
        "--disable-reasoning",
        action="store_true",
        help=(
            "Ask the endpoint to disable model reasoning/thinking for the "
            "probe request."
        ),
    )
    raw.add_argument(
        "--reasoning-control",
        choices=(_REASONING_CONTROL_CHAT_TEMPLATE_KWARGS,),
        default=_REASONING_CONTROL_CHAT_TEMPLATE_KWARGS,
        help=(
            "Request-body style used by --disable-reasoning. The default "
            "sends chat_template_kwargs.enable_thinking=false, which vLLM "
            "passes into thinking-capable chat templates."
        ),
    )
    raw.add_argument(
        "--message",
        default="容量測試 ping {i}，請用一句中文回答。",
    )
    raw.set_defaults(func=run_raw_vllm)

    core = subparsers.add_parser("core-chat", help="Probe Core chat endpoint")
    add_common_options(core)
    core.add_argument("--core-url", default=_DEFAULT_CORE_URL)
    core.add_argument("--token", default="")
    core.add_argument("--email", default="")
    core.add_argument("--password", default="")
    core.add_argument(
        "--character-id",
        action="append",
        default=[],
        help="Character id to include. Repeat for multiple characters.",
    )
    core.add_argument(
        "--characters",
        type=int,
        default=0,
        help="Fetch the first N existing characters from Core.",
    )
    core.add_argument(
        "--message",
        default="容量測試第 {i} 則，請自然簡短回覆。",
    )
    core.add_argument(
        "--disable-operator-persona",
        action="store_true",
        help="Disable post-turn operator persona extraction for this probe.",
    )
    core.set_defaults(func=run_core_chat)
    return parser


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--concurrency", default=_DEFAULT_CONCURRENCY)
    parser.add_argument("--requests-per-step", type=int, default=16)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--read-timeout", type=float, default=300.0)
    parser.add_argument("--write-timeout", type=float, default=30.0)
    parser.add_argument("--pool-timeout", type=float, default=30.0)
    parser.add_argument(
        "--fail-error-rate",
        type=float,
        default=None,
        help="Exit 2 when a step's error rate exceeds this ratio, e.g. 0.05.",
    )
    parser.add_argument(
        "--fail-p95-seconds",
        type=float,
        default=None,
        help="Exit 2 when a step's p95 latency exceeds this value.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.requests_per_step < 1:
        parser.error("--requests-per-step must be >= 1")
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI should print cleanly
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, httpx.HTTPStatusError):
            print(exc.response.text[:500], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
