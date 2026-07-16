from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.application.services.emotion_aggregator import (
    ExponentialDecayEmotionAggregator,
)
from kokoro_link.application.services.emotion_state_projection import (
    project_state_from_emotion_events,
)
from kokoro_link.application.dto.character import (
    CharacterResponse,
    CreateCharacterRequest,
    InitialRelationshipSafeUserProfilePayload,
    UpdateCharacterRequest,
    payload_to_state,
)
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
    AccountRuntimeUsageRepositoryPort,
)
from kokoro_link.contracts.album import AlbumRepositoryPort
from kokoro_link.contracts.arc_series import ArcSeriesRepositoryPort
from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.contracts.clock import ClockPort
from kokoro_link.contracts.emotion import (
    EmotionAggregatorPort,
    EmotionEventRepositoryPort,
)
from kokoro_link.contracts.goal_repository import GoalRepositoryPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.operator_persona import OperatorPersonaRepositoryPort
from kokoro_link.contracts.pending_follow_up import (
    PendingFollowUpRepositoryPort,
)
from kokoro_link.contracts.proactive import ProactiveAttemptRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort, ConversationRepositoryPort
from kokoro_link.contracts.tool import ToolInvocationRepositoryPort
from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.contracts.state_history import StateHistoryRepositoryPort
from kokoro_link.contracts.story_arc import StoryArcRepositoryPort
from kokoro_link.contracts.image_profile import FeatureImageProfileOverride
from kokoro_link.contracts.video_profile import FeatureVideoProfileOverride
from kokoro_link.domain.entities.character import (
    Character,
    CharacterLora,
    FeatureModelOverride,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.state_snapshot import SOURCE_MANUAL
from kokoro_link.domain.value_objects.companion import CharacterCompanion
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)
from kokoro_link.domain.value_objects.profile_field import EvidenceRef, ProfileField

if TYPE_CHECKING:
    from kokoro_link.application.services.rest_recovery_refresher import (
        RestRecoveryRefresher,
    )
    from kokoro_link.application.services.state_tracker import StateChangeTracker
    from kokoro_link.application.services.subscription_access_guard import (
        SubscriptionAccessGuard,
    )


_LOGGER = logging.getLogger(__name__)


class CharacterValidationError(ValueError):
    """Character mutation payload is invalid for the current user."""


def _payload_feature_models(
    payload: "CreateCharacterRequest | UpdateCharacterRequest",
) -> tuple[FeatureModelOverride, ...]:
    """Convert the DTO list into the domain tuple, dropping blanks.

    Used by both create and update paths. Each ``to_domain()`` returns
    ``None`` for an all-blank row so the operator can clear an entry by
    sending ``{feature_key: "...", provider_id: null, model_id: null}``
    without us re-checking that rule here."""
    raw = getattr(payload, "feature_models", None) or []
    return tuple(
        domain for domain in (entry.to_domain() for entry in raw)
        if domain is not None
    )


def _payload_feature_image_profiles(
    payload: "CreateCharacterRequest | UpdateCharacterRequest",
) -> tuple[FeatureImageProfileOverride, ...]:
    """Mirror of :func:`_payload_feature_models` for image-profile
    overrides. Drops blank rows so a clear-an-entry payload doesn't
    leave a no-op pin behind."""
    raw = getattr(payload, "feature_image_profiles", None) or []
    return tuple(
        domain for domain in (entry.to_domain() for entry in raw)
        if domain is not None
    )


def _payload_feature_video_profiles(
    payload: "CreateCharacterRequest | UpdateCharacterRequest",
) -> tuple[FeatureVideoProfileOverride, ...]:
    raw = getattr(payload, "feature_video_profiles", None) or []
    return tuple(
        domain for domain in (entry.to_domain() for entry in raw)
        if domain is not None
    )


def _payload_companions(
    payload: "CreateCharacterRequest | UpdateCharacterRequest",
) -> tuple[CharacterCompanion, ...]:
    """Convert companion payload list into the domain tuple.

    Drops entries that won't construct (e.g. blank ``name``) — the
    operator gets the rest persisted rather than the whole save failing
    because of one malformed row."""
    raw = getattr(payload, "companions", None) or []
    return tuple(
        domain for domain in (entry.to_domain() for entry in raw)
        if domain is not None
    )


