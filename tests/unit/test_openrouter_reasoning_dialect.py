"""AM4: per-preset reasoning dialect for OpenAI-compatible chat.

OpenRouter ignores unknown request params instead of rejecting them, so
the vLLM-convention ``chat_template_kwargs`` disable used to be a silent
no-op there and ``thinking_budget_tokens`` was silently discarded on the
whole openai_compatible path. These tests pin:

- the "openrouter" dialect payload shapes (nested ``reasoning`` object,
  top-level effort), the D4 budget-over-effort exclusivity, and the
  disable-over-effort exclusivity (F2, 2026-08-25: disable must also
  suppress effort — a disabled request never carries a top-level
  ``reasoning_effort`` alongside ``reasoning.enabled: false``),
- the default "chat_template" dialect staying byte-identical to before
  (a configured budget is withheld with ONE warning, never sent),
- ``extra_request_params`` still merging last over dialect output,
- both configuration paths: connection row via ``build_chat_model``
  (dialect read off the catalog preset entry) and routing layer via
  ``with_reasoning_overrides``.
"""

from __future__ import annotations

import logging

from kokoro_link.contracts.llm import ReasoningOverrides
from kokoro_link.contracts.provider_settings import ProviderConnection
from kokoro_link.infrastructure.llm.openai_compatible import (
    OpenAICompatibleChatModel,
    REASONING_DIALECT_CHAT_TEMPLATE,
    REASONING_DIALECT_OPENROUTER,
)
from kokoro_link.infrastructure.provider_settings.adapter_builders import (
    build_chat_model,
)
from kokoro_link.infrastructure.provider_settings.catalog import catalog_by_id


def _openrouter_model(**kwargs) -> OpenAICompatibleChatModel:
    return OpenAICompatibleChatModel(
        provider_id="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key=None,
        model="m",
        reasoning_dialect=REASONING_DIALECT_OPENROUTER,
        **kwargs,
    )


def _row(provider: str, config: dict) -> ProviderConnection:
    return ProviderConnection(
        id="conn-1",
        provider=provider,
        label="test",
        enabled=True,
        capabilities=("llm",),
        config=config,
    )


# ---- openrouter dialect: payload shapes ------------------------------


def test_openrouter_disable_sends_nested_reasoning_enabled_false() -> None:
    model = _openrouter_model(disable_reasoning=True)
    payload = model._build_payload("hi")
    assert payload["reasoning"] == {"enabled": False}
    assert "chat_template_kwargs" not in payload


def test_openrouter_budget_sends_nested_reasoning_max_tokens() -> None:
    model = _openrouter_model(thinking_budget_tokens=2048)
    payload = model._build_payload("hi")
    assert payload["reasoning"] == {"max_tokens": 2048}


def test_openrouter_effort_stays_top_level() -> None:
    model = _openrouter_model(reasoning_effort="high")
    payload = model._build_payload("hi")
    assert payload["reasoning_effort"] == "high"
    assert "reasoning" not in payload


def test_openrouter_budget_wins_over_effort() -> None:
    """D4 (owner 2026-08-25): OpenRouter treats effort and max_tokens as
    mutually exclusive — a configured budget wins, the effort is not
    sent."""
    model = _openrouter_model(
        reasoning_effort="high", thinking_budget_tokens=1024,
    )
    payload = model._build_payload("hi")
    assert payload["reasoning"] == {"max_tokens": 1024}
    assert "reasoning_effort" not in payload


def test_openrouter_disable_beats_budget() -> None:
    """disable + budget resolves to only ``enabled: false`` — a budget
    alongside would contradict the disable; the effort stays withheld
    because a budget is configured (D4)."""
    model = _openrouter_model(
        disable_reasoning=True,
        thinking_budget_tokens=1024,
        reasoning_effort="low",
    )
    payload = model._build_payload("hi")
    assert payload["reasoning"] == {"enabled": False}
    assert "reasoning_effort" not in payload


def test_openrouter_disable_drops_effort_when_no_budget() -> None:
    """disable alone still suppresses effort: sending a strength knob
    alongside ``reasoning.enabled: false`` self-contradicts the disable
    (same principle as the budget suppression; OpenRouter's own
    priority between disable and effort is undocumented, so this
    adapter never emits the pair together — F2)."""
    model = _openrouter_model(disable_reasoning=True, reasoning_effort="low")
    payload = model._build_payload("hi")
    assert payload["reasoning"] == {"enabled": False}
    assert "reasoning_effort" not in payload


def test_openrouter_no_optins_payload_unchanged() -> None:
    model = _openrouter_model()
    payload = model._build_payload("hi")
    assert set(payload) == {"model", "messages"}


