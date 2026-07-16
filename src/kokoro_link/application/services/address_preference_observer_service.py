"""Address preference observation service (HUMANIZATION_ROADMAP §4.2).

Reads recent user messages, calls an LLM-backed observer, and upserts
the resulting ``OperatorAddressPreference``. Runs in the dream pass tail
stage (same cadence as §3.3 phrase habit extraction) — observing every
turn would burn LLM budget for marginal change.

LLM-first stance: the observer chooses bands and salutation freely; we
don't pre-filter messages, don't pattern-match honorifics in Python.
The only Python-side rule is "tide-mark" — we keep the prior preference
when the observer returns ``None`` or empty fields, so the LLM doesn't
have to re-derive stable signals every pass.

Owner decision (2026-05-21): the observed value **overrides** the §3.6
``operator_pace_preference`` explicit setting at prompt-render time;
that priority lives in the prompt builder, not here.
"""

from __future__ import annotations

import logging

from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.contracts.operator_address_preference import (
    OperatorAddressObserverPort,
    OperatorAddressPreferenceRepositoryPort,
)
from kokoro_link.domain.entities.conversation import MessageRole
from kokoro_link.domain.entities.operator_address_preference import (
    OperatorAddressPreference,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID

_LOGGER = logging.getLogger(__name__)


class AddressPreferenceObserverService:
    """Orchestrates one observation pass for a ``(character, operator)`` pair."""

    def __init__(
        self,
        *,
        repository: OperatorAddressPreferenceRepositoryPort,
        observer: OperatorAddressObserverPort,
        settings: HumanizationSettings,
        recent_message_window: int = 20,
        conversation_repository: ConversationRepositoryPort | None = None,
        seed_repository=None,
        operator_profile_service=None,
    ) -> None:
        self._repository = repository
        self._observer = observer
        self._settings = settings
        self._window = recent_message_window
        self._conversations = conversation_repository
        # #3 direction-inversion guard deps (optional / fail-soft). When
        # wired, an observed salutation (direction B) that structurally
        # collides with a direction-A authority — the seed
        # ``user_address_name`` (how the character addresses the player) or
        # the operator's own name/display_name — is dropped as a suspected
        # inversion. Only observations are guarded; a player's explicit
        # setting never flows through this path.
        self._seeds = seed_repository
        self._profiles = operator_profile_service

    async def observe_recent_for_pair(
        self,
        *,
        character_id: str,
        operator_id: str = DEFAULT_OPERATOR_ID,
    ) -> OperatorAddressPreference | None:
        """Dream-pass entry point: pull recent user messages from the
        unified cross-source timeline and hand them to :meth:`observe_pair`.

        Cross-channel merge stays consistent with the §3.6 / §4.2
        precedent — the observer treats the user as one person across
        every source (web + telegram + line + …).
        """
        if self._conversations is None:
            return None
        try:
            messages = await self._conversations.recent_messages_for_character(
                character_id, limit=self._window * 3,
            )
        except Exception:
            _LOGGER.exception(
                "address observer: failed to pull recent messages character=%s",
                character_id,
            )
            return None
        recent_user_messages: list[str] = []
        for msg in messages:
            if msg.role != MessageRole.USER:
                continue
            content = (msg.content or "").strip()
            if not content:
                continue
            recent_user_messages.append(content)
        recent_user_messages = recent_user_messages[-self._window:]
        return await self.observe_pair(
            character_id=character_id,
            operator_id=operator_id,
            recent_user_messages=recent_user_messages,
        )

    async def observe_pair(
        self,
        *,
        character_id: str,
        operator_id: str,
        recent_user_messages: list[str],
    ) -> OperatorAddressPreference | None:
        if not self._settings.address_preference_enabled:
            return None
        if not recent_user_messages:
            return None
        window = recent_user_messages[-self._window:]
        try:
            candidate = await self._observer.observe(
                character_id=character_id,
                operator_id=operator_id,
                recent_user_messages=window,
            )
        except Exception:
            _LOGGER.exception(
                "address preference observer raised; leaving prior preference intact",
            )
            return None
        if candidate is None:
            return None
        candidate = await self._guard_direction_inversion(
            character_id=character_id,
            operator_id=operator_id,
            candidate=candidate,
        )
        prior = await self._repository.get(
            character_id=character_id, operator_id=operator_id,
        )
        base = prior or OperatorAddressPreference.empty(
            character_id=character_id, operator_id=operator_id,
        )
        # Empty fields mean "no change" — preserve prior values so the
        # extractor can incrementally refine without overwriting confirmed
        # signals every time the user happens not to use a salutation.
        merged = base.with_updates(
            salutation=candidate.salutation if candidate.salutation else None,
            formality_level=(
                candidate.formality_level
                if candidate.formality_level else None
            ),
            response_length_pref=(
                candidate.response_length_pref
                if candidate.response_length_pref else None
            ),
            evidence_quote=(
                candidate.evidence_quote
                if candidate.evidence_quote else None
            ),
        )
        if merged.is_empty:
            return None
        await self._repository.upsert(merged)
        return merged

    async def _guard_direction_inversion(
        self,
        *,
        character_id: str,
        operator_id: str,
        candidate: AddressObservationCandidate,
    ) -> AddressObservationCandidate:
        """Drop an observed ``salutation`` that structurally matches a
        direction-A authority (seed ``user_address_name`` or the operator's
        own name/display_name) — a suspected direction inversion.

        Normalisation is strip + casefold exact; no fuzzy matching. Only
        the salutation field is affected — register/length bands still flow
        through. Fail-soft: any lookup error leaves the observation intact
        rather than silently discarding a valid signal.
        """
        salutation = (candidate.salutation or "").strip()
        if not salutation:
            return candidate
        target = salutation.casefold()
        forbidden: set[str] = set()
        if self._seeds is not None:
            try:
                seed = await self._seeds.get(character_id, operator_id)
            except Exception:
                seed = None
                _LOGGER.exception(
                    "address guard: seed lookup failed char=%s op=%s",
                    character_id, operator_id,
                )
            user_address = (
                getattr(seed, "user_address_name", "") or ""
            ).strip()
            if user_address:
                forbidden.add(user_address.casefold())
        if self._profiles is not None:
            try:
                profile = await self._profiles.get_for_user(operator_id)
            except Exception:
                profile = None
                _LOGGER.exception(
                    "address guard: profile lookup failed op=%s", operator_id,
                )
            display_name = (
                getattr(profile, "display_name", "") or ""
            ).strip()
            if display_name:
                forbidden.add(display_name.casefold())
        if target in forbidden:
            _LOGGER.warning(
                "observed salutation dropped: %r matches how the character "
                "addresses the player (direction inversion suspected) "
                "char=%s op=%s",
                salutation, character_id, operator_id,
            )
            from dataclasses import replace

            return replace(candidate, salutation=None)
        return candidate