def _safe_profile_fields(
    *,
    character_id: str,
    profile: InitialRelationshipSafeUserProfilePayload,
    now: datetime,
) -> tuple[ProfileField, ...]:
    values: list[tuple[int, str, str]] = []
    _append_value(values, 1, "name", profile.name)
    _append_value(values, 1, "nickname", profile.nickname)
    _append_value(values, 2, "occupation", profile.occupation)
    _append_value(values, 2, "company_or_school", profile.company_or_school)
    _append_value(values, 2, "interests", "、".join(_clean_list(profile.interests)))
    _append_value(values, 2, "routine", profile.routine)
    _append_value(values, 2, "life_goals", "、".join(_clean_list(profile.life_goals)))
    fields: list[ProfileField] = []
    for layer, key, value in values:
        evidence = EvidenceRef(
            turn_id="character_creation_seed",
            conversation_id="character_creation_seed",
            quote=value,
            extracted_at=now,
        )
        fields.append(
            ProfileField(
                character_id=character_id,
                field_key=key,
                layer=layer,
                value=value,
                confidence=0.95,
                evidence_refs=(evidence,),
                last_updated=now,
                update_count=1,
                source="user_explicit",
            )
        )
    return tuple(fields)


def _append_value(
    values: list[tuple[int, str, str]],
    layer: int,
    key: str,
    value: str,
) -> None:
    text = (value or "").strip()
    if text:
        values.append((layer, key, text))


