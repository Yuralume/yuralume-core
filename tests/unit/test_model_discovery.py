"""Model discovery + draft base_url fallback (fetch-models regression).

2026-07-16 report: NanoGPT "fetch models" failed with "base_url is
required to list models" because the draft config carried no base_url.
The route now falls back to the same per-provider defaults the runtime
adapters use, so presets work with the field left blank while custom
providers keep the explicit error.
"""

from __future__ import annotations

import asyncio

from kokoro_link.infrastructure.provider_settings.model_discovery import (
    discover_models,
)
from kokoro_link.infrastructure.provider_settings.runtime_sync import (
    default_base_url_for,
)


def test_default_base_url_known_presets() -> None:
    assert default_base_url_for("nanogpt") == "https://nano-gpt.com/api/v1"
    assert default_base_url_for("openrouter") == "https://openrouter.ai/api/v1"
    assert default_base_url_for("openai") == "https://api.openai.com/v1"


def test_default_base_url_custom_and_unknown_stay_empty() -> None:
    # Custom/local-unknown providers have no sensible default — discovery
    # must keep surfacing the explicit "base_url is required" error.
    assert default_base_url_for("custom_openai_compatible") == ""
    assert default_base_url_for("yuralume_cloud") == ""
    assert default_base_url_for("no-such-provider") == ""


def test_resolve_draft_base_url_fallback_composition() -> None:
    """The exact rule the list-models route applies to draft configs."""
    from kokoro_link.api.routes.admin_providers import resolve_draft_base_url

    # Preset with empty/missing/whitespace base_url → provider default.
    assert (
        resolve_draft_base_url({}, "nanogpt")
        == "https://nano-gpt.com/api/v1"
    )
    assert (
        resolve_draft_base_url({"base_url": "   "}, "nanogpt")
        == "https://nano-gpt.com/api/v1"
    )
    # An explicit value always wins over the preset default.
    assert (
        resolve_draft_base_url({"base_url": "http://127.0.0.1:8080/v1"}, "nanogpt")
        == "http://127.0.0.1:8080/v1"
    )
    # Custom providers have no default → discovery keeps its explicit error.
    assert resolve_draft_base_url({}, "custom_openai_compatible") == ""
    assert resolve_draft_base_url({}, "yuralume_cloud") == ""


def test_discover_models_requires_base_url() -> None:
    result = asyncio.run(
        discover_models(
            provider_id="custom_openai_compatible",
            adapter_kind="openai_compatible",
            capability="llm",
            base_url="",
            api_key="sk-test",
        )
    )
    assert result.models == []
    assert result.error == "base_url is required to list models"


def test_discover_models_unsupported_provider_reports_error() -> None:
    result = asyncio.run(
        discover_models(
            provider_id="google_veo",
            adapter_kind="google_veo",
            capability="video",
            base_url="https://example.invalid/v1",
            api_key="",
        )
    )
    assert result.models == []
    assert result.error is not None
    assert "not supported" in result.error
