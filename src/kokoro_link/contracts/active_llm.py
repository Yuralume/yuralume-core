"""Resolve the "currently active" chat model at call time.

Historically every auxiliary LLM service (post-turn extractor, goal
reviewer, schedule planner, arc planner, memory consolidator, dialogue
summariser, prompt rewriter) was wired at container build time against
``default_provider_id`` — whatever ``KOKORO_DEFAULT_PROVIDER_ID``
said when the process started. The chat UI's provider/model dropdown
had no effect on those services: operators could switch from LM Studio
to Anthropic for the main reply, yet memory extraction still fired
against LM Studio.

This port lets services look up the model fresh on every call:

- Optional per-character override (``character.feature_models[key]``)
  — lets one character pin Anthropic Sonnet while the rest of the app
  stays on LM Studio.
- Read the global per-feature ``feature_models[key]`` preference (the
  picker in the "進階：各功能分別設定 LLM" panel).
- Read the global ``active_model`` preference (the same value the
  frontend's primary picker saves via
  ``PUT /system/preferences/active-model``).
- Resolve through the ``ChatModelRegistryPort``.
- Fall back to the default provider when nothing higher up resolves.

Services hold a ``ActiveLLMProviderPort`` instead of a frozen
``ChatModelPort`` so a mid-session dropdown flip — or a per-character
override edit — takes effect on the next post-turn / goal review /
schedule plan without a process restart.
"""

from __future__ import annotations

from typing import Protocol

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character


class ActiveLLMProviderPort(Protocol):
    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> ChatModelPort:
        """Return the chat model currently selected for ``feature_key``.

        Fallback chain (highest priority first):

        1. ``character.feature_models[feature_key]`` — only consulted
           when ``character`` is supplied. Lets per-character pins
           shadow every global setting for that one feature.
        2. Global ``feature_models[feature_key]`` preference.
        3. Global ``active_model`` preference.
        4. Container's default provider id.

        When ``feature_key`` is ``None`` the per-feature levels are
        skipped and resolution starts at ``active_model``.

        Never raises — auxiliary services are non-critical path and
        should degrade (return the default model) rather than crash
        the turn.
        """

    async def resolve_model_id(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> str | None:
        """Return the specific model id selected for ``feature_key``,
        or ``None`` when nothing pins one.

        ``None`` means "use whatever the provider considers its default"
        — auxiliary services pass this as the ``model=`` kwarg to
        ``ChatModelPort.generate`` so LM Studio / OpenAI-compatible
        servers pick up the right loaded model instead of whatever the
        container booted with.

        Same fallback chain as :meth:`resolve`. The two methods can
        return values from different levels in the chain (for example
        a per-character entry pins ``provider_id`` but leaves
        ``model_id`` blank → the model id falls through to the global
        ``active_model``).
        """

    async def is_fake(
        self,
        feature_key: str | None = None,
        *,
        character: Character | None = None,
        operator_id: str | None = None,
        content_tolerance: str | None = None,
    ) -> bool:
        """Return ``True`` when the provider currently routed for
        ``feature_key`` (with optional character override) is the
        built-in fake model.

        Processors use this to short-circuit: producing JSON-memory
        output via ``FakeChatModel`` is garbage, so a resolved-to-fake
        auxiliary call should just return an empty result instead of
        polluting storage.
        """