def _clean_list(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = (value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _dob_update_kwarg(
    payload: "UpdateCharacterRequest",
) -> dict[str, object]:
    """Translate the DTO's tri-state ``date_of_birth`` into a kwarg
    dict the domain ``Character.update`` understands.

    Same pattern as ``_arc_template_update_kwarg``: Pydantic v2's
    ``model_fields_set`` lets us tell "field omitted" from "explicit
    null". Omitted → ``{}`` (leave existing alone); ``null`` →
    ``{"date_of_birth": None}`` (clear); ``date`` → ``{"date_of_birth": <date>}``.
    """
    if "date_of_birth" not in payload.model_fields_set:
        return {}
    return {"date_of_birth": payload.date_of_birth}


def _arc_template_update_kwarg(
    payload: "UpdateCharacterRequest",
) -> dict[str, str | None]:
    """Translate the DTO's tri-state ``arc_template_id`` into a kwarg
    dict the domain ``Character.update`` understands.

    - field omitted from the request body → ``{}`` (leave alone)
    - field present, value ``null`` → ``{"arc_template_id": None}`` (unbind)
    - field present, value ``"abc"`` → ``{"arc_template_id": "abc"}`` (bind)

    Pydantic v2's ``model_fields_set`` distinguishes "omitted" from
    "explicit null"; ``Character.update`` uses a sentinel default so
    the absence here naturally leaves the existing binding in place.
    """
    if "arc_template_id" not in payload.model_fields_set:
        return {}
    return {"arc_template_id": payload.arc_template_id}


def _arc_series_update_kwarg(
    payload: "UpdateCharacterRequest",
) -> dict[str, str | None]:
    """Translate the DTO's tri-state ``arc_series_id`` into a domain kwarg."""
    if "arc_series_id" not in payload.model_fields_set:
        return {}
    return {"arc_series_id": payload.arc_series_id}


class CharacterService:
    def __init__(
        self,
        repository: CharacterRepositoryPort,
        *,
        conversation_repository: ConversationRepositoryPort | None = None,
        memory_repository: MemoryRepositoryPort | None = None,
        goal_repository: GoalRepositoryPort | None = None,
        schedule_repository: ScheduleRepositoryPort | None = None,
        state_history_repository: StateHistoryRepositoryPort | None = None,
        proactive_attempt_repository: ProactiveAttemptRepositoryPort | None = None,
        tool_invocation_repository: ToolInvocationRepositoryPort | None = None,
        album_repository: AlbumRepositoryPort | None = None,
        story_arc_repository: "StoryArcRepositoryPort | None" = None,
        pending_follow_up_repository: PendingFollowUpRepositoryPort | None = None,
        operator_persona_repository: OperatorPersonaRepositoryPort | None = None,
        relationship_seed_repository: (
            CharacterOperatorRelationshipSeedRepositoryPort | None
        ) = None,
        state_tracker: "StateChangeTracker | None" = None,
        rest_recovery_refresher: "RestRecoveryRefresher | None" = None,
        emotion_event_repository: EmotionEventRepositoryPort | None = None,
        emotion_aggregator: EmotionAggregatorPort | None = None,
        arc_series_repository: ArcSeriesRepositoryPort | None = None,
        arc_template_repository: ArcTemplateRepositoryPort | None = None,
        account_runtime_profile_resolver: (
            AccountRuntimeProfileResolverPort | None
        ) = None,
        account_runtime_usage_repository: (
            AccountRuntimeUsageRepositoryPort | None
        ) = None,
        clock: ClockPort | None = None,
        subscription_access_guard: "SubscriptionAccessGuard | None" = None,
    ) -> None:
        self._repository = repository
        self._conversation_repository = conversation_repository
        self._memory_repository = memory_repository
        self._goal_repository = goal_repository
        self._schedule_repository = schedule_repository
        self._state_history_repository = state_history_repository
        self._proactive_attempt_repository = proactive_attempt_repository
        self._tool_invocation_repository = tool_invocation_repository
        self._album_repository = album_repository
        self._story_arc_repository = story_arc_repository
        self._pending_follow_up_repository = pending_follow_up_repository
        self._operator_persona_repository = operator_persona_repository
        self._relationship_seed_repository = relationship_seed_repository
        self._state_tracker = state_tracker
        self._rest_recovery_refresher = rest_recovery_refresher
        self._emotion_event_repository = emotion_event_repository
        self._emotion_aggregator: EmotionAggregatorPort = (
            emotion_aggregator or ExponentialDecayEmotionAggregator()
        )
        self._arc_series_repository = arc_series_repository
        self._arc_template_repository = arc_template_repository
        self._account_runtime_profile_resolver = (
            account_runtime_profile_resolver
            or PermissiveAccountRuntimeProfileResolver()
        )
        self._account_runtime_usage_repository = account_runtime_usage_repository
        self._clock = clock
        self._subscription_access_guard = subscription_access_guard

    async def create_character(
        self,
        payload: CreateCharacterRequest,
        *,
        user_id: str = DEFAULT_OPERATOR_ID,
    ) -> CharacterResponse:
        if self._subscription_access_guard is not None:
            await self._subscription_access_guard.ensure_operator_allowed(user_id)
        now = self._now()
        runtime_profile = await self._validate_runtime_profile_allows_character_create(
            user_id,
            now=now,
        )
        character = Character.create(
            name=payload.name,
            summary=payload.summary,
            user_id=user_id,
            personality=payload.personality,
            interests=payload.interests,
            speaking_style=payload.speaking_style,
            boundaries=payload.boundaries,
            aspirations=payload.aspirations,
            appearance=payload.appearance,
            gender_identity=payload.gender_identity,
            third_person_pronoun=payload.third_person_pronoun,
            visual_gender_presentation=payload.visual_gender_presentation,
            visual_subject_type=payload.visual_subject_type,
            visual_generation_style=payload.visual_generation_style,
            date_of_birth=payload.date_of_birth,
            image_urls=tuple(payload.image_urls),
            allowed_tools=tuple(payload.allowed_tools),
            loras=tuple(
                CharacterLora(name=l.name, strength=l.strength)
                for l in payload.loras
            ),
            state=payload_to_state(payload.initial_state),
            proactive_enabled=payload.proactive_enabled,
            proactive_daily_limit=payload.proactive_daily_limit,
            proactive_cooldown_minutes=payload.proactive_cooldown_minutes,
            feed_daily_limit=payload.feed_daily_limit,
            world_awareness_enabled=payload.world_awareness_enabled,
            world_topics=tuple(payload.world_topics),
            subscribed_categories=tuple(payload.subscribed_categories),
            excluded_topics=tuple(payload.excluded_topics),
            world_frame=payload.world_frame,
            accepts_web_proactive=payload.accepts_web_proactive,
            arc_template_id=payload.arc_template_id,
            arc_series_id=payload.arc_series_id,
            feature_models=_payload_feature_models(payload),
            feature_image_profiles=_payload_feature_image_profiles(payload),
            feature_video_profiles=_payload_feature_video_profiles(payload),
            companions=_payload_companions(payload),
            disposition=payload.disposition.to_domain(),
            body_state=payload.body_state.to_domain(),
            operator_pace_preference=payload.operator_pace_preference,
            personality_type=payload.personality_type.to_domain(),
        )
        await self._validate_arc_template_binding(
            character.arc_template_id, character=character,
        )
        await self._validate_arc_series_binding(
            character.arc_series_id, character=character,
        )
        await self._repository.save(character)
        try:
            await self._save_initial_relationship_seed(
                character=character,
                payload=payload,
                operator_id=user_id,
                now=now,
            )
            await self._record_runtime_profile_character_create(
                character_id=character.id,
                user_id=user_id,
                profile=runtime_profile,
                now=now,
            )
        except Exception:
            await self._repository.delete(character.id)
            raise
        return CharacterResponse.from_domain(character)

    async def _validate_runtime_profile_allows_character_create(
        self,
        user_id: str,
        *,
        now: datetime,
    ) -> AccountRuntimeProfile | None:
        profile = await self._account_runtime_profile_resolver.resolve_for_operator(
            user_id,
        )
        if profile.max_characters is not None:
            current = await self._repository.list_for_user(user_id)
            if len(current) >= profile.max_characters:
                raise CharacterValidationError(
                    f"account runtime profile character limit reached "
                    f"({profile.max_characters})",
                )
        if profile.daily_character_create_limit is not None:
            if self._account_runtime_usage_repository is None:
                raise CharacterValidationError(
                    "account runtime profile character create ledger is not "
                    "configured",
                )
            used = await self._account_runtime_usage_repository.count_events(
                operator_id=user_id,
                event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
                since=now - timedelta(hours=24),
                until=now,
            )
            if used >= profile.daily_character_create_limit:
                raise CharacterValidationError(
                    "account runtime profile daily character create limit "
                    f"reached ({profile.daily_character_create_limit}/24h)",
                )
        return profile

    async def _record_runtime_profile_character_create(
        self,
        *,
        character_id: str,
        user_id: str,
        profile: AccountRuntimeProfile | None,
        now: datetime,
    ) -> None:
        if profile is None or profile.daily_character_create_limit is None:
            return
        if self._account_runtime_usage_repository is None:
            raise CharacterValidationError(
                "account runtime profile character create ledger is not configured",
            )
        await self._account_runtime_usage_repository.record_event(
            operator_id=user_id,
            event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE,
            occurred_at=now,
            resource_id=character_id,
        )

    async def _save_initial_relationship_seed(
        self,
        *,
        character: Character,
        payload: CreateCharacterRequest,
        operator_id: str,
        now: datetime,
    ) -> None:
        initial = payload.initial_relationship
        if initial is None:
            return
        if self._relationship_seed_repository is None:
            raise CharacterValidationError(
                "initial relationship repository is not configured",
            )
        seed = initial.to_seed(
            character_id=character.id,
            operator_id=operator_id,
            now=now,
        )
        if not seed.is_empty:
            await self._relationship_seed_repository.save(seed)
        if initial.safe_user_profile.has_values():
            if self._operator_persona_repository is None:
                raise CharacterValidationError(
                    "operator persona repository is not configured",
                )
            for field in _safe_profile_fields(
                character_id=character.id,
                profile=initial.safe_user_profile,
                now=now,
            ):
                await self._operator_persona_repository.upsert_field(
                    character.id,
                    operator_id,
                    field,
                )

    async def list_characters(
        self, *, user_id: str | None = None,
    ) -> list[CharacterResponse]:
        """List characters.

        When ``user_id`` is provided, filters to that owner; otherwise
        returns the unfiltered view (background services / migration
        helpers that operate per-character). HTTP callers always pass
        ``user_id`` via the dependency layer."""
        if user_id is None:
            characters = await self._repository.list()
        else:
            characters = await self._repository.list_for_user(user_id)
        refreshed = [await self._refresh(character) for character in characters]
        return [CharacterResponse.from_domain(character) for character in refreshed]

    async def get_character(
        self, character_id: str, *, user_id: str | None = None,
    ) -> CharacterResponse | None:
        character = await self._repository.get(character_id)
        if character is None:
            return None
        if user_id is not None and character.user_id != user_id:
            # Collapse cross-user access to "not found" to avoid
            # enumeration. Same status the API layer returns when
            # the character genuinely doesn't exist.
            return None
        character = await self._refresh(character)
        return CharacterResponse.from_domain(character)

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(timezone.utc)

    async def get_character_entity(
        self, character_id: str, *, user_id: str | None = None,
    ) -> Character | None:
        """Return the domain entity for internal/service callers."""
        character = await self._repository.get(character_id)
        if character is None:
            return None
        if user_id is not None and character.user_id != user_id:
            return None
        return await self._refresh(character)

    async def _refresh(self, character: Character) -> Character:
        """Read-time rest recovery refresh.

        Pure lazy recovery used to run only on chat turns, so the UI
        showed stale ``energy=0`` for hours after a restart. Now every
        read goes through this, the UI converges, and ``proactive_gate``
        — which reads the same rows via its own fetch — sees the
        recovered numbers immediately.
        """
        if self._rest_recovery_refresher is None:
            return await self._project_emotion_state(character)
        refreshed = await self._rest_recovery_refresher.refresh(character)
        return await self._project_emotion_state(refreshed)

    async def _project_emotion_state(self, character: Character) -> Character:
        if self._emotion_event_repository is None:
            return character
        now = datetime.now(timezone.utc)
        try:
            events = await self._emotion_event_repository.list_recent(
                character_id=character.id,
                operator_id=DEFAULT_OPERATOR_ID,
                since=now - timedelta(hours=24),
                limit=30,
            )
        except Exception:
            _LOGGER.exception(
                "emotion_event_repository.list_recent failed (character=%s)",
                character.id,
            )
            return character
        projected = project_state_from_emotion_events(
            state=character.state,
            events=events,
            aggregator=self._emotion_aggregator,
            now=now,
        )
        return character.with_state(projected)

    async def delete_character(
        self, character_id: str, *, user_id: str | None = None,
    ) -> bool:
        """Cascade-delete a character and all owned data.

        Order matters: child records first, so a partial failure never
        leaves a character row whose memories or conversations are
        orphaned. Returns ``True`` when the character existed.

        When ``user_id`` is provided and the character belongs to a
        different user, returns ``False`` (treated as "not found" by
        the API layer)."""
        if user_id is not None:
            character = await self._repository.get(character_id)
            if character is None or character.user_id != user_id:
                return False
        if self._state_history_repository is not None:
            await self._state_history_repository.delete_for_character(character_id)
        if self._goal_repository is not None:
            await self._goal_repository.delete_for_character(character_id)
        if self._schedule_repository is not None:
            await self._schedule_repository.delete_for_character(character_id)
        if self._memory_repository is not None:
            await self._memory_repository.delete_for_character(character_id)
        if self._proactive_attempt_repository is not None:
            await self._proactive_attempt_repository.delete_for_character(character_id)
        if self._tool_invocation_repository is not None:
            await self._tool_invocation_repository.delete_for_character(character_id)
        if self._album_repository is not None:
            # SA path cascades via FK; explicit call here handles the
            # in-memory repo path + keeps the ordering contract clean
            # (children before parent).
            await self._album_repository.delete_for_character(character_id)
        if self._story_arc_repository is not None:
            await self._story_arc_repository.delete_for_character(character_id)
        if self._operator_persona_repository is not None:
            await self._operator_persona_repository.delete_for_character(character_id)
        if self._relationship_seed_repository is not None:
            await self._relationship_seed_repository.delete_for_character(character_id)
        if self._pending_follow_up_repository is not None:
            # Run before the conversation cascade so the FK from
            # ``pending_follow_ups.conversation_id`` doesn't cascade-
            # delete the rows in an order the in-memory repo can't
            # observe (SA path is fine either way; in-memory path needs
            # this explicit call).
            await self._pending_follow_up_repository.delete_for_character(
                character_id,
            )
        if self._conversation_repository is not None:
            await self._conversation_repository.delete_for_character(character_id)
        return await self._repository.delete(character_id)

    async def reset_character_data(
        self,
        character_id: str,
        *,
        memories: bool = False,
        conversations: bool = False,
        state_history: bool = False,
        operator_persona: bool = False,
        user_id: str | None = None,
    ) -> tuple[int, int, int, int] | None:
        """Selectively wipe ancillary data owned by a character.

        Returns ``(memories_deleted, conversations_deleted,
        state_history_deleted, operator_persona_deleted)``
        or ``None`` when the character doesn't exist. The character row
        itself is **never** removed — for that, use ``delete_character``.

        Intended for the identity-drift "clear memory" escape hatch: the
        operator is about to rewrite the character's personality, and
        wants to prevent old memories / chat logs from dragging the new
        persona back to the old one. Schedules + goals are left alone
        because they're easy to re-author manually and deleting them
        hides useful planning history.
        """
        existing = await self._repository.get(character_id)
        if existing is None:
            return None
        if user_id is not None and existing.user_id != user_id:
            return None
        memories_deleted = 0
        conversations_deleted = 0
        state_history_deleted = 0
        operator_persona_deleted = 0
        if memories and self._memory_repository is not None:
            memories_deleted = await self._memory_repository.delete_for_character(
                character_id,
            )
        if conversations and self._conversation_repository is not None:
            conversations_deleted = await self._conversation_repository.delete_for_character(
                character_id,
            )
        if state_history and self._state_history_repository is not None:
            state_history_deleted = await self._state_history_repository.delete_for_character(
                character_id,
            )
        if operator_persona and self._operator_persona_repository is not None:
            operator_persona_deleted = await self._operator_persona_repository.delete_for_character(
                character_id,
            )
        return (
            memories_deleted,
            conversations_deleted,
            state_history_deleted,
            operator_persona_deleted,
        )

    async def mark_web_conversation_read(
        self, character_id: str,
    ) -> CharacterResponse | None:
        """Reset the proactive unread counter for this character.

        Called when the web UI opens / focuses the chat panel for a
        character — the user has now seen any pending proactive
        messages, so the sidebar badge should clear. Idempotent: safe
        to call repeatedly without side effects beyond a single write.
        """
        character = await self._repository.get(character_id)
        if character is None:
            return None
        if character.unread_proactive_count == 0:
            # Avoid a write storm when the user flips between tabs.
            return CharacterResponse.from_domain(character)
        updated = character.with_unread_proactive(0)
        await self._repository.save(updated)
        return CharacterResponse.from_domain(updated)

    async def mark_feed_replies_seen(
        self, character_id: str,
    ) -> CharacterResponse | None:
        """Reset the LumeGram unread-reply counter.

        Called as part of the ``POST /characters/{id}/feed/seen``
        pipeline — the user opening the overlay implies they've now
        seen any character replies that landed since last open. Same
        idempotent / no-op-when-zero pattern as
        :meth:`mark_web_conversation_read`."""
        character = await self._repository.get(character_id)
        if character is None:
            return None
        if character.unread_feed_reply_count == 0:
            return CharacterResponse.from_domain(character)
        updated = character.with_unread_feed_reply(0)
        await self._repository.save(updated)
        return CharacterResponse.from_domain(updated)

    async def increment_feed_reply_unread(
        self, character_id: str,
    ) -> int:
        """Increment the badge for a freshly-landed character reply.

        Returns the new count (0 when the character no longer exists,
        which the caller treats as a soft skip — the row is already
        persisted; only the badge is missed)."""
        character = await self._repository.get(character_id)
        if character is None:
            return 0
        next_count = character.unread_feed_reply_count + 1
        updated = character.with_unread_feed_reply(next_count)
        await self._repository.save(updated)
        return next_count

    async def update_character(
        self,
        character_id: str,
        payload: UpdateCharacterRequest,
        *,
        user_id: str | None = None,
    ) -> CharacterResponse | None:
        character = await self._repository.get(character_id)
        if character is None:
            return None
        if user_id is not None and character.user_id != user_id:
            return None

        await self._validate_arc_template_update(payload, character=character)
        await self._validate_arc_series_update(payload, character=character)
        new_state = payload_to_state(payload.state) if payload.state else None
        updated = character.update(
            name=payload.name,
            summary=payload.summary,
            personality=payload.personality,
            interests=payload.interests,
            speaking_style=payload.speaking_style,
            boundaries=payload.boundaries,
            aspirations=payload.aspirations,
            appearance=payload.appearance,
            gender_identity=payload.gender_identity,
            third_person_pronoun=payload.third_person_pronoun,
            visual_gender_presentation=payload.visual_gender_presentation,
            visual_subject_type=payload.visual_subject_type,
            visual_generation_style=payload.visual_generation_style,
            image_urls=tuple(payload.image_urls) if payload.image_urls is not None else None,
            allowed_tools=tuple(payload.allowed_tools) if payload.allowed_tools is not None else None,
            loras=tuple(
                CharacterLora(name=l.name, strength=l.strength)
                for l in payload.loras
            ) if payload.loras is not None else None,
            state=new_state,
            proactive_enabled=payload.proactive_enabled,
            proactive_daily_limit=payload.proactive_daily_limit,
            proactive_cooldown_minutes=payload.proactive_cooldown_minutes,
            feed_daily_limit=payload.feed_daily_limit,
            world_awareness_enabled=payload.world_awareness_enabled,
            world_topics=(
                tuple(payload.world_topics) if payload.world_topics is not None else None
            ),
            subscribed_categories=(
                tuple(payload.subscribed_categories)
                if payload.subscribed_categories is not None else None
            ),
            excluded_topics=(
                tuple(payload.excluded_topics)
                if payload.excluded_topics is not None else None
            ),
            world_frame=payload.world_frame,
            accepts_web_proactive=payload.accepts_web_proactive,
            feature_models=(
                _payload_feature_models(payload)
                if payload.feature_models is not None else None
            ),
            feature_image_profiles=(
                _payload_feature_image_profiles(payload)
                if payload.feature_image_profiles is not None else None
            ),
            feature_video_profiles=(
                _payload_feature_video_profiles(payload)
                if payload.feature_video_profiles is not None else None
            ),
            companions=(
                _payload_companions(payload)
                if payload.companions is not None else None
            ),
            disposition=(
                payload.disposition.to_domain()
                if payload.disposition is not None else None
            ),
            body_state=(
                payload.body_state.to_domain()
                if payload.body_state is not None else None
            ),
            operator_pace_preference=payload.operator_pace_preference,
            personality_type=(
                payload.personality_type.to_domain()
                if payload.personality_type is not None else None
            ),
            **_arc_template_update_kwarg(payload),
            **_arc_series_update_kwarg(payload),
            **_dob_update_kwarg(payload),
        )
        # ``voice_profile`` lives outside the giant update() signature
        # because its tri-state (omit / null / payload) is cleaner to
        # express via the dedicated with_voice_profile() helper.
        if "voice_profile" in payload.model_fields_set:
            if payload.voice_profile is None:
                updated = updated.with_voice_profile(None)
            else:
                updated = updated.with_voice_profile(
                    payload.voice_profile.to_domain(),
                )
        if new_state is not None and self._state_tracker is not None:
            await self._state_tracker.record(
                character_id=character.id,
                source=SOURCE_MANUAL,
                before=character.state,
                after=new_state,
            )
        await self._repository.save(updated)
        return CharacterResponse.from_domain(updated)

    async def _validate_arc_series_update(
        self,
        payload: UpdateCharacterRequest,
        *,
        character: Character,
    ) -> None:
        if "arc_series_id" not in payload.model_fields_set:
            return
        if payload.arc_series_id is None:
            return
        if self._arc_series_repository is None:
            return
        await self._validate_arc_series_binding(
            payload.arc_series_id, character=character,
        )

    async def _validate_arc_template_update(
        self,
        payload: UpdateCharacterRequest,
        *,
        character: Character,
    ) -> None:
        if "arc_template_id" not in payload.model_fields_set:
            return
        await self._validate_arc_template_binding(
            payload.arc_template_id, character=character,
        )

    async def _validate_arc_template_binding(
        self,
        template_id: str | None,
        *,
        character: Character,
    ) -> None:
        if template_id is None:
            return
        if self._arc_template_repository is None:
            return
        template = await self._arc_template_repository.get_for_user(
            template_id,
            user_id=character.user_id,
        )
        if template is None:
            raise CharacterValidationError(
                f"Arc template {template_id!r} is not visible to this user",
            )
        if not template.is_applicable_to(character.id):
            raise CharacterValidationError(
                f"Arc template {template_id!r} is not applicable to this character",
            )

    async def _validate_arc_series_binding(
        self,
        series_id: str | None,
        *,
        character: Character,
    ) -> None:
        if series_id is None:
            return
        if self._arc_series_repository is None:
            return
        series = await self._arc_series_repository.get_for_user(
            series_id,
            user_id=character.user_id,
        )
        if series is None:
            raise CharacterValidationError(
                f"Arc series {series_id!r} is not visible to this user",
            )
