"""Direct coverage for ``content_tolerance_for_llm_provider`` +
``normalize_content_tolerance``.

These helpers are the routing signal that decides whether a turn's LLM
calls run against frontier or community handling. The frontier
provider-id set is load-bearing: an id missing from it silently
downgrades the whole turn to community (which then forces the NSFW
community target on money-surface routes). ``openrouter`` in particular
regressed once because it was absent here.
"""

from __future__ import annotations

import pytest

from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_COMMUNITY,
    CONTENT_TOLERANCE_FRONTIER,
    content_tolerance_for_llm_provider,
    normalize_content_tolerance,
)

_FRONTIER_IDS = ("anthropic", "gemini", "google_gemini", "openai", "openrouter", "xai")


@pytest.mark.parametrize("provider_id", _FRONTIER_IDS)
def test_frontier_provider_ids_map_to_frontier(provider_id: str) -> None:
    assert (
        content_tolerance_for_llm_provider(provider_id)
        == CONTENT_TOLERANCE_FRONTIER
    )


def test_openrouter_is_frontier() -> None:
    # Regression pin for Fix 3: an openrouter main model must NOT drag the
    # whole turn to community tolerance in normal mode.
    assert (
        content_tolerance_for_llm_provider("openrouter")
        == CONTENT_TOLERANCE_FRONTIER
    )


@pytest.mark.parametrize(
    "provider_id",
    [
        "custom_openai_compatible",
        "local_openai_compatible",
        "lmstudio",
        "ollama",
        "deepseek",
        "unknown_provider",
        "",
    ],
)
def test_non_frontier_provider_ids_map_to_community(provider_id: str) -> None:
    assert (
        content_tolerance_for_llm_provider(provider_id)
        == CONTENT_TOLERANCE_COMMUNITY
    )


def test_none_provider_id_maps_to_community() -> None:
    assert (
        content_tolerance_for_llm_provider(None)
        == CONTENT_TOLERANCE_COMMUNITY
    )


@pytest.mark.parametrize("provider_id", _FRONTIER_IDS)
def test_nsfw_content_mode_forces_community_even_for_frontier(
    provider_id: str,
) -> None:
    # The NSFW-mode overlay is community by construction; a frontier
    # provider id can't upgrade it back.
    assert (
        content_tolerance_for_llm_provider(
            provider_id,
            current_content_mode=MessageContentMode.NSFW,
        )
        == CONTENT_TOLERANCE_COMMUNITY
    )


def test_nsfw_content_mode_as_string_forces_community() -> None:
    assert (
        content_tolerance_for_llm_provider(
            "anthropic",
            current_content_mode="nsfw",
        )
        == CONTENT_TOLERANCE_COMMUNITY
    )


def test_normal_content_mode_keeps_frontier_for_frontier_provider() -> None:
    assert (
        content_tolerance_for_llm_provider(
            "openrouter",
            current_content_mode=MessageContentMode.NORMAL,
        )
        == CONTENT_TOLERANCE_FRONTIER
    )


@pytest.mark.parametrize("routing_source", ["nsfw_mode", "nsfw_content"])
def test_nsfw_routing_source_forces_community(routing_source: str) -> None:
    assert (
        content_tolerance_for_llm_provider(
            "anthropic",
            routing_source=routing_source,
        )
        == CONTENT_TOLERANCE_COMMUNITY
    )


def test_unrelated_routing_source_does_not_force_community() -> None:
    assert (
        content_tolerance_for_llm_provider(
            "openrouter",
            routing_source="global_group",
        )
        == CONTENT_TOLERANCE_FRONTIER
    )


@pytest.mark.parametrize(
    "provider_id",
    ["  openrouter  ", "OpenRouter", "ANTHROPIC", " Xai "],
)
def test_provider_id_is_normalized_before_lookup(provider_id: str) -> None:
    # provider_id is ``.strip().lower()``'d, so case / whitespace variants
    # of a frontier id still resolve to frontier.
    assert (
        content_tolerance_for_llm_provider(provider_id)
        == CONTENT_TOLERANCE_FRONTIER
    )


def test_whitespace_only_provider_id_maps_to_community() -> None:
    assert (
        content_tolerance_for_llm_provider("   ")
        == CONTENT_TOLERANCE_COMMUNITY
    )


# ---- normalize_content_tolerance -------------------------------------


def test_normalize_keeps_community() -> None:
    assert (
        normalize_content_tolerance(CONTENT_TOLERANCE_COMMUNITY)
        == CONTENT_TOLERANCE_COMMUNITY
    )


def test_normalize_keeps_frontier() -> None:
    assert (
        normalize_content_tolerance(CONTENT_TOLERANCE_FRONTIER)
        == CONTENT_TOLERANCE_FRONTIER
    )


@pytest.mark.parametrize("value", [None, "", "garbage", "Community", "FRONTIER"])
def test_normalize_defaults_unknown_to_frontier(value: str | None) -> None:
    # Anything that isn't the exact ``community`` sentinel is treated as
    # frontier (fail-safe: community is the restrictive branch and must be
    # opted into explicitly).
    assert normalize_content_tolerance(value) == CONTENT_TOLERANCE_FRONTIER
