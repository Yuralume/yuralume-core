"""Tests for ``ModelPreferenceValidator`` — the startup repair that
clears DB model picks pointing at providers / model ids that no longer
exist in the registry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from kokoro_link.application.services.feature_keys import (
    FEATURE_POST_TURN, FEATURE_GOAL_REVIEW,
)
from kokoro_link.application.services.preference_validator import (
    ModelPreferenceValidator,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)


class _StubModel(ChatModelPort):
    def __init__(
        self, provider_id: str, *, models: list[str] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.supports_vision = False
        self._models = models if models is not None else [provider_id]

    async def generate(
        self, prompt: str, *,
        image_urls: Sequence[str] = (), model: str | None = None,
    ) -> str:
        return ""

    async def generate_stream(
        self, prompt: str, *,
        image_urls: Sequence[str] = (), model: str | None = None,
    ) -> AsyncIterator[str]:
        async def _iter() -> AsyncIterator[str]:
            if False:  # pragma: no cover
                yield ""
        return _iter()

    async def list_models(self) -> list[str]:
        return list(self._models)


def _wire(
    *, default: str = "lmstudio",
    models_by_provider: dict[str, list[str]] | None = None,
) -> tuple[
    ModelPreferenceValidator, InMemoryPreferencesRepository,
]:
    registry = InMemoryChatModelRegistry(default_provider_id=default)
    layout = models_by_provider or {
        "lmstudio": ["llama-3", "qwen-7b"],
        "anthropic": ["claude-sonnet-4-5", "claude-opus-4-7"],
    }
    for pid, models in layout.items():
        registry.register(_StubModel(pid, models=models))
    prefs = InMemoryPreferencesRepository()
    validator = ModelPreferenceValidator(
        registry=registry, preferences=prefs,
        default_provider_id=default,
    )
    return validator, prefs


@pytest.mark.asyncio
async def test_repair_noop_when_active_model_pref_absent() -> None:
    validator, prefs = _wire()
    await validator.repair()
    assert await prefs.get("active_model") is None


@pytest.mark.asyncio
async def test_repair_keeps_valid_active_model_pref_untouched() -> None:
    validator, prefs = _wire()
    await prefs.set("active_model", {
        "provider_id": "anthropic", "model_id": "claude-sonnet-4-5",
    })
    await validator.repair()
    assert await prefs.get("active_model") == {
        "provider_id": "anthropic", "model_id": "claude-sonnet-4-5",
    }


@pytest.mark.asyncio
async def test_repair_resets_active_model_when_provider_gone() -> None:
    validator, prefs = _wire(default="lmstudio")
    await prefs.set("active_model", {
        "provider_id": "openai", "model_id": "gpt-4o",
    })
    await validator.repair()
    assert await prefs.get("active_model") == {
        "provider_id": "lmstudio", "model_id": None,
    }


@pytest.mark.asyncio
async def test_repair_resets_to_registered_provider_when_default_is_gone() -> None:
    validator, prefs = _wire(
        default="lmstudio",
        models_by_provider={"anthropic": ["claude-sonnet-4-5"]},
    )
    await prefs.set("active_model", {
        "provider_id": "openai", "model_id": "gpt-4o",
    })
    await validator.repair()
    assert await prefs.get("active_model") == {
        "provider_id": "anthropic", "model_id": None,
    }


@pytest.mark.asyncio
async def test_repair_clears_only_model_id_when_provider_still_valid() -> None:
    validator, prefs = _wire()
    await prefs.set("active_model", {
        "provider_id": "lmstudio", "model_id": "model-i-unloaded",
    })
    await validator.repair()
    assert await prefs.get("active_model") == {
        "provider_id": "lmstudio", "model_id": None,
    }


@pytest.mark.asyncio
async def test_repair_skips_validation_when_provider_lists_no_models() -> None:
    # Some providers don't enumerate (single-model endpoints, fake, …) —
    # we can't tell if model_id is bogus, so we leave it alone.
    validator, prefs = _wire(models_by_provider={
        "lmstudio": [],
        "anthropic": ["claude-sonnet-4-5"],
    })
    await prefs.set("active_model", {
        "provider_id": "lmstudio", "model_id": "anything-goes",
    })
    await validator.repair()
    assert await prefs.get("active_model") == {
        "provider_id": "lmstudio", "model_id": "anything-goes",
    }


@pytest.mark.asyncio
async def test_repair_drops_feature_override_when_provider_gone() -> None:
    validator, prefs = _wire()
    await prefs.set("feature_models", {
        FEATURE_POST_TURN: {
            "provider_id": "openai", "model_id": "gpt-4o",
        },
        FEATURE_GOAL_REVIEW: {
            "provider_id": "anthropic", "model_id": "claude-opus-4-7",
        },
    })
    await validator.repair()
    result = await prefs.get("feature_models")
    assert FEATURE_POST_TURN not in result
    assert result[FEATURE_GOAL_REVIEW] == {
        "provider_id": "anthropic", "model_id": "claude-opus-4-7",
    }


@pytest.mark.asyncio
async def test_repair_clears_feature_model_id_when_only_model_gone() -> None:
    validator, prefs = _wire()
    await prefs.set("feature_models", {
        FEATURE_POST_TURN: {
            "provider_id": "anthropic", "model_id": "claude-opus-99",
        },
    })
    await validator.repair()
    assert await prefs.get("feature_models") == {
        FEATURE_POST_TURN: {
            "provider_id": "anthropic", "model_id": None,
        },
    }


@pytest.mark.asyncio
async def test_repair_drops_unknown_feature_keys() -> None:
    validator, prefs = _wire()
    await prefs.set("feature_models", {
        "renamed_feature_long_ago": {
            "provider_id": "anthropic", "model_id": "claude-sonnet-4-5",
        },
        FEATURE_POST_TURN: {
            "provider_id": "anthropic", "model_id": "claude-sonnet-4-5",
        },
    })
    await validator.repair()
    result = await prefs.get("feature_models")
    assert "renamed_feature_long_ago" not in result
    assert FEATURE_POST_TURN in result


@pytest.mark.asyncio
async def test_repair_drops_group_override_when_provider_gone() -> None:
    validator, prefs = _wire()
    await prefs.set("feature_model_groups", {
        "core_structured_memory": {
            "provider_id": "openai", "model_id": "gpt-4o",
        },
        "player_facing_voice": {
            "provider_id": "anthropic", "model_id": "claude-opus-4-7",
        },
    })

    await validator.repair()

    result = await prefs.get("feature_model_groups")
    assert "core_structured_memory" not in result
    assert result["player_facing_voice"] == {
        "provider_id": "anthropic", "model_id": "claude-opus-4-7",
    }


@pytest.mark.asyncio
async def test_repair_clears_group_model_id_when_only_model_gone() -> None:
    validator, prefs = _wire()
    await prefs.set("feature_model_groups", {
        "core_structured_memory": {
            "provider_id": "anthropic", "model_id": "claude-opus-99",
        },
    })

    await validator.repair()

    assert await prefs.get("feature_model_groups") == {
        "core_structured_memory": {
            "provider_id": "anthropic", "model_id": None,
        },
    }


@pytest.mark.asyncio
async def test_repair_drops_unknown_group_keys() -> None:
    validator, prefs = _wire()
    await prefs.set("feature_model_groups", {
        "old_group": {
            "provider_id": "anthropic", "model_id": "claude-sonnet-4-5",
        },
        "core_structured_memory": {
            "provider_id": "anthropic", "model_id": "claude-sonnet-4-5",
        },
    })

    await validator.repair()

    result = await prefs.get("feature_model_groups")
    assert "old_group" not in result
    assert "core_structured_memory" in result

# ---- routing-level reasoning overrides --------------------------------


@pytest.mark.asyncio
async def test_repair_preserves_reasoning_when_rewriting_mapping() -> None:
    """Repairing one broken entry rewrites the whole mapping — sibling
    entries' reasoning objects must survive the rewrite."""
    validator, prefs = _wire()
    await prefs.set("feature_model_groups", {
        "core_structured_memory": {
            "provider_id": "openai", "model_id": "gpt-4o",
        },
        "high_reasoning_gates": {
            "provider_id": None,
            "model_id": None,
            "reasoning": {"reasoning_effort": "high"},
        },
    })

    await validator.repair()

    result = await prefs.get("feature_model_groups")
    assert "core_structured_memory" not in result
    assert result["high_reasoning_gates"]["reasoning"] == {
        "reasoning_effort": "high",
    }


