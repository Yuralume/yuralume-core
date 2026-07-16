"""Save-time validation for free-form routing reasoning effort values.

Different OpenAI-compatible providers and models accept different literals,
so the UI intentionally keeps ``reasoning_effort`` as free text. Syntax-only
validation cannot prove a provider/model pair supports a value; adapters that
offer the optional validation hook therefore probe their real upstream before
the preference is committed.
"""

from __future__ import annotations

from kokoro_link.contracts.llm import ChatModelRegistryPort


class ReasoningEffortValidationError(ValueError):
    """The selected provider/model could not validate an effort value."""


class RoutingReasoningValidationService:
    def __init__(self, registry: ChatModelRegistryPort) -> None:
        self._registry = registry

    async def validate(
        self,
        *,
        provider_id: str,
        model_id: str | None,
        effort: str,
    ) -> None:
        try:
            model = self._registry.resolve(provider_id)
        except ValueError as exc:
            raise ReasoningEffortValidationError(str(exc)) from exc

        validate = getattr(model, "validate_reasoning_effort", None)
        if not callable(validate):
            raise ReasoningEffortValidationError(
                f"provider {provider_id!r} does not support reasoning_effort "
                "validation",
            )
        try:
            await validate(effort, model=model_id)
        except ReasoningEffortValidationError:
            raise
        except ValueError as exc:
            raise ReasoningEffortValidationError(str(exc)) from exc
        except Exception as exc:
            raise ReasoningEffortValidationError(
                "reasoning_effort validation request failed for provider "
                f"{provider_id!r}: {type(exc).__name__}",
            ) from exc