def test_extra_request_params_override_dialect_reasoning() -> None:
    """The escape hatch still merges last: an advanced user can replace
    the dialect-produced ``reasoning`` object wholesale."""
    model = _openrouter_model(
        thinking_budget_tokens=2048,
        extra_request_params={"reasoning": {"effort": "high"}},
    )
    payload = model._build_payload("hi")
    assert payload["reasoning"] == {"effort": "high"}


# ---- chat_template dialect: zero change, loud budget drop ------------


def test_chat_template_budget_not_sent_and_warns_once(caplog) -> None:
    """The default dialect has no budget counterpart: the payload stays
    byte-identical to a budget-less build, and the drop is logged ONCE
    per adapter (not per call) so it is never silent."""
    model = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
        thinking_budget_tokens=1024,
    )
    with caplog.at_level(logging.WARNING):
        first = model._build_payload("hi")
        second = model._build_payload("hi")
    assert set(first) == {"model", "messages"}
    assert set(second) == {"model", "messages"}
    warnings = [
        record for record in caplog.records
        if "thinking_budget_tokens" in record.getMessage()
    ]
    assert len(warnings) == 1


def test_chat_template_disable_and_effort_shapes_unchanged() -> None:
    model = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
        disable_reasoning=True,
        reasoning_effort="medium",
    )
    payload = model._build_payload("hi")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["reasoning_effort"] == "medium"
    assert "reasoning" not in payload


# ---- routing layer (with_reasoning_overrides) ------------------------


def test_routing_override_binds_budget() -> None:
    base = _openrouter_model()
    bound = base.with_reasoning_overrides(
        ReasoningOverrides(thinking_budget_tokens=512),
    )
    payload = bound._build_payload("hi")
    assert payload["reasoning"] == {"max_tokens": 512}


def test_routing_override_replaces_whole_trio() -> None:
    """An override carrying only a budget must also displace the
    connection's effort (whole-trio replacement + D4), and must leave
    the base adapter untouched."""
    base = _openrouter_model(reasoning_effort="low")
    bound = base.with_reasoning_overrides(
        ReasoningOverrides(thinking_budget_tokens=512),
    )
    payload = bound._build_payload("hi")
    assert payload["reasoning"] == {"max_tokens": 512}
    assert "reasoning_effort" not in payload
    base_payload = base._build_payload("hi")
    assert base_payload["reasoning_effort"] == "low"
    assert "reasoning" not in base_payload


def test_routing_override_budget_on_chat_template_stays_dropped(caplog) -> None:
    """The dialect is a connection property: binding a budget onto a
    chat_template connection still withholds it (with the warning)."""
    base = OpenAICompatibleChatModel(
        provider_id="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        api_key=None,
        model="m",
    )
    bound = base.with_reasoning_overrides(
        ReasoningOverrides(thinking_budget_tokens=512),
    )
    with caplog.at_level(logging.WARNING):
        payload = bound._build_payload("hi")
    assert set(payload) == {"model", "messages"}
    assert any(
        "thinking_budget_tokens" in record.getMessage()
        for record in caplog.records
    )


# ---- connection layer (catalog + build_chat_model) -------------------


def test_catalog_openrouter_entry_declares_dialect_and_budget_field() -> None:
    catalog = catalog_by_id()
    entry = catalog["openrouter"]
    assert entry.reasoning_dialect == REASONING_DIALECT_OPENROUTER
    assert "thinking_budget_tokens" in [f.key for f in entry.config_fields]
    # Other openai_compatible presets keep the default dialect.
    for preset in ("mistral", "deepseek", "nanogpt", "custom_openai_compatible",
                   "local_openai_compatible"):
        assert catalog[preset].reasoning_dialect == REASONING_DIALECT_CHAT_TEMPLATE


def test_build_chat_model_openrouter_row_speaks_openrouter_dialect() -> None:
    row = _row("openrouter", {"thinking_budget_tokens": "2048"})
    model = build_chat_model(row, {"api_key": "sk-or-test"})
    payload = model._build_payload("hi")
    assert payload["reasoning"] == {"max_tokens": 2048}


def test_build_chat_model_other_preset_keeps_chat_template_dialect() -> None:
    row = _row(
        "custom_openai_compatible",
        {
            "base_url": "http://127.0.0.1:8000/v1",
            "default_model": "local-model",
            "disable_reasoning": True,
        },
    )
    model = build_chat_model(row, {"api_key": ""})
    payload = model._build_payload("hi")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning" not in payload