@pytest.mark.asyncio
async def test_repair_keeps_reasoning_when_model_id_cleared() -> None:
    validator, prefs = _wire()
    await prefs.set("feature_models", {
        FEATURE_POST_TURN: {
            "provider_id": "anthropic",
            "model_id": "claude-opus-99",
            "reasoning": {"disable_reasoning": True},
        },
    })

    await validator.repair()

    result = await prefs.get("feature_models")
    assert result[FEATURE_POST_TURN] == {
        "provider_id": "anthropic",
        "model_id": None,
        "reasoning": {"disable_reasoning": True},
    }


@pytest.mark.asyncio
async def test_repair_keeps_reasoning_only_entry() -> None:
    """An entry pinning nothing but reasoning is valid configuration —
    startup repair must not treat it as an all-null entry."""
    validator, prefs = _wire()
    await prefs.set("feature_model_groups", {
        "light_observers": {
            "provider_id": None,
            "model_id": None,
            "reasoning": {"disable_reasoning": True},
        },
        # A genuinely-empty sibling forces the rewrite path.
        "critic_review": {"provider_id": None, "model_id": None},
    })

    await validator.repair()

    result = await prefs.get("feature_model_groups")
    assert "critic_review" not in result
    assert result["light_observers"]["reasoning"] == {
        "disable_reasoning": True,
    }


