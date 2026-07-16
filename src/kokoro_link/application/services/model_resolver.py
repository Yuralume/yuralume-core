"""Shared helper for auxiliary LLM services.

Every auxiliary processor (post-turn, goal review, schedule plan, arc
plan, memory consolidate, dialogue summarise, prompt rewrite, draft
generate, proactive decide) used to hold a frozen ``ChatModelPort``
bound at container build time. That meant the frontend model picker
only affected the main chat generation — memory extraction etc.
silently stayed on the default provider.

``ModelResolver`` wraps either a live ``ActiveLLMProviderPort`` (the
new preferred path — reads the active-model preference on each call)
or a fixed ``ChatModelPort`` (the legacy path, kept for unit tests
that want a deterministic backend). Processors compose this helper:

    self._resolver = ModelResolver(provider=..., feature_key="post_turn")
    ...
    raw = await self._resolver.generate(prompt, character=character)

Exactly one of ``provider`` / ``model`` must be supplied. The helper
never resolves to ``None`` — failure modes (preference stale, registry
misconfigured) return the default provider's model so the call still
runs, just with degraded routing.

Per-character overrides: every method accepts an optional ``character``
keyword. When supplied + the resolver is wrapping a live provider,
``character.feature_models[feature_key]`` takes priority over the
global preferences. The fixed-model path (tests) ignores ``character``
because there's only one backend wired anyway.
"""

from __future__ import annotations

from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.observability.llm_metadata_wrapper import (
    CapturedGeneration,
    MetadataCapturingChatModel,
)

_ProviderContext = dict[str, Character | str | None]


class ModelResolver:
    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        """Exactly one of ``provider`` / ``model`` must be supplied.

        ``feature_key`` tags this resolver for per-feature routing
        (``post_turn`` / ``goal_review`` / ...). When set, the
        underlying ``ActiveLLMProviderPort`` looks up the
        ``feature_models`` preference first and falls back to the
        global ``active_model`` if nothing is pinned for the key.
        """
        if (provider is None) == (model is None):
            raise ValueError(
                "ModelResolver requires exactly one of provider / model",
            )
        self._provider = provider
        self._model = model
        self._feature_key = feature_key

    @property
    def supports_content_tolerance_routing(self) -> bool:
        return self._provider is not None

    @staticmethod
    def _provider_context_kwargs(
        *,
        character: Character | None,
        operator_id: str | None,
        content_tolerance: str | None,
    ) -> _ProviderContext:
        kwargs: _ProviderContext = {"character": character}
        if operator_id is not None:
            kwargs["operator_id"] = operator_id
        if content_tolerance is not None:
            kwargs["content_tolerance"] = content_tolerance
        return kwargs

    async def resolve(
        self,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> tuple[ChatModelPort, str | None]:
        """Return ``(model, model_id)`` — ``model_id`` is None when the
        caller should let the provider pick its default.

        ``character`` (optional) forwards the per-character override
        chain to the live provider. Ignored on the fixed-model path."""
        if self._provider is not None:
            context_kwargs = self._provider_context_kwargs(
                character=character,
                operator_id=operator_id,
                content_tolerance=content_tolerance,
            )
            model = await self._provider.resolve(
                self._feature_key,
                **context_kwargs,
            )
            model_id = await self._provider.resolve_model_id(
                self._feature_key,
                **context_kwargs,
            )
            return model, model_id
        assert self._model is not None
        return self._model, None

    async def generate(
        self,
        prompt: str,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
        **kwargs,
    ) -> str:
        """Resolve the active model and call ``generate``.

        When we hold a live provider and it reports a specific
        ``model_id``, forward it as the ``model=`` kwarg so LM Studio /
        OpenAI-compatible servers route to the right loaded model. In
        the fixed-model path (tests) no override is needed — we pass
        only the kwargs the caller supplied, so legacy test mocks with
        ``async def generate(self, prompt: str)`` keep working.
        """
        model, model_id = await self.resolve(
            character=character,
            operator_id=operator_id,
            content_tolerance=content_tolerance,
        )
        if model_id is not None and "model" not in kwargs:
            kwargs["model"] = model_id
        return await model.generate(prompt, **kwargs)

    async def generate_with_metadata(
        self,
        prompt: str,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
        **kwargs,
    ) -> tuple[CapturedGeneration, str]:
        """Resolve the active model and call ``generate`` with metadata.

        Returns ``(captured, provider_id)`` so observability callers can
        show both the selected model and the adapter/provider that handled
        the request. The ordinary ``generate`` method remains unchanged for
        existing processors that do not need replay metadata.
        """
        model, model_id = await self.resolve(
            character=character,
            operator_id=operator_id,
            content_tolerance=content_tolerance,
        )
        if model_id is not None and "model" not in kwargs:
            kwargs["model"] = model_id
        captured = await MetadataCapturingChatModel(model).generate_capturing(
            prompt,
            **kwargs,
        )
        return captured, getattr(model, "provider_id", "")

    async def is_fake(
        self,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> bool:
        """Return ``True`` when the resolved provider is the built-in
        fake backend — processors use this to short-circuit because
        ``FakeChatModel`` emits deterministic junk that won't parse as
        JSON schemas and would pollute storage."""
        if self._provider is not None:
            context_kwargs = self._provider_context_kwargs(
                character=character,
                operator_id=operator_id,
                content_tolerance=content_tolerance,
            )
            return await self._provider.is_fake(
                self._feature_key,
                **context_kwargs,
            )
        return False
