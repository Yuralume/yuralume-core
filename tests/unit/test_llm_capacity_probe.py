import asyncio

import pytest
import httpx

from scripts.llm_capacity_probe import (
    ProbeSample,
    RawVLLMProbe,
    bearer_headers,
    normalize_api_base,
    parse_concurrency_levels,
    percentile,
    reasoning_disable_payload,
    run_step,
)


def test_parse_concurrency_levels_deduplicates_and_trims() -> None:
    assert parse_concurrency_levels(" 1, 2,2, 4 ") == (1, 2, 4)


def test_parse_concurrency_levels_rejects_empty() -> None:
    with pytest.raises(ValueError):
        parse_concurrency_levels(" , ")


def test_percentile_interpolates() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([10.0], 95) == 10.0
    assert percentile([], 95) == 0.0


def test_normalize_api_base_accepts_app_or_api_base() -> None:
    assert normalize_api_base("http://127.0.0.1:8002") == (
        "http://127.0.0.1:8002/api/v1"
    )
    assert normalize_api_base("http://127.0.0.1:8002/api/v1") == (
        "http://127.0.0.1:8002/api/v1"
    )


def test_bearer_headers_omits_empty_token() -> None:
    assert bearer_headers("") == {}
    assert bearer_headers("abc") == {"Authorization": "Bearer abc"}


def test_reasoning_disable_payload_uses_chat_template_kwargs() -> None:
    assert reasoning_disable_payload("chat-template-kwargs") == {
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_raw_vllm_payload_omits_reasoning_control_by_default() -> None:
    probe = RawVLLMProbe(
        client=httpx.AsyncClient(),
        endpoint="http://example.test/v1",
        model="model-a",
        api_key="",
        message="hello {i}",
        max_tokens=32,
    )
    payload = probe._build_payload(7)

    assert payload["model"] == "model-a"
    assert payload["messages"][1]["content"] == "hello 7"
    assert payload["max_tokens"] == 32
    assert "chat_template_kwargs" not in payload


def test_raw_vllm_payload_can_disable_reasoning() -> None:
    probe = RawVLLMProbe(
        client=httpx.AsyncClient(),
        endpoint="http://example.test/v1",
        model="model-a",
        api_key="",
        message="hello {i}",
        max_tokens=None,
        disable_reasoning=True,
    )
    payload = probe._build_payload(0)

    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "max_tokens" not in payload


@pytest.mark.asyncio
async def test_run_step_bounds_concurrency_and_summarizes_errors() -> None:
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def request_once(index: int) -> ProbeSample:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.01)
        async with lock:
            active -= 1
        if index == 2:
            return ProbeSample(ok=False, latency_seconds=0.02, error="boom")
        return ProbeSample(ok=True, latency_seconds=0.01, response_chars=5)

    summary = await run_step(
        label="unit",
        concurrency=2,
        total_requests=5,
        request_once=request_once,
    )

    assert peak <= 2
    assert summary.total == 5
    assert summary.ok == 4
    assert summary.failed == 1
    assert summary.first_errors == ("boom",)
    assert summary.chars_per_second > 0