@pytest.mark.asyncio
async def test_repair_drops_malformed_reasoning_object() -> None:
    """Garbage reasoning shapes (wrong types, empty objects) are
    normalised away instead of being carried forever."""
    validator, prefs = _wire()
    await prefs.set("feature_models", {
        FEATURE_POST_TURN: {
            "provider_id": "anthropic",
            "model_id": "claude-sonnet-4-5",
            "reasoning": {"reasoning_effort": 42, "disable_reasoning": "yes"},
        },
        # Force the rewrite path.
        FEATURE_GOAL_REVIEW: {"provider_id": "openai", "model_id": "x"},
    })

    await validator.repair()

    result = await prefs.get("feature_models")
    assert result[FEATURE_POST_TURN] == {
        "provider_id": "anthropic",
        "model_id": "claude-sonnet-4-5",
    }


# ---- routing-level vision overrides -----------------------------------


@pytest.mark.asyncio
async def test_repair_preserves_vision_when_rewriting_mapping() -> None:
    """Repairing one broken entry rewrites the whole mapping — sibling
    entries' supports_vision pins must survive the rewrite."""
    validator, prefs = _wire()
    await prefs.set("feature_model_groups", {
        "core_structured_memory": {
            "provider_id": "openai", "model_id": "gpt-4o",
        },
        "multimodal_perception": {
            "provider_id": "anthropic",
            "model_id": "claude-sonnet-4-5",
            "supports_vision": True,
        },
    })

    await validator.repair()

    result = await prefs.get("feature_model_groups")
    assert "core_structured_memory" not in result
    assert result["multimodal_perception"] == {
        "provider_id": "anthropic",
        "model_id": "claude-sonnet-4-5",
        "supports_vision": True,
    }


@pytest.mark.asyncio
async def test_repair_keeps_vision_only_entry() -> None:
    """An entry pinning nothing but supports_vision is valid config — the
    all-null drop must not eat it."""
    validator, prefs = _wire()
    await prefs.set("feature_model_groups", {
        "multimodal_perception": {
            "provider_id": None,
            "model_id": None,
            "supports_vision": False,
        },
        # A genuinely-empty sibling forces the rewrite path.
        "critic_review": {"provider_id": None, "model_id": None},
    })

    await validator.repair()

    result = await prefs.get("feature_model_groups")
    assert "critic_review" not in result
    assert result["multimodal_perception"]["supports_vision"] is False


@pytest.mark.asyncio
async def test_repair_drops_malformed_vision_value() -> None:
    validator, prefs = _wire()
    await prefs.set("feature_models", {
        FEATURE_POST_TURN: {
            "provider_id": "anthropic",
            "model_id": "claude-sonnet-4-5",
            "supports_vision": "yes",
        },
        # Force the rewrite path.
        FEATURE_GOAL_REVIEW: {"provider_id": "openai", "model_id": "x"},
    })

    await validator.repair()

    result = await prefs.get("feature_models")
    assert result[FEATURE_POST_TURN] == {
        "provider_id": "anthropic",
        "model_id": "claude-sonnet-4-5",
    }


@pytest.mark.asyncio
async def test_repair_preserves_vision_on_active_model_when_model_cleared() -> None:
    validator, prefs = _wire()
    await prefs.set("active_model", {
        "provider_id": "lmstudio",
        "model_id": "model-i-unloaded",
        "supports_vision": False,
    })

    await validator.repair()

    assert await prefs.get("active_model") == {
        "provider_id": "lmstudio",
        "model_id": None,
        "supports_vision": False,
    }
