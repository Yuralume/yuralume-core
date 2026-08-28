"""Proactive messaging dispatcher.

Coordinates a single evaluation pass for one character:

1. Short-circuit if the character opted out.
2. Collect cheap signals (idle time, sent-today count, last attempt
   time, current activity) and ask the gate.
3. Find an eligible binding (``accepts_proactive``). Without one there
   is nowhere to push, so we log and return.
4. Hand a ``ProactiveContext`` to the decider (LLM or stub). If it
   says no, log and return.
5. Run whatever tools the decision asked for, then put the composed text
   past the outbound honesty gate (HV2). A message that claims a
   completed external action the tools did not perform is withheld —
   see :meth:`ProactiveDispatcher._resolve_outbound_honesty` for why
   this surface needs the gate more than the promise loop does.
6. Append the generated message to the binding's conversation as an
   ``assistant`` turn and push to the platform. Failures are logged
   as ``ERRORED`` attempts so the operator can see them in the UI.

Every exit path writes a ``ProactiveAttempt`` — operators need the log
to debug "why didn't the character message me?" or "why did it again".
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import date as date_type, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING
from uuid import uuid4

from kokoro_link.contracts.calendar_context import CalendarContextPort
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.weather_context import WeatherContextPort
from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
from kokoro_link.contracts.emotion import EmotionEventRepositoryPort
from kokoro_link.contracts.goal_repository import GoalRepositoryPort
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.novelty_gate import (
    ALL_AXES,
    NoveltyGateContext,
    NoveltyGatePort,
    NoveltyVerdict,
)
from kokoro_link.contracts.observability import (
    TurnRecorderPort,
    TurnRecordingDraft,
)
from kokoro_link.contracts.messaging import (
    ChannelAdapterPort,
    ChannelBindingRepositoryPort,
    MessagingAccountRepositoryPort,
    OutboundAttachment,
)
from kokoro_link.contracts.prompt import (
    PromptToolDescriptor,
    ToolOutcomeMessage,
)
from kokoro_link.contracts.register_profile import (
    RegisterProfileContext,
    RegisterProfilePort,
)
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.contracts.tool import ToolRegistryPort
from kokoro_link.contracts.character_event_mention import (
    CharacterEventMentionRepositoryPort,
)
from kokoro_link.domain.entities.character_event_mention import (
    CharacterEventMention,
)
from kokoro_link.contracts.proactive import (
    GateVerdict,
    ProactiveAttemptRepositoryPort,
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
    ProactiveGatePort,
)
from kokoro_link.contracts.proactive_intention import (
    ProactiveIntentionDecision,
    ProactiveIntentionJudgePort,
)
from kokoro_link.contracts.repositories import (
    CharacterRepositoryPort,
    ConversationRepositoryPort,
)
from kokoro_link.application.services.output_quality import (
    OutputQualityOrchestrator,
    OutputQualityPolicy,
    OutputQualityReview,
    fired_axes,
)
from kokoro_link.application.services.proactive_event_bus import (
    ProactiveEvent,
    ProactiveEventBus,
)
from kokoro_link.application.services.persona_disclosure_gate import (
    persona_safe_for_account,
)
from kokoro_link.application.services.pre_message_proactive_budget import (
    PRE_MESSAGE_BUDGET_UNAVAILABLE_REASON,
    PRE_MESSAGE_PROACTIVE_CAP,
    evaluate_pre_message_proactive_budget,
    evaluate_pre_message_proactive_delay,
)
from kokoro_link.application.services.proactive_delivery.eligible_binding import (
    ResolvedProactiveSink,
    find_eligible_proactive_binding,
    list_eligible_proactive_bindings,
)
from kokoro_link.application.services.proactive_delivery.line_conversation_recorder import (  # noqa: E501
    HostedLineConversationRecorder,
)
from kokoro_link.application.services.proactive_delivery.local_adapter import (
    LocalDeliveryTarget,
    LocalMessagingProactiveDeliveryAdapter,
)
from kokoro_link.application.services.proactive_delivery.envelope_hash import (
    compute_envelope_hash,
)
from kokoro_link.contracts.external_proactive import (
    DeliveryAcceptance,
    ENVELOPE_KIND_PROACTIVE,
    ExternalProactiveDeliveryPort,
    ProactiveEnvelope,
    ProactiveSegment,
    envelope_to_payload,
)
from kokoro_link.contracts.external_proactive_ledger import (
    ExternalProactiveEventRepositoryPort,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.application.services.location_context import (
    calendar_region_from_operator,
    prompt_location_fact,
    weather_location_from_operator,
)
from kokoro_link.application.services.persona_curiosity_observability import (
    persona_curiosity_plan_summary,
)
from kokoro_link.application.services.outcome_claim_audit import (
    PARK_PROACTIVE_CORRECTION_OVERCLAIMED_AGAIN,
    PARK_PROACTIVE_CORRECTION_RAISED,
    PARK_PROACTIVE_CORRECTION_SILENT,
    PARK_PROACTIVE_JUDGE_UNAVAILABLE,
    PARK_PROACTIVE_OVERCLAIMED_AFTER_TOOLS,
    outcome_claim_audit_scope,
    outcome_claim_audit_summary,
)
from kokoro_link.application.services.outcome_claim_guard import (
    OutcomeClaimGuard,
)
from kokoro_link.application.services.tool_attachment_delivery import (
    to_outbound_attachments,
)
from kokoro_link.contracts.outcome_claim import OutcomeClaimEvidence
from kokoro_link.contracts.player_knowledge_disclosure import (
    DisclosureCandidate,
    PlayerKnowledgeDisclosureJudgePort,
)
from kokoro_link.application.services.memory_disclosure_service import (
    MemoryDisclosureService,
    select_private_candidates,
)
from kokoro_link.infrastructure.prompt.outcome_claim_honesty import (
    CORRECTION_ZERO_CALL,
    render_honesty_correction,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.conversation import (
    Message,
    MessageAttachment,
    MessageRole,
    SOURCE_WEB,
)
from kokoro_link.domain.entities.emotion_event import (
    CAUSE_PROACTIVE_ATTEMPT,
    EmotionEvent,
)
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.story_arc import (
    BEAT_PENDING,
    OPERATOR_POSITION_CENTRAL,
    StoryArc,
    StoryArcBeat,
)
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.value_objects.goal_status import GoalStatus
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)
from kokoro_link.domain.services.address_resolver import resolve_character_address
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.domain.value_objects.resolved_address import AddressProvenance
from kokoro_link.domain.value_objects.timezone import timezone_for_id
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_initial_relationship_seed_lines,
)
from kokoro_link.infrastructure.prompt.memory_lines import format_memory_line
from kokoro_link.infrastructure.dialogue.llm_summarizer import (
    render_dialogue_line,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    format_civil_days_ago_label,
)
from kokoro_link.infrastructure.prompt.temporal_evidence import (
    TemporalEvent,
    quoted_event,
    render_temporal_context_lines,
)

if TYPE_CHECKING:
    from kokoro_link.contracts.visible_slots import VisibleSlotPort
    from kokoro_link.application.services.deferred_intent_service import (
        DeferredIntentService,
    )
    from kokoro_link.contracts.operator_address_preference import (
        OperatorAddressPreferenceRepositoryPort,
    )
    from kokoro_link.contracts.player_persona_note import (
        PlayerPersonaNoteRepositoryPort,
    )
    from kokoro_link.application.services.event_seed_dispenser import (
        EventSeedDispenser,
    )
    from kokoro_link.application.services.rest_recovery_refresher import (
        RestRecoveryRefresher,
    )
    from kokoro_link.application.services.schedule_service import ScheduleService
    from kokoro_link.application.services.state_tracker import StateChangeTracker
    from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
    from kokoro_link.application.services.story_arc_service import StoryArcService
    from kokoro_link.application.services.story_event_service import StoryEventService
    from kokoro_link.application.services.persona_curiosity_service import (
        PersonaCuriosityService,
    )
    from kokoro_link.application.services.notification_service import (
        NotificationService,
    )
    from kokoro_link.contracts.persona_curiosity import (
        PersonaCuriosityPlan,
        PersonaCuriosityPlannerPort,
    )
    from kokoro_link.contracts.story_scene import (
        StorySceneSessionRepositoryPort,
    )

_LOGGER = logging.getLogger(__name__)

# Direction-B address resolutions worth surfacing in the proactive prompt:
# an explicit per-character seed name or an observed salutation. A bare
# character-name (or empty) resolution is dropped so the cold-start prompt
# stays quiet about an unobserved salutation.
_SEED_OR_OBSERVED_PROVENANCE = frozenset(
    {AddressProvenance.EXPLICIT_SEED, AddressProvenance.OBSERVED_PREFERENCE},
)

# How many of the character's own recent SENT pushes to surface. Tuned
# to span several days at the default daily-limit of 3 (≈ 2.5 days) so a
# "ignored for days" streak stays visible to the decider; the prompt
# itself only quotes the first few verbatim to bound length.
_RECENT_SENT_LIMIT = 8

#: How many raw turns ride along under the dialogue summary as the
#: deterministic time anchor. Three is enough to show the last exchange
#: plus its lead-in without turning the decider prompt back into a
#: transcript — the summary is still what carries the wider thread.
_FRESH_DIALOGUE_TAIL_TURNS = 3

# How many recalled memories reach the decider / composer prompt. Named
# because KB8 needs the *rendered* set, not the fetched one: the query
# limit and the render slice used to be two separate literal 6s, and a
# disclosure candidate list built from the wrong one would offer the
# judge memories the message was never written against.
_MEMORY_RECALL_LIMIT = 6

# The hosted proactive path records its assistant turn on the character's
# ``source="line"`` conversation — the same thread the inbound external-chat
# turn machine (DR-LH0-004) drives, so proactive + reply share one timeline.
_SOURCE_LINE = "line"

#: Counter / log label for this surface's output-quality outcomes. One
#: word, fixed: it is a Prometheus label value and the string an operator
#: greps the structured log for.
_QUALITY_SURFACE = "proactive"

#: The human-readable half of a withheld tick's audit row. The machine
#: half is ``ProactiveOutcome.QUALITY_WITHHELD`` — this string explains,
#: it does not classify, so nothing may branch on it. "The character chose
#: not to message" and "the gate withheld a broken message" are opposite
#: problems and the outcome is what keeps them apart.
_QUALITY_HARD_SKIP_REASON = "output quality gate withheld the message"


@dataclass(frozen=True, slots=True)
class _ClaimedEventSeed:
    """A world event this tick won the right to use, or the absence of one.

    ``item_id`` addresses the inbox row (release it if nothing is sent);
    ``world_event_id`` addresses the event itself (record the mention if
    something is). They are different identities and both are needed —
    the inbox row is per-character bookkeeping, the event is the shared
    pool row chat later reads the link out of."""

    title: str = ""
    summary: str = ""
    source: str = ""
    locale: str = ""
    item_id: str | None = None
    world_event_id: str | None = None


_NO_EVENT_SEED = _ClaimedEventSeed()


@dataclass(frozen=True, slots=True)
class _ProactiveToolRun:
    """What the decision's tool calls actually produced.

    ``attachments`` is what will *ship* — already through the delivery
    filter, so a render whose URL was dropped for want of a public base
    URL is absent here even though the GPU ran. ``outcomes`` is what
    *happened*, failures included. The honesty gate needs both and they
    disagree often enough to be worth two fields: 「照片傳給你了」 is a
    lie the player can check against the first, not the second.
    """

    attachments: tuple[OutboundAttachment, ...] = ()
    outcomes: tuple[ToolOutcomeMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class _HonestyResolution:
    """What the outbound honesty gate decided about this tick.

    ``decision is None`` is the withhold: nothing goes out. Otherwise the
    decision and the run are the ones that should ship — which may be the
    *corrected* pair rather than the ones handed in, so callers must use
    what comes back instead of what they passed.
    """

    decision: "ProactiveDecision | None" = None
    run: _ProactiveToolRun = field(default_factory=_ProactiveToolRun)


@dataclass(frozen=True, slots=True)
class _RecentDialogue:
    """What one dialogue load yielded for this tick.

    ``summary`` is the decider-facing block (LLM summary + verbatim tail).
    ``last_player_text`` is the player's own most recent line, kept
    separately because the quality gate's 時間座標 block needs the *quote*
    and not a summary of it: 「玩家最後一次說話」 dated sixteen hours ago
    tells the judge how stale the material is, but only the words tell it
    whether the concern expired with the gap (「我要回家了」) or did not
    (「下週要搬家」). Both come out of the same load, so the quote costs
    no extra query.

    ``last_player_at`` is **that line's own** ``created_at``, and it
    travels with the text as one unit for the reason the pairing was got
    wrong once already: the anchor used to quote this turn while dating
    it from ``idle_minutes``, which is derived from ``last_active_at`` —
    a field that in cloud mode is also advanced by 分歧劇場／起幕／融合
    故事, none of which write a ``USER`` message. A player who spent the
    week in the drama surface after saying 「我要回家了」 three days ago
    therefore had those words stamped 「約 5 分鐘前」, and the
    ``temporal_inconsistency`` axis was handed manufactured evidence that
    the concern was fresh — it *passed* 「回家了嗎？」 on the strength of
    it. Quote and instant come from the same message or neither is
    rendered.

    Empty text with a ``None`` instant is the honest absence — no
    summariser wired, no messages, everything filtered — and every
    consumer already treats it as "this block does not render".
    """

    summary: str = ""
    last_player_text: str = ""
    last_player_at: datetime | None = None


_NO_RECENT_DIALOGUE = _RecentDialogue()


@dataclass(frozen=True, slots=True)
class _QualityGateResolution:
    """What the output-quality band decided about this tick's draft.

    ``withheld`` is not derivable from ``decision`` and cannot be inferred
    from its ``reason`` string: a decision arriving with ``should_send=False``
    may equally be the *character's* own choice to stay quiet, and the two
    take different audit outcomes — only one of them is allowed to anchor
    the cooldown. Carrying the bit explicitly is what keeps the caller from
    sniffing prose to tell them apart.
    """

    decision: "ProactiveDecision"
    metadata: dict[str, object] = field(default_factory=dict)
    withheld: bool = False


class ProactiveDispatcher:
    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        conversation_repository: ConversationRepositoryPort,
        account_repository: MessagingAccountRepositoryPort,
        binding_repository: ChannelBindingRepositoryPort,
        attempt_repository: ProactiveAttemptRepositoryPort,
        gate: ProactiveGatePort,
        decider: ProactiveDeciderPort,
        adapters: dict[Platform, ChannelAdapterPort],
        intention_judge: ProactiveIntentionJudgePort | None = None,
        schedule_resolver: "ScheduleResolver | None" = None,
        memory_repository: MemoryRepositoryPort | None = None,
        disclosure_judge: "PlayerKnowledgeDisclosureJudgePort | None" = None,
        goal_repository: GoalRepositoryPort | None = None,
        story_event_service: "StoryEventService | None" = None,
        story_arc_service: "StoryArcService | None" = None,
        state_tracker: "StateChangeTracker | None" = None,
        rest_recovery_refresher: "RestRecoveryRefresher | None" = None,
        tool_registry: ToolRegistryPort | None = None,
        tool_orchestrator: "ToolOrchestrator | None" = None,
        event_bus: ProactiveEventBus | None = None,
        dialogue_summarizer: DialogueSummarizerPort | None = None,
        event_seed_dispenser: "EventSeedDispenser | None" = None,
        event_mention_repository: (
            "CharacterEventMentionRepositoryPort | None"
        ) = None,
        calendar_context_port: CalendarContextPort | None = None,
        weather_context_port: "WeatherContextPort | None" = None,
        schedule_service: "ScheduleService | None" = None,
        operator_persona_service=None,  # noqa: ANN001 - optional app service
        player_persona_note_repository: (
            "PlayerPersonaNoteRepositoryPort | None"
        ) = None,
        relationship_seed_repository: (
            CharacterOperatorRelationshipSeedRepositoryPort | None
        ) = None,
        persona_curiosity_service: "PersonaCuriosityService | None" = None,
        persona_curiosity_planner: "PersonaCuriosityPlannerPort | None" = None,
        operator_profile_service=None,  # noqa: ANN001 - optional, resolves primary_language
        public_base_url: str = "",
        public_base_url_provider: Callable[[], Awaitable[str]] | None = None,
        local_tz: tzinfo | None = None,
        turn_recorder: TurnRecorderPort | None = None,
        emotion_event_repository: EmotionEventRepositoryPort | None = None,
        deferred_intent_service: "DeferredIntentService | None" = None,
        address_preference_repository: "OperatorAddressPreferenceRepositoryPort | None" = None,
        clock: ClockPort | None = None,
        prompt_pack_hash_provider: Callable[[], str] | None = None,
        notification_service: "NotificationService | None" = None,
        register_profiler: RegisterProfilePort | None = None,
        register_profile_enabled: bool = False,
        reply_quality_gate: NoveltyGatePort | None = None,
        reply_quality_gate_enabled: bool = False,
        reply_quality_gate_max_retries: int = 1,
        # QG0 — the shared review→regenerate→dispose band, which this
        # surface runs its composed push through
        # (:meth:`_gate_proactive_decision`). ``None`` leaves the gate
        # inert: no review, no regeneration, and the decider's message
        # ships exactly as it did before QG.
        output_quality_orchestrator: OutputQualityOrchestrator | None = None,
        outcome_claim_guard: OutcomeClaimGuard | None = None,
        subscription_access_guard: SubscriptionAccessGuard | None = None,
        visible_slot_port: "VisibleSlotPort | None" = None,
        external_delivery: ExternalProactiveDeliveryPort | None = None,
        local_delivery: ExternalProactiveDeliveryPort | None = None,
        hosted_delivery: ExternalProactiveDeliveryPort | None = None,
        proactive_event_ledger: (
            ExternalProactiveEventRepositoryPort | None
        ) = None,
        proactive_envelope_ttl_seconds: float = 3600.0,
        hosted_identity_resolver: (
            Callable[[str], Awaitable["tuple[str, str] | None"]] | None
        ) = None,
        story_scene_sessions: "StorySceneSessionRepositoryPort | None" = None,
    ) -> None:
        self._characters = character_repository
        self._conversations = conversation_repository
        self._accounts = account_repository
        self._bindings = binding_repository
        self._attempts = attempt_repository
        self._gate = gate
        self._decider = decider
        self._intention_judge = intention_judge
        self._adapters = {p.value: a for p, a in adapters.items()}
        self._schedule_resolver = schedule_resolver
        self._memories = memory_repository
        # KB8 — the judge is optional (no judge ⇒ no proactive-side
        # disclosure, the pre-KB8 behaviour every existing test gets),
        # but the ledger writer is not injected: it needs only the
        # memory repository this dispatcher already holds, and making it
        # a second optional argument would let a deployment wire the
        # judge without a writer and lose every verdict in silence.
        self._disclosure_judge = disclosure_judge
        self._memory_disclosure = (
            MemoryDisclosureService(memories=memory_repository)
            if memory_repository is not None
            else None
        )
        self._goals = goal_repository
        self._story_event_service = story_event_service
        self._story_arc_service = story_arc_service
        self._state_tracker = state_tracker
        self._rest_recovery_refresher = rest_recovery_refresher
        self._tool_registry = tool_registry
        self._tool_orchestrator = tool_orchestrator
        # HV2. ``None`` = no gate, and every exit below then behaves
        # exactly as it did before — which is what a deployment with no
        # judge route, and every existing test, gets. Deliberately the
        # *same* guard instance the promise loop holds: the honesty rate
        # and the outage streak are one number about one deployment, and
        # a second guard would split both in half without saying so.
        self._outcome_claim_guard = outcome_claim_guard
        self._event_bus = event_bus
        self._dialogue_summarizer = dialogue_summarizer
        self._event_seed_dispenser = event_seed_dispenser
        self._event_mentions = event_mention_repository
        self._calendar_context_port = calendar_context_port
        self._weather_context_port = weather_context_port
        self._schedule_service = schedule_service
        self._operator_persona_service = operator_persona_service
        self._player_persona_note_repository = player_persona_note_repository
        self._relationship_seed_repository = relationship_seed_repository
        self._persona_curiosity_service = persona_curiosity_service
        self._persona_curiosity_planner = persona_curiosity_planner
        # FRONTEND_I18N_PLAN §使用者主要語言 — surface the character
        # owner's pinned content language to the decider so proactive
        # openers match chat language. Optional so legacy single-user
        # deploys without auth still wire cleanly; falls back to
        # "zh-TW" when missing.
        self._operator_profile_service = operator_profile_service
        self._public_base_url = public_base_url.rstrip("/")
        self._public_base_url_provider = public_base_url_provider
        # ``count_sent_today`` uses ``now.replace(hour=0, ...)`` to find
        # the start of "today". That means the tzinfo of whatever we
        # pass in is the day boundary — passing UTC would mean daily
        # limits reset at 08:00 local for GMT+8 operators, not midnight.
        # Pinning to the same local_tz that ScheduleService uses makes
        # "today" match what the operator expects.
        self._local_tz = local_tz or timezone.utc
        self._turn_recorder = turn_recorder
        self._prompt_pack_hash_provider = prompt_pack_hash_provider
        self._emotion_events = emotion_event_repository
        self._deferred_intents = deferred_intent_service
        self._address_preferences = address_preference_repository
        self._clock = clock
        self._notification_service = notification_service
        self._register_profiler = register_profiler
        self._register_profile_enabled = bool(register_profile_enabled)
        # RA: the *judging* runs through ``output_quality_orchestrator``,
        # which the container builds around this very object. Kept as the
        # "was a gate wired at all" answer — do not drop it from the
        # wiring as redundant, the gate goes inert without it.
        self._reply_quality_gate = reply_quality_gate
        self._reply_quality_gate_enabled = bool(reply_quality_gate_enabled)
        self._reply_quality_gate_max_retries = max(
            0,
            int(reply_quality_gate_max_retries),
        )
        self._output_quality_orchestrator = output_quality_orchestrator
        self._subscription_access_guard = subscription_access_guard
        self._visible_slot_port = visible_slot_port
        # §8.3 — only the *external* sink is routed through the port; the web /
        # history / SSE sink stays in this dispatcher. When no adapter is
        # injected we default to the self-host messaging adapter built from the
        # same repositories + platform adapters, so behaviour is byte-identical
        # to the former inline ``_deliver`` (existing tests wire no port).
        self._external_delivery: ExternalProactiveDeliveryPort = (
            external_delivery
            if external_delivery is not None
            else LocalMessagingProactiveDeliveryAdapter(
                account_repository=account_repository,
                binding_repository=binding_repository,
                conversation_repository=conversation_repository,
                adapters=adapters,
            )
        )
        # H4: the local-binding path and the hosted-channel path must NEVER
        # share one adapter — a hosted deployment that also has a self-host
        # binding would otherwise feed the local binding's identity into the
        # hosted adapter. Each path resolves to its dedicated port; both default
        # to ``external_delivery`` so single-adapter (self-host / legacy test)
        # wiring is byte-identical.
        self._local_delivery: ExternalProactiveDeliveryPort = (
            local_delivery if local_delivery is not None else self._external_delivery
        )
        self._hosted_delivery: ExternalProactiveDeliveryPort = (
            hosted_delivery
            if hosted_delivery is not None
            else self._external_delivery
        )
        # DR-LH0-005 pre-send ledger. Optional: unset (self-host default) skips
        # the durable record entirely and delivers exactly as before; when wired
        # the immutable envelope is durably saved BEFORE ``accept`` so a lost
        # response re-sends the same event without re-running judge / decider.
        self._proactive_event_ledger = proactive_event_ledger
        self._proactive_envelope_ttl = timedelta(
            seconds=max(1.0, proactive_envelope_ttl_seconds),
        )
        # LH4 Core-C — hosted routing. When set (hosted mode), a character with
        # no local messaging binding can still route its proactive push to the
        # Cloud Channel: the resolver reverse-maps the character to its owner's
        # cloud ``(tenant_id, account_id)`` projection. ``None`` (self-host /
        # local) keeps the external gate single-path — behaviour byte-identical
        # to before, so no existing proactive test changes.
        self._hosted_identity_resolver = hosted_identity_resolver
        # SC1-E — while a character is inside a 起幕 scene, it does not also
        # message the player from outside it. Read
        # straight from the session repository rather than through the scene
        # service: this is one indexed lookup in the cheap-gate band, and
        # depending on the whole scene runtime here would make the proactive
        # path import the opener, the waterfall and the quota guard.
        self._story_scene_sessions = story_scene_sessions
        # M8: shared idempotent line-history recorder — the dispatcher records
        # right after acceptance; the retry worker re-records accepted rows whose
        # append never landed. One implementation keeps them in step.
        self._line_conversation_recorder = HostedLineConversationRecorder(
            conversation_repository=conversation_repository,
        )

    async def evaluate(
        self,
        *,
        character_id: str,
        trigger: ProactiveTrigger,
        now: datetime | None = None,
        logical_slot: str | None = None,
    ) -> ProactiveAttempt:
        when = self._resolve_now(now)
        character = await self._characters.get(character_id)
        if character is None:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.ERRORED,
                reason="character not found",
                now=when,
            )
        if (
            self._subscription_access_guard is not None
            and not await self._subscription_access_guard.is_character_allowed(
                character,
            )
        ):
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.DISABLED,
                reason="subscription is inactive",
                now=when,
            )

        if not character.proactive_enabled:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.DISABLED,
                reason="proactive_enabled is False",
                now=when,
            )

        # SC1-E — paused, not cancelled. A character playing a scene must
        # not also send a message from outside it (§3.2); the moment the
        # scene closes the very next tick evaluates exactly as before,
        # because nothing about this gate is remembered anywhere. Sits in
        # the cheap band (one indexed read) so a paused character costs no
        # schedule resolution and no model call, and it is GATE_BLOCKED
        # rather than DISABLED so the cooldown anchor — which only advances
        # on attempts that passed the gate — is untouched by the pause.
        if await self._is_playing_a_scene(character_id):
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.GATE_BLOCKED,
                reason="story scene in progress",
                now=when,
            )

        relationship_seed = await self._load_relationship_seed(character)
        if (
            _requires_user_started_interaction(trigger)
            and not await self._has_user_started_interaction(character)
        ):
            if not _seed_allows_pre_message_proactive(relationship_seed):
                return await self._log(
                    character_id=character_id,
                    trigger=trigger,
                    outcome=ProactiveOutcome.GATE_BLOCKED,
                    reason="waiting for first user message",
                    now=when,
                )
            # TR2-B — "you may find me first" is now the creation form's
            # default rather than a deliberate tick, so the deliberate
            # part is carried here instead: nothing goes out while the
            # character is still hours old. Cheapest gate in the branch
            # (a field already on the loaded entity, no repository read),
            # so it runs before the budget lookup. Only a lower bound —
            # once the window lapses, *whether and when* to speak is
            # still the decider's semantic call under quiet hours.
            pre_message_delay = evaluate_pre_message_proactive_delay(
                character.created_at, now=when,
            )
            if not pre_message_delay.passed:
                return await self._log(
                    character_id=character_id,
                    trigger=trigger,
                    outcome=ProactiveOutcome.GATE_BLOCKED,
                    reason=pre_message_delay.reason,
                    now=when,
                )
            # TR2-A — the seed said "you may find me first", not "you may
            # keep finding me forever". Everything below that normally
            # restrains a chatty character reads a conversation that does
            # not exist yet: the unanswered-streak signal is hard-wired to
            # 0 while ``idle_minutes`` is None, and cooldown / daily limit
            # are per-day rhythms, not a total. So the pre-message window
            # gets its own ceiling here, in the cheap band, before rest
            # recovery and before any model call. It stops applying the
            # moment the player speaks — from then on the normal cadence
            # has real signal to work with.
            pre_message_budget = await self._check_pre_message_budget(
                character_id=character_id, now=when,
            )
            if not pre_message_budget.passed:
                return await self._log(
                    character_id=character_id,
                    trigger=trigger,
                    outcome=ProactiveOutcome.GATE_BLOCKED,
                    reason=pre_message_budget.reason,
                    now=when,
                )

        # Rest recovery is lazy in the chat path — without this the
        # scheduler would keep seeing stale fatigue/energy and the gate
        # would block proactive sends indefinitely for any character
        # who went to bed exhausted and hasn't chatted since. We apply
        # the same exponential decay here, persist when it changes, and
        # record a REST_RECOVERY snapshot so the state-history UI shows
        # why energy crept up without a user turn.
        character = await self._apply_rest_recovery(character, when)

        # Cooldown is anchored on "last time we actually spent LLM budget",
        # not any attempt — otherwise every gate-blocked tick would
        # reset the cooldown and it would never lapse in practice.
        last_passing = (
            await self._attempts.latest_passing_gate_for_character(character_id)
        )
        last_attempt = await self._attempts.latest_for_character(character_id)
        operator = await self._load_operator_profile(character)
        operator_tz = _timezone_for_operator(operator, self._local_tz)
        local_now = when.astimezone(operator_tz)
        initial_relationship_lines = render_initial_relationship_seed_lines(
            relationship_seed,
        )
        sent_today = await self._attempts.count_sent_today(
            character_id, now=local_now,
        )
        idle_minutes = _compute_idle_minutes(character, when)
        current_activity, upcoming, schedule, just_finished_activity = (
            await self._resolve_schedule(character, when)
        )

        # T2 — a parked motive can carry its own appointment ("we agreed
        # on 19:30"). An INTENTION_SKIPPED tick still advances the
        # cooldown anchor, so a 19:22 "wait for the agreed time" verdict
        # would otherwise blank the 19:30 window it was waiting for.
        # One indexed read on the cheap side of the gate; empty for every
        # character with no alarm pending, which is the normal case.
        due_intents = await self._load_due_deferred_intents(
            character_id=character.id, when=when,
        )
        verdict = await self._gate.check(
            character=character,
            trigger=trigger,
            now=when,
            sent_today=sent_today,
            last_attempt_at=(
                last_passing.decided_at if last_passing else None
            ),
            idle_minutes=idle_minutes,
            current_activity=current_activity,
            local_tz=operator_tz,
            cooldown_exempt=bool(due_intents),
        )
        if not verdict.passed:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.GATE_BLOCKED,
                reason=verdict.reason,
                now=when,
            )

        # The alarm is spent the instant it buys a pass — win or lose
        # downstream. Leaving it set would exempt every subsequent tick
        # from the cooldown and burn one judge call each time. The motive
        # itself stays parked and still reaches the judge below; if the
        # judge skips again it writes a fresh row, possibly with a new
        # appointment, which is the intended behaviour.
        await self._spend_due_revisits(due_intents)

        # P3-Dedup §3.4 — claim the tick slot AFTER the gate passes and BEFORE
        # composing / delivering. A distributed reclaimed job racing the
        # original on the same character loses the claim here and returns
        # without any provider call, so the visible send happens at most once.
        # ``logical_slot=None`` (manual / API evaluate) skips the claim entirely
        # — the unconstrained current behaviour. The claim NEVER raises into the
        # tick: a slot-store failure fails open (send proceeds).
        if not await self._claim_visible_slot(character_id, logical_slot):
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.SLOT_TAKEN,
                reason=f"visible slot already claimed: {logical_slot}",
                now=when,
            )

        eligible = await self._find_eligible_binding(character_id)
        web_enabled = bool(character.accepts_web_proactive)
        # LH4 Core-C hosted dual-path. With no local binding, a hosted
        # deployment can still route the push to the Cloud Channel. Resolving
        # the cloud identity + cost-preflight sits HERE — the position
        # equivalent to the NO_BINDING gate — so a cloud-unmapped or
        # channel-ineligible character is skipped BEFORE the decider spends
        # any LLM budget (cheap-gate order preserved). ``None`` when self-host
        # (no resolver), no cloud mapping, or the channel is not eligible; all
        # three collapse to the same "nowhere hosted to push" semantics.
        hosted_target: tuple[str, str] | None = None
        if eligible is None:
            hosted_target = await self._resolve_hosted_target(character_id)
        if eligible is None and hosted_target is None and not web_enabled:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.NO_BINDING,
                reason=(
                    "no binding has accepts_proactive=True and "
                    "accepts_web_proactive is False"
                ),
                now=when,
            )
        binding, account = eligible if eligible else (None, None)

        recent_memories_text, disclosure_candidates = (
            await self._load_recent_memories(character_id, when)
        )
        active_goals_text = await self._load_active_goals_text(
            character_id, when, operator_tz,
        )
        available_tools = self._describe_tools(character)
        story_events = await self._load_story_events(character, when)
        recent_sent_attempts = await self._load_recent_sent_attempts(character_id)
        recent_dialogue = await self._summarize_recent_dialogue(
            character, now=when, local_tz=operator_tz,
        )
        recent_dialogue_summary = recent_dialogue.summary
        persona_curiosity_plan = await self._load_persona_curiosity_plan(
            character=character,
            operator=operator,
            recent_dialogue_summary=recent_dialogue_summary,
            initial_relationship_lines=initial_relationship_lines,
            now=when,
        )
        persona_curiosity_metadata = persona_curiosity_plan_summary(
            persona_curiosity_plan,
            surface="proactive",
        )
        active_arc, upcoming_beats, beat_awaiting_player = (
            await self._ensure_active_arc(character, when, operator_tz)
        )
        seed = await self._claim_event_seed(character, when)
        seed_item_id = seed.item_id

        calendar_context = self._describe_calendar(
            when, operator_tz, operator=operator,
        )
        weather_context = await self._describe_weather(when, operator=operator)
        upcoming_day_schedules = await self._load_upcoming_day_schedules(
            character.id, when, operator_tz,
        )
        operator_persona_lines = await self._load_operator_persona_lines(
            character,
        )
        # ``account`` is the external sink resolved above — the gate has
        # to see it, so this load cannot move earlier than the binding.
        player_persona_note = await self._load_player_persona_note(
            character, account,
        )
        # HUMANIZATION_ROADMAP §3.4 — surface still-active deferred motives
        # so the intention judge can re-evaluate timing on a re-tick. The
        # alarms that bought this tick were already spent above, so the
        # re-read rows come back alarm-less: overlay the pre-spend
        # snapshots or the judge loses the one fact that distinguishes
        # this tick from the one it skipped on (T2 / F2-1).
        deferred_intents = _overlay_due_intents(
            await self._load_active_deferred_intents(
                character_id=character.id, when=when,
            ),
            due_intents,
        )
        # HUMANIZATION_ROADMAP §4.2 — observed register / address preference.
        address_preference = await self._load_address_preference(
            character_id=character.id,
        )
        # Resolve how the player addresses this character (seed > observed)
        # so an explicit per-character seed name leads the proactive prompt.
        # A bare character-name fallback is suppressed so the cold-start
        # prompt stays quiet about an unobserved salutation.
        resolved_character = resolve_character_address(
            seed=relationship_seed,
            preference=address_preference,
            character=character,
        )
        resolved_character_salutation = (
            resolved_character.primary
            if resolved_character.provenance in _SEED_OR_OBSERVED_PROVENANCE
            else None
        )
        operator_primary_language = _language_for_operator(operator)
        operator_location_context = prompt_location_fact(operator)
        unanswered_streak = _count_unanswered_streak(
            recent_sent_attempts, idle_minutes=idle_minutes, now=when,
        )
        context = ProactiveContext(
            character=character,
            trigger=trigger,
            now=when,
            current_activity=current_activity,
            upcoming_activities=list(upcoming),
            schedule=schedule,
            just_finished_activity=just_finished_activity,
            idle_minutes=idle_minutes,
            sent_today=sent_today,
            last_proactive_at=(
                last_passing.decided_at if last_passing
                else (last_attempt.decided_at if last_attempt else None)
            ),
            recent_memories_text=recent_memories_text,
            active_goals_text=active_goals_text,
            available_tools=available_tools,
            story_events=story_events,
            recent_dialogue_summary=recent_dialogue_summary,
            active_arc=active_arc,
            upcoming_beats=upcoming_beats,
            beat_awaiting_player=beat_awaiting_player,
            recent_sent_attempts=recent_sent_attempts,
            unanswered_streak=unanswered_streak,
            world_event_seed_title=seed.title,
            world_event_seed_summary=seed.summary,
            world_event_seed_source=seed.source,
            world_event_seed_locale=seed.locale,
            operator_location_context=operator_location_context,
            calendar_context=calendar_context,
            weather_context=weather_context,
            upcoming_day_schedules=tuple(upcoming_day_schedules),
            operator_persona_lines=tuple(operator_persona_lines),
            player_persona_note=player_persona_note,
            initial_relationship_lines=tuple(initial_relationship_lines),
            persona_curiosity_plan=persona_curiosity_plan,
            deferred_intents=deferred_intents,
            address_preference=address_preference,
            resolved_character_salutation=resolved_character_salutation,
            operator_primary_language=operator_primary_language,
            local_tz=operator_tz,
        )
        if self._intention_judge is not None:
            try:
                intention = await self._intention_judge.judge(context)
            except Exception:
                _LOGGER.exception("proactive intention judge crashed")
                await self._restore_due_revisits(due_intents)
                await self._release_event_seed(character_id, seed_item_id)
                return await self._log(
                    character_id=character_id,
                    trigger=trigger,
                    outcome=ProactiveOutcome.ERRORED,
                    reason="intention judge raised",
                    metadata={"persona_curiosity": persona_curiosity_metadata},
                    now=when,
                )
            if not intention.should_consume_slot:
                # F2-3 — a fail-soft skip is not a decision: no motive is
                # recorded on that path either, so without the restore
                # the appointment simply vanishes. A judge that looked
                # and still held back keeps its alarm spent — that skip
                # is the character's own call.
                if intention.judge_unavailable:
                    await self._restore_due_revisits(due_intents)
                # HUMANIZATION_ROADMAP §3.4 — park the motive so the
                # next tick can re-evaluate timing instead of forgetting
                # an authentic urge after one bad moment.
                await self._record_deferred_intent(
                    character_id=character_id,
                    trigger=trigger,
                    decision=intention,
                    local_tz=operator_tz,
                    now=when,
                )
                await self._release_event_seed(character_id, seed_item_id)
                return await self._log(
                    character_id=character_id,
                    trigger=trigger,
                    outcome=ProactiveOutcome.INTENTION_SKIPPED,
                    reason=_format_intention_skip_reason(intention),
                    metadata={"persona_curiosity": persona_curiosity_metadata},
                    now=when,
                )
        try:
            decision = await self._decider.decide(context)
        except Exception:
            _LOGGER.exception("proactive decider crashed")
            await self._release_event_seed(character_id, seed_item_id)
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.ERRORED,
                reason="decider raised",
                metadata={"persona_curiosity": persona_curiosity_metadata},
                now=when,
            )

        quality_metadata: dict[str, object] = {}
        quality_withheld = False
        if decision.should_send and decision.message:
            gated = await self._gate_proactive_decision(
                context=context,
                decision=decision,
                character=character,
                recent_dialogue=recent_dialogue,
            )
            decision = gated.decision
            quality_metadata = gated.metadata
            quality_withheld = gated.withheld

        if not decision.should_send or not decision.message:
            await self._release_event_seed(character_id, seed_item_id)
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                # Two very different silences share this exit. The
                # character choosing not to speak is a decision the
                # cooldown should honour; a quality gate refusing the
                # prose it wrote is not, and tagging both
                # ``DECIDER_SKIPPED`` let one broken draft mute the whole
                # window (the 2026-08-26 defect).
                outcome=(
                    ProactiveOutcome.QUALITY_WITHHELD if quality_withheld
                    else ProactiveOutcome.DECIDER_SKIPPED
                ),
                reason=decision.reason,
                metadata={
                    "persona_curiosity": persona_curiosity_metadata,
                    **quality_metadata,
                },
                now=when,
            )

        # Run any tool calls the decider asked for *before* pushing
        # the outbound. Attachments from each successful call get
        # merged into the outbound payload; failures are silently
        # dropped (the text message still goes out on its own so the
        # user isn't left with an empty push waiting on a broken
        # ComfyUI). Audit rows are written by the orchestrator.
        # Tool invocations are audited against the conversation they belong to.
        # Local path → the binding's conversation id; hosted path → the
        # character's ``source="line"`` conversation (the same thread the
        # proactive turn lands on below). ``None`` when neither exists yet,
        # matching the local behaviour for an unbound conversation.
        if binding is not None:
            tool_conversation_id = binding.conversation_id
        elif hosted_target is not None:
            tool_conversation_id = await self._latest_line_conversation_id(
                character.id,
            )
        else:
            tool_conversation_id = None
        run = await self._execute_decision_tools(
            character=character,
            decision=decision,
            conversation_id=tool_conversation_id,
        )
        # HV2 — the last gate before anything reaches a player. The proactive
        # decider writes its message in the *same* JSON that requests the
        # tool, so the prose is composed before the tool has run and cannot
        # know whether it worked: 「拍了張照片給你」 is true only if the render
        # succeeded *and* the file survived the delivery filter. That makes
        # the mismatch shape structural here rather than accidental.
        #
        # Scoped so this tick's verdicts land in HV3's per-round trail
        # rather than only in the process-wide counters. Without the
        # scope the calls below are still made and still counted — they
        # just leave no row, so a 謊稱率 computed from the audit table
        # would silently exclude the highest-volume outbound surface.
        with outcome_claim_audit_scope():
            reviewed = await self._resolve_outbound_honesty(
                character=character,
                context=context,
                decision=decision,
                run=run,
                conversation_id=tool_conversation_id,
            )
            honesty_audit = outcome_claim_audit_summary()
        # ``None`` when the gate never reached the judge (no guard
        # wired), so an ungated deployment's attempt metadata stays
        # exactly the shape it was.
        honesty_metadata: dict[str, object] = (
            {"outcome_claim": honesty_audit} if honesty_audit else {}
        )
        if reviewed.decision is None:
            await self._release_event_seed(character_id, seed_item_id)
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.DECIDER_SKIPPED,
                reason="outbound honesty gate withheld the message",
                metadata={
                    "persona_curiosity": persona_curiosity_metadata,
                    **quality_metadata,
                    **honesty_metadata,
                },
                now=when,
            )
        decision = reviewed.decision
        attachments = reviewed.run.attachments

        # Fan out: web (if opted in) + messaging binding (if any).
        # A failure on one target must not block the other — e.g. a
        # dead Telegram bot token shouldn't swallow the web badge
        # update the user is actually watching for.
        delivered = 0
        web_delivered = False
        external_delivered = False
        binding_id_for_log: str | None = None
        if web_enabled:
            try:
                await self._deliver_web(
                    character=character,
                    text=decision.message,
                    attachments=attachments,
                    when=when,
                )
                delivered += 1
                web_delivered = True
            except Exception:
                _LOGGER.exception("proactive web delivery crashed")
        if binding is not None and account is not None:
            try:
                acceptance = await self._deliver_external(
                    character=character,
                    binding=binding,
                    account=account,
                    text=decision.message,
                    attachments=attachments,
                    locale=operator_primary_language,
                    when=when,
                )
                if acceptance.delivered:
                    delivered += 1
                    external_delivered = True
                    binding_id_for_log = binding.id
                else:
                    _LOGGER.warning(
                        "proactive external delivery not accepted "
                        "character=%s reason=%s",
                        character_id, acceptance.reason,
                    )
            except Exception:
                _LOGGER.exception("proactive messaging delivery crashed")
        elif hosted_target is not None:
            try:
                acceptance = await self._deliver_hosted(
                    character=character,
                    tenant_id=hosted_target[0],
                    account_id=hosted_target[1],
                    text=decision.message,
                    attachments=attachments,
                    locale=operator_primary_language,
                    when=when,
                )
                if acceptance.delivered:
                    delivered += 1
                    external_delivered = True
                    # The Hosted delivery id is an opaque Channel receipt,
                    # not a Core channel_bindings.id foreign key.
                    # binding_id_for_log intentionally remains None.
                else:
                    _LOGGER.warning(
                        "proactive hosted delivery not accepted "
                        "character=%s reason=%s",
                        character_id, acceptance.reason,
                    )
            except Exception:
                _LOGGER.exception("proactive hosted delivery crashed")

        if delivered == 0:
            await self._release_event_seed(character_id, seed_item_id)
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.ERRORED,
                reason="delivery raised",
                binding_id=(binding.id if binding else None),
                message=decision.message,
                metadata={
                    "persona_curiosity": persona_curiosity_metadata,
                    **quality_metadata,
                    **honesty_metadata,
                },
                now=when,
            )

        # HUMANIZATION_ROADMAP §3.4 — a successful send folds the
        # character's pending motives into reality; mark them consumed
        # so they stop re-surfacing in subsequent judge calls.
        await self._consume_deferred_intents(context.deferred_intents, now=when)
        # Something reached the player, so the seed's claim is now
        # permanent: this is the last moment the event is still
        # addressable from anywhere the player can see. Record it, or
        # chat loses the material the moment the push lands.
        await self._record_event_mention(
            character_id=character_id, seed=seed, when=when,
        )
        await self._notify_web_push(
            character=character,
            message=decision.message,
            web_delivered=web_delivered,
            external_delivered=external_delivered,
        )
        # KB8 — only now, and only on this branch. Everything above this
        # line can still end the tick without the player having read a
        # word; here at least one channel has accepted the message, so
        # asking "what did it tell him" is a question about something
        # that happened.
        await self._flip_disclosed_memories(
            character=character,
            message_text=decision.message,
            candidates=disclosure_candidates,
        )

        return await self._log(
            character_id=character_id,
            trigger=trigger,
            outcome=ProactiveOutcome.SENT,
            reason=decision.reason,
            binding_id=binding_id_for_log,
            message=decision.message,
            metadata={
                "persona_curiosity": persona_curiosity_metadata,
                **quality_metadata,
                **honesty_metadata,
            },
            # Only the send path carries the prompt into the turn record:
            # skip / gate-blocked ticks fire every few minutes and would
            # otherwise flood the table with prompts for messages that
            # never existed (
            # §2 P0). ``None`` from a decider that doesn't assemble a
            # prompt degrades to the previous empty-string behaviour.
            prompt_assembled=decision.prompt_assembled or "",
            now=when,
        )

    async def _gate_proactive_decision(
        self,
        *,
        context: ProactiveContext,
        decision: ProactiveDecision,
        character: Character,
        recent_dialogue: _RecentDialogue = _NO_RECENT_DIALOGUE,
    ) -> _QualityGateResolution:
        """RA — run the composed push through the shared QG band (D7).

        This surface used to *ask* for a verdict and then throw it away:
        the retry fired on any failure, and whatever came back was kept
        the moment it was non-empty, so a second draft that still leaked
        a schema tag or answered in the wrong language shipped exactly
        like the first. Background policy fixes both halves — the
        regenerated draft is re-reviewed, and a hard defect that survives
        that review ends the tick.

        "End the tick" is spelled ``should_send=False`` rather than a new
        refusal path: the caller already releases the event seed on that
        branch, and nothing on it counts towards the daily quota (only
        ``SENT`` rows do). The audit row is **not** the decider's, though
        — ``withheld=True`` sends it out as ``QUALITY_WITHHELD``, which is
        the one skip outcome the cooldown does not anchor on. A machine
        refusing broken prose must not also cost the character its next
        window; that is the difference between skipping a tick and going
        quiet for an hour.
        """
        orchestrator = self._output_quality_orchestrator
        if (
            not self._reply_quality_gate_enabled
            or orchestrator is None
            or orchestrator.gate is None
            # The same judge instance the container hands the band; asked
            # about here too, exactly as the encounter surface does, so
            # "this deployment wired no gate" is one answer rather than
            # two that can disagree.
            or self._reply_quality_gate is None
        ):
            return _QualityGateResolution(decision=decision)
        # Both cost a model call / query, so they are gathered only once
        # the gate is known to be live — and hoisted out of ``context_for``
        # so the re-review reuses them instead of profiling twice.
        profile = await self._profile_proactive_register(context, character)
        diversity = _proactive_diversity_evidence(context)

        def context_for(draft: ProactiveDecision) -> NoveltyGateContext:
            return self._proactive_gate_context(
                context=context,
                decision=draft,
                character=character,
                register_profile=profile,
                diversity_evidence=diversity,
                recent_dialogue=recent_dialogue,
            )

        async def regenerate(feedback: str) -> ProactiveDecision | None:
            retry_context = replace(
                context,
                recent_dialogue_summary=(
                    f"{context.recent_dialogue_summary}\n"
                    f"上一輪主動訊息品質問題：{feedback}"
                ).strip(),
            )
            try:
                retry_decision = await self._decider.decide(retry_context)
            except Exception:
                _LOGGER.exception("proactive quality retry decider crashed")
                return None
            if not retry_decision.should_send or not retry_decision.message:
                # Shown the verdict, the decider chose silence. That is not
                # a second draft, so ``None``: the band must fall back to
                # its disposal table rather than treat "nothing" as a fix.
                return None
            return retry_decision

        review = await orchestrator.review(
            decision,
            surface=_QUALITY_SURFACE,
            context_for=context_for,
            regenerate=regenerate,
            policy=OutputQualityPolicy.BACKGROUND_FAIL_CLOSED,
            character=character,
            max_retries=self._reply_quality_gate_max_retries,
            enabled=self._reply_quality_gate_enabled,
        )
        verdict = _reportable_verdict(review)
        metadata: dict[str, object] = {
            "reply_quality_gate": _quality_gate_metadata(
                verdict,
                enabled=True,
                retry_count=1 if review.regen_attempted else 0,
                outcome=review.outcome,
            ),
            "register_profile": _register_profile_metadata(
                profile,
                enabled=self._register_profile_enabled,
            ),
            "diversity": _diversity_metadata(diversity),
        }
        if review.final is None:
            _LOGGER.warning(
                "proactive: hard output-quality failure survived regeneration "
                "character=%s axes=%s feedback=%s — nothing sent this tick",
                character.id,
                ",".join(fired_axes(verdict)) or "-",
                (verdict.feedback if verdict else "") or "-",
            )
            return _QualityGateResolution(
                decision=replace(
                    decision,
                    should_send=False,
                    # Cleared as well as switched off: the defective prose
                    # is what the gate refused, and leaving it on the
                    # returned decision is one careless read away from
                    # being delivered anyway.
                    message=None,
                    reason=_QUALITY_HARD_SKIP_REASON,
                ),
                metadata=metadata,
                withheld=True,
            )
        return _QualityGateResolution(decision=review.final, metadata=metadata)

    async def _profile_proactive_register(
        self,
        context: ProactiveContext,
        character: Character,
    ):
        if (
            not self._register_profile_enabled
            or self._register_profiler is None
        ):
            return None
        profile_context = RegisterProfileContext(
            character_id=character.id,
            operator_id=getattr(character, "user_id", DEFAULT_OPERATOR_ID),
            latest_user_message=(
                context.recent_dialogue_summary
                or f"proactive trigger: {context.trigger.value}"
            ),
            recent_dialogue_summary=context.recent_dialogue_summary,
            relationship_context=tuple([
                *context.operator_persona_lines,
                *context.initial_relationship_lines,
            ]),
            content_tolerance=CONTENT_TOLERANCE_FRONTIER,
        )
        try:
            return await self._register_profiler.profile(
                profile_context,
                character=character,
            )
        except Exception:
            _LOGGER.exception("proactive register profiler failed open")
            return None

    def _proactive_gate_context(
        self,
        *,
        context: ProactiveContext,
        decision: ProactiveDecision,
        character: Character,
        register_profile,
        diversity_evidence: ReplyDiversityEvidence,
        recent_dialogue: _RecentDialogue = _NO_RECENT_DIALOGUE,
    ) -> NoveltyGateContext:
        """Everything the judge sees about one draft push.

        Rebuilt per draft (the band calls it again for the re-review), so
        ``response_text`` **and** ``tool_prompt_lines`` always describe the
        candidate actually being judged rather than the one that failed.

        ``recent_dialogue`` arrives whole rather than as a loose quote: the
        時間座標 anchor needs the player's words *and* the instant those
        words are true at, and the two are only trustworthy while they
        travel together.
        """
        return NoveltyGateContext(
            character_id=character.id,
            operator_id=getattr(character, "user_id", DEFAULT_OPERATOR_ID),
            response_text=decision.message or "",
            known_material=tuple(
                item for item in (
                    context.recent_memories_text,
                    context.active_goals_text,
                    context.recent_dialogue_summary,
                    context.world_event_seed_summary,
                )
                if item and item.strip()
            ),
            recent_self_lines=tuple(
                attempt.message or ""
                for attempt in context.recent_sent_attempts[:4]
                if attempt.message
            ),
            self_repetition_hint="",
            latest_user_message=context.recent_dialogue_summary,
            content_tolerance=CONTENT_TOLERANCE_FRONTIER,
            register_profile=register_profile,
            diversity_evidence=diversity_evidence,
            persona_context=(
                f"性格：{', '.join(character.personality)}",
                f"說話風格：{character.speaking_style}",
                *context.initial_relationship_lines,
            ),
            # Without this the judge's rubric pins ``language_mismatch``
            # false, which left 晶晶體 unjudgeable on the surface that
            # produces the most player-visible prose. Already fail-soft to
            # ``"zh-TW"`` where the context is built.
            operator_primary_language=context.operator_primary_language,
            tool_prompt_lines=_decision_tool_prompt_lines(decision),
            temporal_context_lines=_proactive_temporal_lines(
                context,
                last_player_message=recent_dialogue.last_player_text,
                last_player_at=recent_dialogue.last_player_at,
            ),
        )

    async def _apply_rest_recovery(
        self, character: Character, now: datetime,
    ) -> Character:
        """Event-path recovery: POST_TURN / ACTIVITY_TRANSITION triggers
        don't go through ``_tick_all``, so we refresh here too.

        Delegates to the shared ``RestRecoveryRefresher`` to keep the
        write/snapshot policy consistent across call sites. When no
        refresher is wired (old tests), falls back to a local compute
        so the gate at least sees up-to-date values in-memory.
        """
        if self._rest_recovery_refresher is not None:
            return await self._rest_recovery_refresher.refresh(
                character, now=now,
            )
        from kokoro_link.infrastructure.state.recovery import apply_rest_recovery

        recovered_state = apply_rest_recovery(character.state, now=now)
        if recovered_state is character.state:
            return character
        return character.with_state(recovered_state)

    async def _claim_visible_slot(
        self, character_id: str, logical_slot: str | None,
    ) -> bool:
        """Try to own this character's proactive slot for ``logical_slot``.

        Returns ``True`` (proceed) when there is nothing to claim (no slot
        port wired, or ``logical_slot is None`` = manual/API path) or the
        claim succeeds. Returns ``False`` only when the slot is verifiably
        already owned by another executor. A slot-store error fails OPEN
        (returns ``True``) — a claim failure must never suppress a legitimate
        single-runner send."""
        if self._visible_slot_port is None or logical_slot is None:
            return True
        from kokoro_link.contracts.visible_slots import SLOT_KIND_PROACTIVE

        try:
            return await self._visible_slot_port.claim(
                character_id, SLOT_KIND_PROACTIVE, logical_slot,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: visible slot claim crashed; failing open "
                "character=%s slot=%s", character_id, logical_slot,
            )
            return True

    async def _find_eligible_binding(
        self, character_id: str,
    ) -> tuple[ChannelBinding, MessagingAccount] | None:
        # The dispatcher still resolves the local binding for its pre-decider
        # NO_BINDING gate, the tool-invocation conversation id, and the SENT
        # ``binding_id`` audit — all self-host-messaging facts. The external
        # *send* itself is routed through ``self._external_delivery`` (§8.3).
        # One shared helper keeps this and the local adapter's resolution from
        # drifting.
        return await find_eligible_proactive_binding(
            account_repository=self._accounts,
            binding_repository=self._bindings,
            character_id=character_id,
        )

    async def _resolve_pre_composed_sink(
        self, character_id: str, target: ResolvedProactiveSink | None,
    ) -> tuple[ChannelBinding, MessagingAccount] | None:
        """Where a pre-composed message may actually go.

        Unpinned (``target is None``) this is the plain resolution every
        caller without a gate of its own gets — unchanged.

        Pinned, it costs the same one listing but asks a different question:
        not "which binding wins now" but "is the binding the gate authorised
        still eligible" — see
        :meth:`ResolvedProactiveSink.confirm_against`. Both ways of losing
        the pin (another binding overtook it, the owner switched it off)
        collapse to ``None`` here, which is the fail-safe branch: the message
        falls back to web / hosted rather than landing on an endpoint no gate
        ever looked at.
        """
        if target is None:
            return await self._find_eligible_binding(character_id)
        if target.eligible is None:
            # The gate resolved "nowhere external". Re-deriving one now would
            # deliver to a sink it never saw.
            return None
        confirmed = target.confirm_against(
            await list_eligible_proactive_bindings(
                account_repository=self._accounts,
                binding_repository=self._bindings,
                character_id=character_id,
            ),
        )
        if confirmed is None:
            _LOGGER.info(
                "pre-composed: pinned binding is no longer eligible "
                "character=%s binding=%s — skipping the external sink",
                character_id,
                target.binding.id if target.binding else None,
            )
        return confirmed

    async def _deliver_external(
        self,
        *,
        character: Character,
        binding: ChannelBinding,
        account: MessagingAccount,
        text: str,
        attachments: tuple[OutboundAttachment, ...],
        locale: str,
        when: datetime,
    ) -> DeliveryAcceptance:
        """Build the immutable envelope and hand it to the LOCAL-binding
        delivery port.

        H4: the local-binding path deliberately does NOT write the pre-send
        ledger. The ledger + retry worker exist for the HOSTED Cloud-Channel
        path, whose HTTP response can be lost; the retry worker re-sends every
        pending/accepted-unrecorded ledger row through the HOSTED adapter. A
        local-binding event written here would therefore be re-sent to the Cloud
        Channel — the wrong endpoint entirely. Local delivery is synchronous and
        has no lost-response window (same as a self-host single container, which
        wires no ledger at all), so it needs no ledger entry.

        L12: the gate-resolved ``(binding, account)`` is handed to the adapter
        as an explicit target so the send is pinned to the endpoint the gate
        authorised, never re-resolved to a binding that shifted mid-tick.
        """
        envelope = self._build_proactive_envelope(
            character=character,
            account=account,
            text=text,
            attachments=attachments,
            locale=locale,
            when=when,
        )
        return await self._local_delivery.accept(
            envelope, target=LocalDeliveryTarget(binding=binding, account=account),
        )

    def _build_proactive_envelope(
        self,
        *,
        character: Character,
        account: MessagingAccount,
        text: str,
        attachments: tuple[OutboundAttachment, ...],
        locale: str,
        when: datetime,
    ) -> ProactiveEnvelope:
        # Self-host: the tenant is the owning operator, the account is the
        # resolved local messaging account.
        tenant_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        return self._make_envelope(
            tenant_id=tenant_id,
            account_id=account.id,
            character_id=character.id,
            text=text,
            attachments=attachments,
            locale=locale,
            when=when,
        )

    def _make_envelope(
        self,
        *,
        tenant_id: str,
        account_id: str,
        character_id: str,
        text: str,
        attachments: tuple[OutboundAttachment, ...],
        locale: str,
        when: datetime,
    ) -> ProactiveEnvelope:
        return ProactiveEnvelope(
            event_id=uuid4().hex,
            tenant_id=tenant_id,
            account_id=account_id,
            character_id=character_id,
            kind=ENVELOPE_KIND_PROACTIVE,
            segments=(ProactiveSegment(text=text),),
            attachments=tuple(attachments),
            locale=locale,
            created_at=when,
            expires_at=when + self._proactive_envelope_ttl,
        )

    async def _resolve_hosted_target(
        self, character_id: str,
    ) -> tuple[str, str] | None:
        """Hosted dual-path gate (LH4 Core-C).

        Resolve the character's cloud ``(tenant_id, account_id)`` and confirm
        the Channel will accept a proactive push now. ``None`` — self-host (no
        resolver), no cloud mapping, OR the non-authoritative cost preflight is
        not eligible — collapses to NO_BINDING-gate semantics: there is nowhere
        hosted to push, so the caller skips the hosted path (and the decider).
        """
        if self._hosted_identity_resolver is None:
            return None
        identity = await self._resolve_hosted_identity(character_id)
        if identity is None:
            return None
        try:
            eligibility = await self._hosted_delivery.check_eligibility(
                character_id,
            )
        except Exception:
            _LOGGER.exception(
                "proactive hosted: check_eligibility crashed character=%s",
                character_id,
            )
            return None
        if not eligibility.eligible:
            return None
        return identity

    async def _resolve_hosted_identity(
        self, character_id: str,
    ) -> tuple[str, str] | None:
        if self._hosted_identity_resolver is None:
            return None
        try:
            return await self._hosted_identity_resolver(character_id)
        except Exception:
            _LOGGER.exception(
                "proactive hosted: identity resolve failed character=%s",
                character_id,
            )
            return None

    async def _latest_line_conversation_id(
        self, character_id: str,
    ) -> str | None:
        try:
            conversation = await self._conversations.latest_for_character(
                character_id, source=_SOURCE_LINE,
            )
        except Exception:
            _LOGGER.exception(
                "proactive hosted: latest line conversation lookup failed "
                "character=%s", character_id,
            )
            return None
        return conversation.id if conversation is not None else None

    async def _deliver_hosted(
        self,
        *,
        character: Character,
        tenant_id: str,
        account_id: str,
        text: str,
        attachments: tuple[OutboundAttachment, ...],
        locale: str,
        when: datetime,
    ) -> DeliveryAcceptance:
        """Hosted external send (LH4 Core-C).

        Mirrors :meth:`_deliver_external`'s pre-send-ledger discipline (record
        BEFORE ``accept``, settle after) but builds the envelope from the cloud
        ``(tenant_id, account_id)`` rather than a local messaging account. On an
        accepted send the proactive turn is recorded on the character's
        ``source="line"`` conversation — the "send-then-record" parity with the
        self-host adapter's ``_deliver``.
        """
        envelope = self._make_envelope(
            tenant_id=tenant_id,
            account_id=account_id,
            character_id=character.id,
            text=text,
            attachments=attachments,
            locale=locale,
            when=when,
        )
        await self._record_pre_send(envelope, now=when)
        acceptance = await self._hosted_delivery.accept(envelope)
        await self._settle_pre_send(envelope, acceptance, now=when)
        if acceptance.delivered:
            # M8: the accepted send and its line-history row share one
            # lifecycle. Record the turn; only mark ``conversation_recorded``
            # when the append actually landed — a failed append leaves the
            # accepted ledger row for the retry worker to re-append idempotently
            # instead of being silently lost.
            recorded = await self._line_conversation_recorder.record(
                event_id=envelope.event_id,
                character_id=character.id,
                text=text,
                when=when,
            )
            if recorded and self._proactive_event_ledger is not None:
                await self._proactive_event_ledger.mark_conversation_recorded(
                    envelope.event_id, now=when,
                )
        return acceptance

    async def _record_pre_send(
        self, envelope: ProactiveEnvelope, *, now: datetime,
    ) -> None:
        if self._proactive_event_ledger is None:
            return
        await self._proactive_event_ledger.save_pre_send(
            event_id=envelope.event_id,
            tenant_id=envelope.tenant_id,
            account_id=envelope.account_id,
            character_id=envelope.character_id,
            kind=envelope.kind,
            envelope_json=json.dumps(
                envelope_to_payload(envelope),
                ensure_ascii=False,
                sort_keys=True,
            ),
            payload_hash=compute_envelope_hash(envelope),
            expires_at=envelope.expires_at,
            now=now,
        )

    async def _settle_pre_send(
        self,
        envelope: ProactiveEnvelope,
        acceptance: DeliveryAcceptance,
        *,
        now: datetime,
    ) -> None:
        """Close out the ledger row per the channel's acceptance. Best-effort:
        a ledger write failure must not fail an already-delivered send."""
        if self._proactive_event_ledger is None:
            return
        try:
            if acceptance.delivered:
                await self._proactive_event_ledger.mark_accepted(
                    envelope.event_id, now=now,
                )
            else:
                await self._proactive_event_ledger.mark_terminal(
                    envelope.event_id,
                    reason=acceptance.reason or "channel rejected delivery",
                    now=now,
                )
        except Exception:
            _LOGGER.exception(
                "proactive pre-send ledger settle failed event=%s",
                envelope.event_id,
            )

    async def _deliver_web(
        self,
        *,
        character: Character,
        text: str,
        attachments: tuple[OutboundAttachment, ...],
        when: datetime,
    ) -> None:
        """Write the proactive message to the character's web thread,
        bump the unread badge, and publish an event for SSE clients.

        Reuses whatever ``source="web"`` conversation the user already
        has open — so when they refresh / reconnect the message appears
        inline with their normal chat history, not in a parallel log.
        """
        from kokoro_link.domain.entities.conversation import Conversation

        conversation = await self._conversations.latest_for_character(
            character.id, source=SOURCE_WEB,
        )
        if conversation is None:
            conversation = Conversation.start(
                character_id=character.id, source=SOURCE_WEB,
            )

        # Demote absolute URLs that point at our own ``public_base_url``
        # back to server-relative form before persisting into the web
        # conversation. The collection step in ``_collect_tool_attachments``
        # absolute-ifies for TG/LINE (their servers fetch by URL), but
        # the web frontend should fetch from whatever origin the
        # operator opened the browser on — otherwise an internal-LAN
        # visit gets pinned to the external DDNS host the bot uses,
        # round-trips through hairpin NAT, and times out. Absolute URLs
        # pointing at OTHER hosts (e.g. external CDN) pass through
        # untouched so they still load correctly.
        public_base_url = await self._resolve_public_base_url()
        message_attachments = tuple(
            MessageAttachment(
                kind=att.kind,
                url=self._demote_to_relative(att.url, public_base_url),
                mime_type=att.mime_type,
                caption=att.caption,
            )
            for att in attachments
        )
        appended = conversation.append(
            Message(
                role=MessageRole.ASSISTANT,
                content=text,
                attachments=message_attachments,
                created_at=when,
            ),
        )
        await self._conversations.save(appended)

        next_count = character.unread_proactive_count + 1
        updated_character = character.with_unread_proactive(next_count)
        await self._characters.save(updated_character)

        if self._event_bus is not None:
            await self._event_bus.publish(
                ProactiveEvent(
                    character_id=character.id,
                    conversation_id=appended.id,
                    message=text,
                    created_at=when,
                    unread_count=updated_character.unread_proactive_count,
                ),
            )

    def _demote_to_relative(self, url: str, public_base_url: str | None = None) -> str:
        """Strip our own ``public_base_url`` prefix to keep the URL
        portable across access origins.

        Used when persisting tool attachments into the web conversation
        — the same URL might later be served via internal LAN domain,
        external DDNS, tunneled localhost in dev, etc. Keeping it
        relative means whichever origin the operator visits, the
        ``<img>`` resolves against that origin and dodges hairpin NAT.

        URLs not starting with our base (external CDN, third-party
        hosts) and already-relative URLs pass through untouched."""
        base_url = (public_base_url or self._public_base_url).rstrip("/")
        if not base_url:
            return url
        if url.startswith(base_url):
            tail = url[len(base_url):]
            if tail.startswith("/"):
                return tail
        return url

    def _describe_tools(
        self, character: Character,
    ) -> tuple[PromptToolDescriptor, ...]:
        if self._tool_registry is None:
            return ()
        tools = self._tool_registry.list_for_character(character)
        return tuple(
            PromptToolDescriptor(
                name=t.name,
                description=t.description,
                parameters_schema=t.parameters_schema,
            )
            for t in tools
        )

    # -- the outbound honesty gate (HV2) ----------------------------------

    def _claim_evidence(
        self, context: ProactiveContext, run: _ProactiveToolRun,
    ) -> OutcomeClaimEvidence:
        """Everything that actually happened on this tick, and nothing else.

        ``offered_tools`` earns its place next to ``outcomes``: "you were
        handed a camera and did not pick it up" is the zero-call case, and
        a judge that saw only an empty outcome list could not tell it from
        a deployment that has no camera at all.
        """
        return OutcomeClaimEvidence(
            offered_tools=tuple(
                tool.name for tool in context.available_tools
            ),
            outcomes=run.outcomes,
            delivered_attachments=len(run.attachments),
        )

    async def _resolve_outbound_honesty(
        self,
        *,
        character: Character,
        context: ProactiveContext,
        decision: ProactiveDecision,
        run: _ProactiveToolRun,
        conversation_id: str | None,
    ) -> _HonestyResolution:
        """May this push go out? An empty ``decision`` = send nothing.

        Two exits, mirroring the promise loop's, because the honest way out
        of each is different:

        **Zero tool calls.** Nothing ran, so any claim of a completed
        external action is unsupported — and the judge's whole job is to
        tell that apart from a promise about later, an action inside the
        fiction, and the player's own material read back. One corrected
        re-decide follows, and it may legitimately come back either as an
        honest message *or* as the tool call it should have asked for the
        first time; both are the gate working.

        **Tools ran.** No re-decide, deliberately. The decider composes its
        message in the same JSON that orders the tool, so it has never seen
        a tool result and a second pass would be writing just as blind as
        the first — while being free to order a *second* render. Until
        ``ProactiveContext`` can carry ``tool_results``, the honest move is
        to spend nothing more and send nothing. A tick costs one tick.

        Never raises. A gate that can throw would turn a model hiccup into
        an ``ERRORED`` attempt, which is strictly worse than the
        dishonesty it exists to stop.
        """
        guard = self._outcome_claim_guard
        if guard is None:
            return _HonestyResolution(decision=decision, run=run)
        verdict = await guard.review(
            message_text=decision.message or "",
            evidence=self._claim_evidence(context, run),
            character=character,
            operator_primary_language=context.operator_primary_language,
        )
        if verdict.consistent:
            return _HonestyResolution(decision=decision, run=run)
        if verdict.unavailable:
            guard.record_parked(
                reason=PARK_PROACTIVE_JUDGE_UNAVAILABLE.phrase,
                park_kind=PARK_PROACTIVE_JUDGE_UNAVAILABLE.kind,
            )
            _LOGGER.warning(
                "proactive: no honesty verdict character=%s — failing "
                "closed, nothing sent this tick", character.id,
            )
            return _HonestyResolution()
        after_tools = bool(run.outcomes)
        guard.record_block(after_tools=after_tools)
        if after_tools:
            guard.record_parked(
                reason=PARK_PROACTIVE_OVERCLAIMED_AFTER_TOOLS.phrase,
                park_kind=PARK_PROACTIVE_OVERCLAIMED_AFTER_TOOLS.kind,
            )
            _LOGGER.warning(
                "proactive: the message claimed %d outcome(s) the tools did "
                "not deliver character=%s — withholding the push (the "
                "decider writes before the tool runs, so there is nothing "
                "to rewrite from)",
                len(verdict.unsupported_claims), character.id,
            )
            return _HonestyResolution()
        _LOGGER.warning(
            "proactive: pass 1 called no tool but claimed %d completed "
            "outcome(s) character=%s — re-deciding once with a correction",
            len(verdict.unsupported_claims), character.id,
        )
        retry_context = replace(
            context,
            honesty_correction=render_honesty_correction(
                CORRECTION_ZERO_CALL, verdict.unsupported_claims,
                # The decider's output is one JSON object carrying
                # ``should_send``/``message``/``tool_calls`` together — the
                # composer-shaped "output only tool JSON, no message text"
                # road makes ``LLMProactiveDecider`` downgrade to
                # should_send=False before the tool call is ever read
                # (F3). This variant keeps the same two honest roads but
                # phrases road 1 for the decider's single-JSON contract.
                single_json_contract=True,
            ),
        )
        try:
            retry = await self._decider.decide(retry_context)
        except Exception:
            _LOGGER.exception("proactive honesty re-decide crashed")
            guard.record_parked(
                reason=PARK_PROACTIVE_CORRECTION_RAISED.phrase,
                park_kind=PARK_PROACTIVE_CORRECTION_RAISED.kind,
            )
            return _HonestyResolution()
        if not retry.should_send or not retry.message:
            # Shown its own overclaim, the character chose silence. That is
            # one of the two honest roads, not a failure — but nothing goes
            # out, so it is still a park as far as the counters go.
            guard.record_parked(
                reason=PARK_PROACTIVE_CORRECTION_SILENT.phrase,
                park_kind=PARK_PROACTIVE_CORRECTION_SILENT.kind,
            )
            return _HonestyResolution()
        retry_run = await self._execute_decision_tools(
            character=character,
            decision=retry,
            conversation_id=conversation_id,
        )
        second = await guard.review(
            message_text=retry.message,
            evidence=self._claim_evidence(context, retry_run),
            character=character,
            operator_primary_language=context.operator_primary_language,
        )
        if second.consistent:
            guard.record_corrected()
            _LOGGER.info(
                "proactive: the correction produced an honest message "
                "character=%s — shipping the second draft", character.id,
            )
            return _HonestyResolution(decision=retry, run=retry_run)
        guard.record_parked(
            reason=PARK_PROACTIVE_CORRECTION_OVERCLAIMED_AGAIN.phrase,
            park_kind=PARK_PROACTIVE_CORRECTION_OVERCLAIMED_AGAIN.kind,
        )
        _LOGGER.warning(
            "proactive: the correction re-decide claimed an outcome again "
            "character=%s — withholding the push", character.id,
        )
        return _HonestyResolution()

    async def _execute_decision_tools(
        self,
        *,
        character: Character,
        decision,  # ProactiveDecision (avoid re-import)
        conversation_id: str | None,
    ) -> _ProactiveToolRun:
        """Run the calls the decision asked for, keeping what happened.

        A failure is still not fatal here — it does not abort the tick, and
        a message that never mentioned the picture still ships without it.
        What changed with HV2 is that the failure is *recorded* rather than
        only logged. The decision's prose was written before the tool ran,
        so whether it is still true depends entirely on this list; the
        caller that has to answer that question could not, while the only
        trace of a dead renderer was a log line nobody reads mid-tick.
        """
        if not decision.tool_calls or self._tool_orchestrator is None:
            return _ProactiveToolRun()
        collected: list[OutboundAttachment] = []
        outcomes: list[ToolOutcomeMessage] = []
        public_base_url = await self._resolve_public_base_url()
        for call in decision.tool_calls:
            try:
                _, result = await self._tool_orchestrator.execute(
                    character=character,
                    call=call,
                    conversation_id=conversation_id,
                )
            except Exception as exc:
                _LOGGER.exception(
                    "proactive tool %s crashed", call.name,
                )
                outcomes.append(
                    ToolOutcomeMessage(
                        tool_name=call.name,
                        ok=False,
                        output_text="",
                        error=f"tool crashed: {exc}",
                    ),
                )
                continue
            if not result.ok:
                _LOGGER.info(
                    "proactive tool %s failed: %s", call.name, result.error,
                )
                outcomes.append(
                    ToolOutcomeMessage(
                        tool_name=call.name,
                        ok=False,
                        output_text="",
                        error=result.error or "unknown error",
                    ),
                )
                continue
            delivered = to_outbound_attachments(
                result.attachments,
                public_base_url=public_base_url,
                surface="proactive",
            )
            collected.extend(delivered)
            outcomes.append(
                ToolOutcomeMessage(
                    tool_name=call.name,
                    ok=True,
                    output_text=result.output_text,
                    attachment_urls=tuple(item.url for item in delivered),
                ),
            )
        return _ProactiveToolRun(
            attachments=tuple(collected), outcomes=tuple(outcomes),
        )

    async def _resolve_public_base_url(self) -> str:
        if self._public_base_url_provider is None:
            return self._public_base_url
        try:
            resolved = await self._public_base_url_provider()
        except Exception:
            _LOGGER.exception(
                "proactive public base URL provider failed; using env fallback",
            )
            return self._public_base_url
        if not isinstance(resolved, str):
            return self._public_base_url
        resolved = resolved.strip().rstrip("/")
        return resolved or self._public_base_url

    async def _resolve_schedule(
        self, character: Character, now: datetime,
    ):
        if self._schedule_resolver is None:
            return None, [], None, None
        try:
            return await self._schedule_resolver(character, now)
        except Exception:
            _LOGGER.exception("proactive schedule resolver crashed")
            return None, [], None, None

    async def _load_recent_memories(
        self, character_id: str, now: datetime | None = None,
    ) -> "tuple[str, tuple[MemoryItem, ...]]":
        """The recall block, plus the memories the player hasn't been told.

        Returns both from one query rather than letting the caller
        re-derive the second: KB8's flip is only defensible if the
        candidate set is *exactly* what this tick put in front of the
        composer, and a second query — even one issued microseconds
        later — is a different set the moment anything writes a memory.
        """
        if self._memories is None:
            return "", ()
        try:
            items = await self._memories.query(
                character_id, limit=_MEMORY_RECALL_LIMIT,
            )
        except Exception:
            _LOGGER.exception("proactive: memory repository query failed")
            return "", ()
        # The renderer takes the same slice, so the candidates are the
        # memories that were rendered — not the ones that were fetched.
        return (
            _format_memories(items, now=now),
            select_private_candidates(items[:_MEMORY_RECALL_LIMIT]),
        )

    async def _flip_disclosed_memories(
        self,
        *,
        character: Character,
        message_text: str,
        candidates: "tuple[MemoryItem, ...]",
    ) -> tuple[str, ...]:
        """Ask what the delivered push actually told the player (KB8).

        The one added model call in this ticket, and it buys the thing
        the free alternatives cannot: a push is composed from several
        recalled memories and says one thing, so "it was in the prompt"
        is not evidence it was said. D10 weighed a small per-send call
        against a ledger that marks untold facts as told and chose the
        call.

        Every way this can go wrong ends in "nothing flipped": no judge
        wired, no candidates, an unparseable reply, an upstream outage, a
        repository error. That direction is the point — the character
        re-introducing something costs a repeated sentence, while the
        inverse writes a falsehood into a ledger with no reverse
        transition.

        Never raises: it runs after delivery, so an exception here would
        turn a message the player already has into a failed tick.
        """
        if (
            self._disclosure_judge is None
            or self._memory_disclosure is None
            or not candidates
            or not (message_text or "").strip()
        ):
            return ()
        try:
            verdict = await self._disclosure_judge.judge(
                message_text=message_text,
                candidates=tuple(
                    DisclosureCandidate(
                        memory_id=item.id, content=item.content,
                    )
                    for item in candidates
                ),
                character=character,
            )
        except Exception:
            _LOGGER.exception(
                "proactive disclosure judge crashed character=%s",
                character.id,
            )
            return ()
        if verdict.unavailable or not verdict.disclosed_ids:
            return ()
        # Re-bound the verdict here as well as inside the adapter. The
        # port allows any implementation, and this is the last point
        # before a write that cannot be undone.
        allowed = {item.id for item in candidates}
        return await self._memory_disclosure.disclose(
            character_id=character.id,
            memory_ids=[
                item_id for item_id in verdict.disclosed_ids
                if item_id in allowed
            ],
        )

    async def _load_upcoming_day_schedules(
        self, character_id: str, when: datetime, local_tz: tzinfo,
    ) -> list:
        """Read pre-planned tomorrow + day-after schedules.

        Read-only — the proactive scheduler tick is the eager
        generator (``ensure_window``); this dispatcher path only
        renders what the repository already has. Returns an empty
        list when the schedule service is unwired or no upcoming
        days are present.
        """
        if self._schedule_service is None:
            return []
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        target_date = when.astimezone(local_tz).date()
        try:
            return await self._schedule_service.load_upcoming_schedules(
                character_id, start_after=target_date,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: load_upcoming_schedules failed character=%s",
                character_id,
            )
            return []

    async def _load_operator_profile(self, character) -> OperatorProfile | None:  # noqa: ANN001
        service = self._operator_profile_service
        if service is None:
            return None
        user_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            return await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return None

    async def _load_operator_language(self, character) -> str:  # noqa: ANN001
        """Resolve the BCP 47 ``primary_language`` of the character's
        owner. ``"zh-TW"`` is the deterministic fallback for any path
        that can't reach the operator profile — matches the alembic
        backfill so behaviour is consistent."""
        default = "zh-TW"
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            operator = await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        lang = getattr(operator, "primary_language", "") or ""
        return lang.strip() or default

    async def _load_operator_timezone(self, character) -> tzinfo:  # noqa: ANN001
        default = self._local_tz
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            operator = await service.get_for_user(user_id)
            return timezone_for_id(getattr(operator, "timezone_id", None))
        except Exception:  # pragma: no cover - defensive
            return default

    async def _load_player_persona_note(
        self,
        character: Character,
        account: "MessagingAccount | None",
    ) -> str:
        """The player's declared identity / world premise, if it may be said here.

        One compose is fanned out to web, the local binding and the
        hosted channel at once, so the note has to be decided against the
        *least private* sink rather than per delivery. When an external
        binding exists, the account behind it therefore governs: a group
        or open-allowlist account (the same shape that already suppresses
        persona learning inbound) blanks the note for every sink on this
        tick, including the owner's own web session. Losing staging on a
        browser tab is a far cheaper mistake than reading the owner's
        declared setting aloud to a group chat.
        """
        if self._player_persona_note_repository is None:
            return ""
        if account is not None and not persona_safe_for_account(account):
            return ""
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            row = await self._player_persona_note_repository.get(
                character_id=character.id,
                operator_id=operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: player persona note load failed character=%s",
                character.id,
            )
            return ""
        return row.note if row is not None else ""

    async def resolve_proactive_sink(
        self, character_id: str,
    ) -> ResolvedProactiveSink | None:
        """Resolve — ONCE — the external sink a pre-composed send may use.

        The repository-touching half of the disclosure gate, kept separate so
        a caller that composes for tens of seconds before delivering can hold
        on to the answer and hand it back to :meth:`deliver_pre_composed` as
        an explicit target. Asking twice is the defect this exists to prevent:
        the gate would authorise the owner's own DM and the send would land on
        whichever binding was touched most recently by the time the composer
        finished. There is deliberately no convenience form that resolves and
        discards in one call — that shape is exactly the unpinned re-query
        the release path used to leak through.

        ``None`` means the lookup itself failed — no sink, no verdict. The
        caller must treat it as "cannot disclose", not as "web only"; that
        distinction is why this returns an optional rather than raising.
        """
        try:
            eligible = await self._find_eligible_binding(character_id)
        except Exception:
            _LOGGER.exception(
                "proactive: disclosure gate lookup failed character=%s",
                character_id,
            )
            return None
        return ResolvedProactiveSink.of(eligible)

    @staticmethod
    def persona_disclosure_allowed_for(
        sink: ResolvedProactiveSink | None,
    ) -> bool:
        """The verdict for an already-resolved sink.

        Pure, so the gate and the send provably read the same snapshot rather
        than two lookups that merely tend to agree. ``None`` (unresolved) is
        fail-closed for the same reason a raising lookup is."""
        if sink is None:
            return False
        if sink.account is None:
            # Nowhere external at all — only the owner's own session can
            # hear this, so the declaration is theirs to hear.
            return True
        return persona_safe_for_account(sink.account)

    async def _load_operator_persona_lines(self, character: Character) -> list[str]:
        service = self._operator_persona_service
        if service is None:
            return []
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            persona = await service.get_current(character.id, operator_id)
            return list(service.render_for_prompt(persona))
        except Exception:
            _LOGGER.exception(
                "proactive: operator persona render failed character=%s",
                character.id,
            )
            return []

    async def _load_relationship_seed(
        self, character: Character,
    ) -> CharacterOperatorRelationshipSeed | None:
        if self._relationship_seed_repository is None:
            return None
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            return await self._relationship_seed_repository.get(
                character_id=character.id,
                operator_id=operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: relationship seed lookup failed character=%s",
                character.id,
            )
            return None

    async def _load_persona_curiosity_plan(
        self,
        *,
        character: Character,
        operator: OperatorProfile | None,
        recent_dialogue_summary: str,
        now: datetime,
        initial_relationship_lines: list[str] | tuple[str, ...] = (),
    ) -> "PersonaCuriosityPlan | None":
        if (
            self._operator_persona_service is None
            or self._persona_curiosity_service is None
            or self._persona_curiosity_planner is None
        ):
            return None
        operator_id = getattr(operator, "id", None) or DEFAULT_OPERATOR_ID
        try:
            persona = await self._operator_persona_service.get_current(
                character.id,
                operator_id,
            )
            context = await self._persona_curiosity_service.build_context(
                persona=persona,
                surface="proactive",
                recent_dialogue_summary=recent_dialogue_summary,
                initial_relationship_lines=tuple(initial_relationship_lines),
                now=now,
                operator_primary_language=_language_for_operator(operator),
            )
            plan = await self._persona_curiosity_planner.plan(
                context,
                character=character,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: persona curiosity plan failed character=%s",
                character.id,
            )
            return None
        await self._record_persona_curiosity_planned(
            context=context,
            plan=plan,
            now=now,
        )
        return plan

    async def _record_persona_curiosity_planned(
        self,
        *,
        context,
        plan: "PersonaCuriosityPlan",
        now: datetime,
    ) -> None:
        if self._persona_curiosity_service is None:
            return
        try:
            await self._persona_curiosity_service.record_planned_attempt(
                context=context,
                plan=plan,
                now=now,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: persona curiosity planned-attempt record failed",
            )

    def _describe_calendar(
        self,
        when: datetime,
        local_tz: tzinfo,
        *,
        operator: OperatorProfile | None = None,
    ) -> str:
        """Render the real-world calendar block for the local-tz civil
        date that ``when`` falls on.

        Empty string when no calendar port is wired or describe raises
        — the decider section then renders nothing. Logged so operators
        can tell a "missing" block from a "disabled" one.
        """
        if self._calendar_context_port is None:
            return ""
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        target_date = when.astimezone(local_tz).date()
        try:
            return self._calendar_context_port.describe(
                target_date,
                region=calendar_region_from_operator(operator),
            )
        except Exception:
            _LOGGER.exception(
                "proactive: calendar describe failed target=%s", target_date,
            )
            return ""

    async def _describe_weather(
        self,
        when: datetime,
        *,
        operator: OperatorProfile | None = None,
    ) -> str:
        """Async weather counterpart. ``when`` is currently unused by
        the only implemented adapter (Open-Meteo always reports the
        present moment) but threaded through for parity / future use."""
        if self._weather_context_port is None:
            return ""
        try:
            return await self._weather_context_port.describe(
                now=when,
                location=weather_location_from_operator(operator),
            )
        except Exception:
            _LOGGER.exception(
                "proactive: weather describe failed; falling back to empty",
            )
            return ""

    async def _summarize_recent_dialogue(
        self,
        character: Character,
        *,
        now: datetime,
        local_tz: tzinfo,
    ) -> _RecentDialogue:
        """Condense the character's latest dialogue for the decider.

        Pulls messages merged across every source (web / telegram /
        line / …) — the character is one person on every channel, so
        the decider sees the same unified timeline as the chat prompt.
        Returns empty string when the summariser is unwired, no
        messages exist, or summarisation fails — the decider treats
        empty as "no dialogue context" and skips the section.

        **Two anchors, not one.** The summary itself is now built from a
        time-anchored transcript (see ``LLMDialogueSummarizer``), but a
        summary is still model output: it can blur or drop the anchor
        however the template is worded. So the last few turns are also
        appended verbatim with their timestamps, assembled in Python —
        nothing between ``Message.created_at`` and the decider prompt.
        If the summary ever misdates an event again, the raw tail sits
        directly beneath it saying otherwise.

        The tail rides the existing ``recent_dialogue_summary`` field
        rather than a new ``ProactiveContext`` slot plus template
        placeholder: ``decider_instructions.txt`` is shadowed by a hosted
        tuned overlay, and a new placeholder would need both copies kept
        in lockstep forever. Same idiom the quality-gate retry already
        uses to fold feedback into this field.

        Returns the summary **and** the player's last line with its own
        ``created_at`` (G2-3): the quality gate's time anchor wants the
        quote, and this is the only place in the tick that already holds
        the messages — which is also the only place that holds the
        instant that quote is true at. Every early return yields the empty
        dialogue rather than reaching for a second query; a missing quote
        drops the speech anchor and leaves the interaction anchor
        standing, which is the honest degradation.
        """
        if self._dialogue_summarizer is None:
            return _NO_RECENT_DIALOGUE
        try:
            messages = await self._conversations.recent_messages_for_character(
                character.id, limit=40, exclude_tool_only=True,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: dialogue load failed character=%s", character.id,
            )
            return _NO_RECENT_DIALOGUE
        if not messages:
            return _NO_RECENT_DIALOGUE
        messages = sanitize_messages_for_tolerance(
            messages,
            content_tolerance=CONTENT_TOLERANCE_FRONTIER,
        )
        if not messages:
            return _NO_RECENT_DIALOGUE
        try:
            summary = await self._dialogue_summarizer.summarize(
                character=character,
                messages=messages,
                now=now,
                local_tz=local_tz,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: dialogue summarise failed character=%s", character.id,
            )
            summary = ""
        fresh_tail = _render_fresh_dialogue_tail(
            character, messages, now=now, local_tz=local_tz,
        )
        last_player = _last_player_turn(messages)
        return _RecentDialogue(
            summary="\n\n".join(
                part for part in ((summary or "").strip(), fresh_tail) if part
            ),
            last_player_text=(
                (last_player.content or "").strip() if last_player else ""
            ),
            last_player_at=(
                getattr(last_player, "created_at", None)
                if last_player else None
            ),
        )

    async def _has_user_started_interaction(self, character: Character) -> bool:
        if character.state.last_active_at is not None:
            return True
        try:
            return await self._conversations.has_user_message_for_character(
                character.id,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: first-user-message lookup failed character=%s",
                character.id,
            )
            return False

    async def _check_pre_message_budget(
        self, *, character_id: str, now: datetime,
    ) -> GateVerdict:
        """TR2-A — how much of the pre-message push budget is left.

        Only ever called while the player has not spoken, which is what
        makes the audit log sufficient on its own: every SENT row this
        character has is, by construction, a pre-message push. (The two
        promise-fulfilment triggers that could send without a prior
        player turn cannot exist here — a promise is lodged from a chat
        turn, and a chat turn is what "the player has spoken" means.)

        Fetching ``cap`` rows is enough for both questions: at ``cap``
        rows the ceiling already answers, and below it the fetch is the
        complete history whose newest row carries the spacing.

        A repository failure blocks rather than sends. The read is one
        indexed lookup and the cost of getting it wrong is asymmetric:
        an over-strict tick delays a push that the decider was free to
        skip anyway, while an over-permissive one is the exact unbounded
        nagging this gate exists to prevent.
        """
        try:
            sent = await self._attempts.list_recent_sent(
                character_id, limit=max(PRE_MESSAGE_PROACTIVE_CAP, 1),
            )
        except Exception:
            _LOGGER.exception(
                "proactive: pre-message budget lookup failed character=%s",
                character_id,
            )
            return GateVerdict(
                passed=False,
                reason=PRE_MESSAGE_BUDGET_UNAVAILABLE_REASON,
            )
        return evaluate_pre_message_proactive_budget(tuple(sent), now=now)

    async def _claim_event_seed(
        self, character: Character, when: datetime,
    ) -> _ClaimedEventSeed:
        """Try to claim a curated world event for the proactive surface.

        Returns :data:`_NO_EVENT_SEED` when no dispenser is wired, the
        character opted out of world awareness, or no fresh seed is
        available. ``item_id`` is the inbox row the claim won — the
        caller hands it to :meth:`_release_event_seed` if the message
        never goes out, so the seed flows back to the next surface
        instead of being burned silently.
        """
        if self._event_seed_dispenser is None:
            return _NO_EVENT_SEED
        if not character.world_awareness_enabled:
            return _NO_EVENT_SEED
        try:
            claimed = await self._event_seed_dispenser.claim(
                character_id=character.id, surface="proactive_message",
                now=when,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: event seed claim failed character=%s", character.id,
            )
            return _NO_EVENT_SEED
        if claimed is None:
            return _NO_EVENT_SEED
        return _ClaimedEventSeed(
            title=claimed.event.title or "",
            summary=claimed.event.summary or "",
            source=claimed.event.source or "",
            locale=claimed.event.locale or "",
            item_id=claimed.item.id,
            world_event_id=claimed.event.id,
        )

    async def _record_event_mention(
        self, *, character_id: str, seed: _ClaimedEventSeed, when: datetime,
    ) -> None:
        """Remember that this character used this event to reach out.

        Called only after a delivery actually landed — the same point at
        which the seed's claim becomes permanent. Chat reads these back
        so a player who asks about the news the character just messaged
        them about gets an answer instead of a blank stare: the claimed
        inbox row is invisible to the chat peek forever after.

        Best-effort by construction. A mention that fails to record
        costs context on a later turn; raising here would cost the
        proactive tick its outcome log after the message already went
        out.

        **Known gap — the hosted retry path.** A transient hosted
        delivery failure leaves ``delivered == 0``, so this tick
        releases the seed and records nothing; when
        ``ProactiveDeliveryRetryWorker`` later succeeds off the durable
        ledger, the player gets the message but no mention exists, so
        chat cannot recall it. Closing it means carrying the event id
        through ``ProactiveEnvelope`` and the ledger — i.e. changing the
        delivery/retry contract — and it would still be recording a send
        whose seed this tick already handed back to the other surfaces.
        The direction matches what the seed release already chose: miss
        the record rather than assert a send that may never happen."""
        if seed.world_event_id is None or self._event_mentions is None:
            return
        try:
            await self._event_mentions.record(
                CharacterEventMention.create(
                    character_id=character_id,
                    world_event_id=seed.world_event_id,
                    surface="proactive_message",
                    mentioned_at=when,
                ),
            )
        except Exception:
            _LOGGER.exception(
                "proactive: event mention record failed character=%s event=%s",
                character_id, seed.world_event_id,
            )

    async def _release_event_seed(
        self, character_id: str, item_id: str | None,
    ) -> None:
        """Counter-part to :meth:`_claim_event_seed`. Best-effort.

        Called when a claim was made but the message never went out
        (decider skipped, decider crashed, no eligible delivery
        binding). Without this the seed is locked to ``proactive_message``
        forever even though no message referenced it, starving feed and
        drama of fresh inbox rows.
        """
        if item_id is None or self._event_seed_dispenser is None:
            return
        try:
            await self._event_seed_dispenser.release(
                item_id=item_id, surface="proactive_message",
            )
        except Exception:
            _LOGGER.exception(
                "proactive: event seed release failed character=%s item=%s",
                character_id, item_id,
            )

    async def _ensure_active_arc(
        self, character: Character, when: datetime, local_tz: tzinfo,
    ):
        """Mirror ChatService: lazy-create the character's active arc so
        the decider sees the same narrative anchor user-chat does.
        Without this the proactive path only had gacha events + dialogue
        summary, and newly created characters had no arc at all — leading
        to openers that ignored whatever arc the user was mid-way through.

        Returns ``(arc, upcoming_beats, beat_awaiting_player)``; empty on
        any failure — arcs are colour, never worth aborting a proactive
        push over."""
        if self._story_arc_service is None:
            return None, (), None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        today = when.astimezone(local_tz).date()
        try:
            arc = await self._story_arc_service.ensure_active_arc(
                character, today=today, auto_start=True,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: arc ensure_active_arc crashed character=%s",
                character.id,
            )
            return None, (), None
        if arc is None:
            return None, (), None
        try:
            forward = arc.forward_beats(
                after=today, limit=2, include_today=True,
            )
        except Exception:
            _LOGGER.exception("proactive: arc.forward_beats crashed")
            forward = []
        return arc, tuple(forward), _beat_awaiting_the_player(arc, today=today)

    async def _load_story_events(
        self, character: Character, when: datetime,
    ) -> tuple[StoryEvent, ...]:
        """Today's story events (idempotent ensure). Empty on failure.

        ``unattended=True``: this runs on a background tick with no
        player in the room, so a due beat whose ``operator_position`` is
        ``central`` must be left waiting rather than attempted and
        (past the recheck threshold) written into canon unseen
The invitation this dispatcher
        may go on to send is what that beat is waiting for — playing it
        here would answer the invitation before it was made.
        """
        if self._story_event_service is None:
            return ()
        try:
            report = await self._story_event_service.ensure_today(
                character, now=when, unattended=True,
            )
            return tuple(report.events)
        except Exception:
            _LOGGER.exception("proactive: story ensure_today crashed")
            return ()

    async def _load_active_deferred_intents(
        self,
        *,
        character_id: str,
        when: datetime,
    ) -> tuple:
        """HUMANIZATION_ROADMAP §3.4 helper.

        Returns the still-active deferred motives the intention judge
        previously parked, as a tuple suitable for ``ProactiveContext``.
        Empty when the feature is off, the service is not wired, or no
        motives are pending. All failures collapse to an empty tuple so
        the dispatcher never blocks proactive evaluation on this
        secondary signal.
        """
        if self._deferred_intents is None or not self._deferred_intents.enabled:
            return ()
        try:
            intents = await self._deferred_intents.list_active(
                character_id, DEFAULT_OPERATOR_ID, now=when,
            )
        except Exception:
            _LOGGER.exception(
                "deferred_intent list_active crashed character=%s",
                character_id,
            )
            return ()
        return tuple(intents)

    async def _load_address_preference(
        self,
        *,
        character_id: str,
    ):
        """HUMANIZATION_ROADMAP §4.2 helper.

        Returns the persisted ``OperatorAddressPreference`` for the
        ``(character, default_operator)`` pair, or ``None`` when the
        repository is missing / no row recorded / lookup failed. All
        failures collapse to ``None`` — the prompt builder treats this
        as "no observation, fall back to §3.6 pace".
        """
        if self._address_preferences is None:
            return None
        try:
            return await self._address_preferences.get(
                character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
            )
        except Exception:
            _LOGGER.exception(
                "address_preference lookup crashed character=%s",
                character_id,
            )
            return None

    async def _load_due_deferred_intents(
        self,
        *,
        character_id: str,
        when: datetime,
    ) -> tuple:
        """T2 helper — parked motives whose ``revisit_at`` alarm has rung.

        Runs before the cheap gate, so it stays a single narrow query and
        never triggers the GC sweep ``list_active`` does. All failures
        collapse to an empty tuple: losing an exemption degrades to the
        pre-T2 behaviour, while raising here would take the whole tick
        down over a secondary signal.
        """
        if self._deferred_intents is None or not self._deferred_intents.enabled:
            return ()
        try:
            intents = await self._deferred_intents.list_due(
                character_id, DEFAULT_OPERATOR_ID, now=when,
            )
        except Exception:
            _LOGGER.exception(
                "deferred_intent list_due crashed character=%s",
                character_id,
            )
            return ()
        return tuple(intents)

    async def _spend_due_revisits(self, intents: "tuple") -> None:
        """Clear the alarms that just bought a gate pass (single-use)."""
        if self._deferred_intents is None or not intents:
            return
        try:
            await self._deferred_intents.clear_revisit_many(
                [intent.id for intent in intents],
            )
        except Exception:
            _LOGGER.exception("deferred_intent clear_revisit_many crashed")

    async def _restore_due_revisits(self, intents: "tuple") -> None:
        """Give the alarms back when the tick they bought never produced
        a judgement.

        Spending is unconditional on purpose (a tick that reached the
        judge must not be able to exempt the next one), but a tick where
        the judge itself was unavailable never *asked the question* — the
        appointment is still unkept, and burning the alarm there would
        let one upstream 5xx cost the promise permanently. The restore is
        best-effort and conditional in the store: a concurrent consume or
        a fresher appointment wins.

        A restored alarm re-fires on the next tick, so a sustained
        outage retries once per tick instead of once — bounded by the
        row's own TTL, and each retry is the same failing call the tick
        would have made anyway.
        """
        if self._deferred_intents is None or not intents:
            return
        try:
            await self._deferred_intents.restore_revisit_many(intents)
        except Exception:
            _LOGGER.exception("deferred_intent restore_revisit_many crashed")

    async def _record_deferred_intent(
        self,
        *,
        character_id: str,
        trigger: ProactiveTrigger,
        decision: ProactiveIntentionDecision,
        local_tz: tzinfo,
        now: datetime,
    ) -> None:
        if self._deferred_intents is None:
            return
        try:
            await self._deferred_intents.record_if_useful(
                character_id=character_id,
                operator_id=DEFAULT_OPERATOR_ID,
                trigger=trigger.value,
                decision=decision,
                revisit_at=_parse_revisit_at(
                    decision.revisit_at_iso, local_tz=local_tz, now=now,
                ),
                now=now,
            )
        except Exception:
            _LOGGER.exception(
                "deferred_intent record crashed character=%s",
                character_id,
            )

    async def _consume_deferred_intents(
        self,
        intents: "tuple",
        *,
        now: datetime,
    ) -> None:
        if self._deferred_intents is None or not intents:
            return
        try:
            await self._deferred_intents.mark_consumed_many(
                [intent.id for intent in intents], now=now,
            )
        except Exception:
            _LOGGER.exception("deferred_intent mark_consumed_many crashed")

    async def _load_recent_sent_attempts(
        self, character_id: str,
    ) -> tuple[ProactiveAttempt, ...]:
        """Grab the most-recent actually-sent proactive attempts.

        The decider needs this to avoid re-generating the same message
        across cooldown windows — the LLM sees the same context every
        tick (personality, state, today's story event) and without the
        tail of its own recent output it happily paraphrases the same
        opener all night. Source-filtered by ``list_recent_sent`` so the
        flood of GATE_BLOCKED audit rows (one per ~5-min tick) can't bury
        cross-day SENT history the way the old over-fetch-and-filter did.
        """
        try:
            sent = await self._attempts.list_recent_sent(
                character_id, limit=_RECENT_SENT_LIMIT,
            )
        except Exception:
            _LOGGER.exception("proactive: recent-sent query failed")
            return ()
        return tuple(sent)

    async def _is_playing_a_scene(self, character_id: str) -> bool:
        """Is this character inside a live 起幕 scene right now?

        **Fails open.** An unreadable session table means an unknown
        answer, and the two ways to be wrong are not symmetric: pausing on
        error would silence every proactive message on the deployment for
        as long as the read kept failing, while proceeding costs at most
        one message that arrives during a scene. The pause is narrative
        polish (§3.2), not a safety or billing wall — the walls in this
        method's neighbourhood fail closed precisely because they are.

        Deliberately NOT applied to :meth:`deliver_pre_composed`: that
        path releases a busy-defer follow-up or a scheduled promise the
        player is already owed, and its caller turns any non-``sent``
        outcome into a terminal ``failed`` row. Blocking there would not
        postpone the message, it would delete it — trading a small framing
        break for a broken promise. Deferring instead of dropping belongs
        in the follow-up dispatcher's own release decision, where "not
        now" already has a meaning.
        """
        if self._story_scene_sessions is None:
            return False
        try:
            session = await self._story_scene_sessions.get_open_for_character(
                character_id,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: story scene lookup failed character_id=%s",
                character_id,
            )
            return False
        return session is not None

    async def _load_active_goals_text(
        self,
        character_id: str,
        now: datetime | None = None,
        local_tz: tzinfo | None = None,
    ) -> str:
        if self._goals is None:
            return ""
        try:
            goals = await self._goals.list_for_character(
                character_id, statuses=(GoalStatus.ACTIVE,),
            )
        except Exception:
            _LOGGER.exception("proactive: goal repository query failed")
            return ""
        if not goals:
            return ""
        tz = local_tz or self._local_tz
        lines = [
            f"- {g.content}（優先 {g.priority}）{_goal_age_tag(g, now, tz)}"
            for g in goals[:5]
        ]
        return "\n".join(lines)

    async def deliver_pre_composed(
        self,
        *,
        character_id: str,
        text: str,
        trigger: ProactiveTrigger,
        reason: str = "",
        attachments: tuple[OutboundAttachment, ...] = (),
        target: ResolvedProactiveSink | None = None,
        now: datetime | None = None,
    ) -> ProactiveAttempt:
        """Fan out a message whose text was decided elsewhere.

        Used by the ``PendingFollowUpDispatcher`` (and any other future
        caller that owns its own decision flow). Skips gate / decider /
        cooldown — the caller is responsible for not abusing it. Runs
        the same web + binding fan-out as the standard evaluate path,
        and writes a ``proactive_attempt`` row so the dispatch shows up
        in the audit log.

        ``target`` is the L12 pin, extended to this path: the caller's own
        gate already resolved a :class:`ResolvedProactiveSink` and decided
        what the message may say from it (whether it carries the owner's
        declared identity), so the send has to land on THAT sink — including
        when the sink is empty, which means the gate cleared the text for a
        web-only character and no binding that surfaced during the compose
        window was ever gated. Passing it is what makes the two agree; the
        default ``None`` keeps every caller that has no gate of its own
        resolving here exactly as before.

        Failure semantics mirror ``evaluate``: a partial fan-out (web
        succeeds, TG fails) still counts as SENT — the user got the
        message somewhere. Both sides failing logs ERRORED.
        """
        when = self._resolve_now(now)
        body = (text or "").strip()
        if not body:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.ERRORED,
                reason="empty pre-composed message",
                now=when,
            )
        character = await self._characters.get(character_id)
        if character is None:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.ERRORED,
                reason="character not found",
                now=when,
            )
        if (
            self._subscription_access_guard is not None
            and not await self._subscription_access_guard.is_character_allowed(
                character,
            )
        ):
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.DISABLED,
                reason="subscription is inactive",
                now=when,
            )

        eligible = await self._resolve_pre_composed_sink(character_id, target)
        # The gate authorised ONE external endpoint and it is gone. Reaching
        # for the hosted channel now would substitute a sink the gate never
        # saw — the same defect as re-resolving a local binding, one door
        # along. Web (below) still gets the message.
        pin_withdrawn = (
            eligible is None
            and target is not None
            and target.eligible is not None
        )
        web_enabled = bool(character.accepts_web_proactive)
        # Same LH4 Core-C hosted dual-path as ``evaluate``: no local binding but
        # a resolvable, channel-eligible cloud identity is a valid hosted sink.
        hosted_target: tuple[str, str] | None = None
        if eligible is None and not pin_withdrawn:
            hosted_target = await self._resolve_hosted_target(character_id)
        if eligible is None and hosted_target is None and not web_enabled:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.NO_BINDING,
                reason=(
                    "no binding has accepts_proactive=True and "
                    "accepts_web_proactive is False"
                ),
                now=when,
            )
        binding, account = eligible if eligible else (None, None)

        delivered = 0
        web_delivered = False
        external_delivered = False
        binding_id_for_log: str | None = None
        if web_enabled:
            try:
                await self._deliver_web(
                    character=character,
                    text=body,
                    attachments=attachments,
                    when=when,
                )
                delivered += 1
                web_delivered = True
            except Exception:
                _LOGGER.exception(
                    "pre-composed web delivery crashed character=%s",
                    character_id,
                )
        if binding is not None and account is not None:
            try:
                acceptance = await self._deliver_external(
                    character=character,
                    binding=binding,
                    account=account,
                    text=body,
                    attachments=attachments,
                    locale=await self._load_operator_language(character),
                    when=when,
                )
                if acceptance.delivered:
                    delivered += 1
                    external_delivered = True
                    binding_id_for_log = binding.id
                else:
                    _LOGGER.warning(
                        "pre-composed external delivery not accepted "
                        "character=%s reason=%s",
                        character_id, acceptance.reason,
                    )
            except Exception:
                _LOGGER.exception(
                    "pre-composed messaging delivery crashed character=%s",
                    character_id,
                )
        elif hosted_target is not None:
            try:
                acceptance = await self._deliver_hosted(
                    character=character,
                    tenant_id=hosted_target[0],
                    account_id=hosted_target[1],
                    text=body,
                    attachments=attachments,
                    locale=await self._load_operator_language(character),
                    when=when,
                )
                if acceptance.delivered:
                    delivered += 1
                    external_delivered = True
                    # The Hosted delivery id is an opaque Channel receipt,
                    # not a Core channel_bindings.id foreign key.
                    # binding_id_for_log intentionally remains None.
                else:
                    _LOGGER.warning(
                        "pre-composed hosted delivery not accepted "
                        "character=%s reason=%s",
                        character_id, acceptance.reason,
                    )
            except Exception:
                _LOGGER.exception(
                    "pre-composed hosted delivery crashed character=%s",
                    character_id,
                )

        if delivered == 0:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.ERRORED,
                reason="delivery raised",
                binding_id=(binding.id if binding else None),
                message=body,
                now=when,
            )

        await self._notify_web_push(
            character=character,
            message=body,
            web_delivered=web_delivered,
            external_delivered=external_delivered,
        )

        return await self._log(
            character_id=character_id,
            trigger=trigger,
            outcome=ProactiveOutcome.SENT,
            reason=reason or f"pre-composed via {trigger.value}",
            binding_id=binding_id_for_log,
            message=body,
            now=when,
        )

    async def _notify_web_push(
        self,
        *,
        character: Character,
        message: str,
        web_delivered: bool,
        external_delivered: bool,
    ) -> None:
        if not web_delivered or self._notification_service is None:
            return
        try:
            await self._notification_service.notify_proactive(
                character,
                message,
                external_delivered=external_delivered,
            )
        except Exception:
            _LOGGER.exception(
                "proactive web push notification failed character=%s",
                character.id,
            )

    async def _log(
        self,
        *,
        character_id: str,
        trigger: ProactiveTrigger,
        outcome: ProactiveOutcome,
        reason: str,
        now: datetime,
        binding_id: str | None = None,
        message: str | None = None,
        metadata: dict | None = None,
        prompt_assembled: str = "",
    ) -> ProactiveAttempt:
        attempt = ProactiveAttempt.record(
            character_id=character_id,
            trigger=trigger,
            outcome=outcome,
            reason=reason,
            binding_id=binding_id,
            message=message,
            metadata=metadata,
            now=now,
        )
        try:
            await self._attempts.add(attempt)
        except Exception:
            _LOGGER.exception("failed to persist proactive attempt log")
        emotion_event_ids = await self._record_proactive_emotion_event(
            attempt=attempt,
        )
        if self._turn_recorder is not None:
            try:
                await self._turn_recorder.record(TurnRecordingDraft(
                    character_id=character_id,
                    kind="proactive",
                    prompt_pack_hash=(
                        self._prompt_pack_hash_provider()
                        if self._prompt_pack_hash_provider is not None else ""
                    ),
                    prompt_assembled=prompt_assembled,
                    response_text=message or "",
                    post_turn_refs={
                        "proactive_attempt_id": attempt.id,
                        "trigger": trigger.value,
                        "outcome": outcome.value,
                        "reason": reason,
                        "binding_id": binding_id,
                        **(metadata or {}),
                        "emotion_event_ids": emotion_event_ids,
                    },
                ))
            except Exception:
                _LOGGER.exception("turn_recorder dispatch failed (kind=proactive)")
        return attempt

    async def _record_proactive_emotion_event(
        self, *, attempt: ProactiveAttempt,
    ) -> list[str]:
        """Mirror proactive audit outcomes into the emotion event stream.

        This is a low-intensity provenance event, not a semantic mood
        rewrite. Numeric deltas stay zero; later LLM-first layers can
        decide whether repeated blocked/sent attempts matter.
        """
        if self._emotion_events is None:
            return []
        try:
            event = EmotionEvent.new(
                character_id=attempt.character_id,
                operator_id=DEFAULT_OPERATOR_ID,
                cause_ref_kind=CAUSE_PROACTIVE_ATTEMPT,
                cause_ref_id=attempt.id,
                valence=_proactive_event_valence(attempt.outcome),
                arousal=0.1 if attempt.outcome != ProactiveOutcome.DISABLED else 0.0,
                intensity=_proactive_event_intensity(attempt.outcome),
                applied_to_state=False,
                emotion_label=f"proactive:{attempt.outcome.value}",
                evidence_quote=_proactive_event_evidence(attempt),
                decay_half_life_minutes=360,
                now=attempt.decided_at,
            )
            await self._emotion_events.add(event)
            return [event.id]
        except Exception:
            _LOGGER.exception(
                "emotion_event_repository.add failed (cause=proactive_attempt, attempt=%s)",
                attempt.id,
            )
            return []

    def _resolve_now(self, now: datetime | None) -> datetime:
        return ensure_utc(
            now if now is not None else (
                self._clock.now()
                if self._clock is not None
                else datetime.now(timezone.utc)
            ),
        )


def _beat_awaiting_the_player(
    arc: StoryArc, *, today: date_type,
) -> StoryArcBeat | None:
    """Earliest due beat whose scene is *about* the player, if any.

    OP3. ``central`` means the scene has
    no content without the player, so the autonomous scan walks past it
    (OP2-B) and it simply sits there — this is the beat the character
    might reach out about.

    Why "due" (``scheduled_date <= today``) rather than the forward feed
    the arc block already carries: ``forward_beats`` is anchored at
    ``>= today`` and therefore *drops* a beat the day after it comes due
    — precisely when it has been waiting longest and an invitation
    matters most. The due window mirrors ``StoryArcService.
    _due_pending_beats`` so "waiting" means the same thing here as it
    does to the code that plays beats.

    ``BEAT_PENDING`` only, again matching the play-eligibility filter:
    ``active`` would mean the scene is already running, and inviting
    someone into a scene they are mid-way through is not an invitation.
    """
    due = [
        beat
        for beat in arc.beats
        if beat.status == BEAT_PENDING
        and beat.scheduled_date <= today
        and beat.operator_position == OPERATOR_POSITION_CENTRAL
    ]
    if not due:
        return None
    due.sort(key=lambda beat: (beat.scheduled_date, beat.sequence))
    return due[0]


def _compute_idle_minutes(
    character: Character, now: datetime,
) -> float | None:
    last = character.state.last_active_at
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0.0, (now - last).total_seconds() / 60.0)


def _count_unanswered_streak(
    recent_sent_attempts: tuple[ProactiveAttempt, ...],
    *,
    idle_minutes: float | None,
    now: datetime,
) -> int:
    """Leading run of SENT pushes the user has not replied to.

    Walks newest→oldest and counts attempts that went out *after* the
    user last spoke (so they remain unanswered), stopping at the first
    one the user replied to. Uses the exact same "replied iff the user
    spoke after the push" test as the prompt's per-message reply tag, so
    the streak count and the "（對方還沒回）" tags can never disagree.
    Returns 0 when there is no prior conversation (``idle_minutes`` is
    None) — silence from a user who never spoke is not "being ignored".
    """
    if idle_minutes is None:
        return 0
    streak = 0
    for attempt in recent_sent_attempts:  # newest first
        elapsed_min = (now - attempt.decided_at).total_seconds() / 60.0
        if idle_minutes < elapsed_min:
            # User spoke after this push → they replied → run ends.
            break
        streak += 1
    return streak


def _timezone_for_operator(
    operator: OperatorProfile | None,
    fallback: tzinfo,
) -> tzinfo:
    if operator is None:
        return fallback
    try:
        return timezone_for_id(getattr(operator, "timezone_id", None))
    except Exception:  # pragma: no cover - defensive
        return fallback


def _language_for_operator(operator: OperatorProfile | None) -> str:
    if operator is None:
        return "zh-TW"
    lang = (operator.primary_language or "").strip()
    return lang or "zh-TW"


def _last_player_turn(messages: list[Message]) -> Message | None:
    """The player's own most recent message, or ``None``.

    Returns the message rather than its text so the caller cannot date the
    quote from anything but the turn it came out of — the defect this
    signature exists to make unrepresentable (see ``_RecentDialogue``).

    Read off the same sanitised list the summariser saw — quoting text the
    tolerance filter removed would put back, in the judge's prompt, exactly
    what was taken out of the decider's.

    Only ``USER`` turns qualify: the character's own last line is already
    dated separately in the same block, and a system notice was never
    something the player said. Whether the turn carries a usable
    ``created_at`` is *not* a search criterion — an undated newest turn
    must cost the block its quote, not silently promote an older line into
    the 「最後一次說話」 slot it does not occupy.
    """
    for message in reversed(messages):
        if message.role is not MessageRole.USER:
            continue
        if not (message.content or "").strip():
            continue
        return message
    return None


def _render_fresh_dialogue_tail(
    character: Character,
    messages: list[Message],
    *,
    now: datetime,
    local_tz: tzinfo,
) -> str:
    """The last few turns verbatim, each stamped with when it happened.

    Deterministic on purpose — this block is the control against which
    the LLM summary above it can be checked, so it must not itself pass
    through a model. Rendering goes through the summariser's own
    ``render_dialogue_line`` so both blocks quote the same clock.

    Kept to a handful of turns: the point is to pin *recency* (「這句是十
    分鐘前說的，不是昨天」), and the summary already carries the wider
    thread. Returns ``""`` when nothing has text, so the caller can
    concatenate unconditionally.
    """
    tail = [
        message for message in messages if (message.content or "").strip()
    ][-_FRESH_DIALOGUE_TAIL_TURNS:]
    if not tail:
        return ""
    lines = [
        render_dialogue_line(character, message, now=now, local_tz=local_tz)
        for message in tail
    ]
    return (
        "最近幾則對話原文（含實際發生時間；時間一律以這裡的標註為準，"
        "不要自行推測是哪一天）：\n" + "\n".join(lines)
    )


def _format_memories(
    items: list[MemoryItem], *, now: datetime | None = None,
) -> str:
    """Recall block for the proactive decider / composer.

    KB7: this used to hand-roll ``- [{kind}] {content}{time}`` — its own
    time-tag helper, no participant tag, and (once the disclosure ledger
    existed) no way to tell the character's private week from a shared
    one. Proactive is the main channel of the 2026-08-25 incident family
    — she reaches out unprompted, so a memory the player never witnessed
    is exactly what gets opened with — so the renderer converges on the
    shared one. The kind bucket survives as ``include_kind``: the decider
    weighs 「[semantic] 他住淡水」 differently from 「[episodic] 昨天一起去」.
    """
    if not items:
        return ""
    return "\n".join(
        format_memory_line(item, now=now, include_kind=True)
        for item in items[:_MEMORY_RECALL_LIMIT]
    )


def _goal_age_tag(
    goal: CharacterGoal, now: datetime | None, local_tz: tzinfo,
) -> str:
    """Append 「（3 天前立下）」 so a goal reads as a dated statement.

    Goals are written in the moment ("陪使用者**明早**一起出門吃刨冰") and
    then live for weeks; without an age the decider has no way to tell a
    promise made this morning from one whose 「明早」 passed three days ago.
    Sibling of ``memory_lines.memory_time_tag``, but counted in civil days
    rather than elapsed hours — commitments expire on calendar boundaries.
    Empty when the reference clock or the stamp is missing, leaving the
    line exactly as before.
    """
    label = format_civil_days_ago_label(
        getattr(goal, "created_at", None), now, local_tz=local_tz,
    )
    return f"（{label}立下）" if label else ""


def _format_intention_skip_reason(
    decision: ProactiveIntentionDecision,
) -> str:
    parts = ["intention skipped"]
    if decision.reason.strip():
        parts.append(decision.reason.strip())
    if decision.best_timing.strip():
        parts.append(f"best_timing={decision.best_timing.strip()}")
    if decision.risk.strip():
        parts.append(f"risk={decision.risk.strip()}")
    reason = " | ".join(parts)
    if len(reason) > 500:
        return reason[:497].rstrip() + "..."
    return reason


def _overlay_due_intents(active: tuple, due: tuple) -> tuple:
    """Show the judge the alarms as they stood when they rang.

    ``_spend_due_revisits`` clears ``revisit_at`` the moment an alarm buys
    a gate pass — before the context is assembled — so every row read
    back from the store is alarm-less and the judge's 「已經到了」 line can
    never render on the very tick it exists for. Overlaying the pre-spend
    snapshots by id restores that signal for this evaluation only; the
    store stays cleared, so the exemption is still single-use.

    Rows that rang but fell outside the ``list_active`` window lead the
    result — they are the reason this tick exists at all.
    """
    if not due:
        return active
    snapshots = {intent.id: intent for intent in due}
    merged = [snapshots.pop(intent.id, intent) for intent in active]
    return (*snapshots.values(), *merged)


def _parse_revisit_at(
    raw: str | None,
    *,
    local_tz: tzinfo,
    now: datetime,
) -> datetime | None:
    """Parse ``ProactiveIntentionDecision.revisit_at_iso`` to aware UTC.

    Same tolerance contract as ``chat_service._parse_promise_datetime``:
    a naive value is read in the operator's civil timezone, then stored
    as UTC. Anything unparseable — or already in the past — collapses to
    ``None``, so a hallucinated or stale timestamp can never hand out a
    cooldown exemption; the motive is still parked, just without an
    alarm.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= ensure_utc(now):
        return None
    return parsed


def _requires_user_started_interaction(trigger: ProactiveTrigger) -> bool:
    return trigger not in (
        ProactiveTrigger.PENDING_FOLLOW_UP,
        ProactiveTrigger.SCHEDULED_PROMISE,
    )


def _seed_allows_pre_message_proactive(
    seed: CharacterOperatorRelationshipSeed | None,
) -> bool:
    """IR4: gate on explicit operator permission alone.

    ``proactive_cadence_hint`` used to be co-required, which meant a
    player who checked "can find me first" but left the frequency hint
    blank got permanently gated — nothing in creation UI ever said the
    checkbox alone wasn't enough. The cadence hint is now purely
    advisory: when present it still renders into the prompt (see
    ``render_initial_relationship_seed_lines``) so the character has a
    frequency/timing steer; when absent the character judges cadence on
    its own, same as any other unset relationship-seed field.
    """
    if seed is None:
        return False
    return bool(seed.proactive_permission)


def _proactive_event_valence(outcome: ProactiveOutcome) -> float:
    """How this tick coloured the character's mood, if at all.

    ``QUALITY_WITHHELD`` is absent on purpose and falls to the neutral
    default: the character wrote its message and, as far as it knows, has
    no idea a machine refused the prose. Giving it the decider's own
    ``-0.05`` would teach the character to feel bad about our bug.
    """
    values = {
        ProactiveOutcome.SENT: 0.15,
        ProactiveOutcome.ERRORED: -0.2,
        ProactiveOutcome.INTENTION_SKIPPED: -0.05,
        ProactiveOutcome.DECIDER_SKIPPED: -0.05,
    }
    return values.get(outcome, 0.0)


def _proactive_event_intensity(outcome: ProactiveOutcome) -> float:
    values = {
        ProactiveOutcome.SENT: 0.25,
        ProactiveOutcome.ERRORED: 0.2,
        ProactiveOutcome.INTENTION_SKIPPED: 0.12,
        ProactiveOutcome.DECIDER_SKIPPED: 0.12,
        ProactiveOutcome.GATE_BLOCKED: 0.08,
        ProactiveOutcome.NO_BINDING: 0.08,
        ProactiveOutcome.DISABLED: 0.05,
    }
    return values.get(outcome, 0.1)


def _proactive_event_evidence(attempt: ProactiveAttempt) -> str:
    parts = [
        f"trigger={attempt.trigger.value}",
        f"outcome={attempt.outcome.value}",
    ]
    if attempt.reason:
        parts.append(f"reason={attempt.reason}")
    if attempt.message:
        parts.append(f"message={attempt.message[:80]}")
    return " | ".join(parts)[:240]


def _proactive_diversity_evidence(
    context: ProactiveContext,
) -> ReplyDiversityEvidence:
    lines = tuple(
        attempt.message or ""
        for attempt in context.recent_sent_attempts[:8]
        if attempt.message
    )
    return ReplyDiversityEvidence(
        assistant_line_count=len(lines),
        self_repetition_hint="",
        phrase_frequency_lines=(
            (
                "recent_sent_attempts 已提供近期主動訊息；請判斷是否重複同一目的或措辭。"
            )
            if lines else ()
        ),
    )


def _proactive_temporal_lines(
    context: ProactiveContext,
    *,
    last_player_message: str = "",
    last_player_at: datetime | None = None,
) -> tuple[str, ...]:
    """The 時間座標 block for one push: now, plus what it is answering.

    Proactive is the surface where a stale concern is most likely to
    surface as a fresh one — nobody wrote to the character, so the draft
    is built entirely out of material that already happened. The player's
    last turn is the anchor that matters (the 2026-08-27 incident was a
    「要回家了」 from sixteen hours earlier answered as if it were
    minutes), and the character's own last push is second: re-asking the
    same question a few hours apart is the same defect seen from the
    other side.

    Two *different* facts about the player, deliberately kept apart:

    * **說話** — ``last_player_message`` with ``last_player_at``, both read
      off the same ``Message`` by the dialogue load this tick already did
      (G2-3). This is the material a stale push is built out of, so it is
      the anchor the ``temporal_inconsistency`` rubric actually reads:
      「我要回家了」 sixteen hours ago is a concern that expired with the
      gap, 「下週要搬家」 is one that did not, and no timestamp alone
      separates them. The quote renders **only** with its own instant;
      dating it from anything else is the defect this parameter pair
      replaced (see ``_RecentDialogue``).
    * **互動** — ``idle_minutes``, i.e. ``last_active_at``. In cloud mode
      分歧劇場／起幕／融合故事 advance it without a ``USER`` message, so it
      answers "is the player around", never "when did they last speak".
      Kept because it is the only reading available at all when no
      summariser is wired (self-host, where the two coincide anyway), and
      labelled for what it is so the judge cannot read it as speech.

    ``idle_minutes is None`` means the pair never interacted; the block
    then simply omits that anchor, and the rubric pins the axis false on a
    block holding nothing but 現在 — the correct failure mode.
    """
    events: list[TemporalEvent] = []
    if last_player_message.strip() and last_player_at is not None:
        events.append(quoted_event(
            "玩家最後一次說話", last_player_message, last_player_at,
        ))
    if context.idle_minutes is not None:
        events.append((
            "玩家最後一次互動（不一定是說話）",
            context.now - timedelta(minutes=max(context.idle_minutes, 0.0)),
        ))
    last_push = next(
        (
            attempt for attempt in context.recent_sent_attempts
            if attempt.message and attempt.message.strip()
        ),
        None,
    )
    if last_push is not None:
        events.append(
            quoted_event("你上次主動說", last_push.message or "", last_push.decided_at),
        )
    return render_temporal_context_lines(
        now=context.now,
        local_tz=context.local_tz,
        events=events,
    )


def _decision_tool_prompt_lines(
    decision: ProactiveDecision,
) -> tuple[str, ...]:
    """The tool prompts this draft ships with, labelled by their source.

    Without them the judge is shown an empty 「隨附工具 prompt」 column while
    the prose says 「拍了張照片給你」 — which is clause (a) of the
    ``tool_prompt_defect`` rubric read literally, so *every* push carrying a
    ``generate_image`` call hard-failed and, under background policy, was
    withheld. The gate was systematically deleting exactly the messages it
    was least entitled to.

    Every call is rendered, not just the visual ones: the dispatcher cannot
    ask a ``ToolCall`` what it renders (the capability lives on the tool, not
    the call), and guessing from the name is the inference the tool contract
    explicitly forbids. Showing a labelled ``web_search query: …`` line costs
    the judge one line it can identify and dismiss; hiding a real image
    prompt behind a wrong guess costs the player the message.

    One line per argument rather than one per call, following the feed's
    ``"image_prompt: …"`` idiom: the rubric also has to be able to see a
    prompt that is malformed, and a JSON blob squashed onto a single line
    reads as structure noise instead of as the prompt it is.
    """
    lines: list[str] = []
    for call in decision.tool_calls:
        for key, value in (call.arguments or {}).items():
            rendered = "" if value is None else str(value).strip()
            if not rendered:
                continue
            lines.append(f"{call.name} {key}: {rendered}")
    return tuple(lines)


def _reportable_verdict(
    review: "OutputQualityReview[ProactiveDecision]",
) -> NoveltyVerdict | None:
    """Which of the band's two verdicts the audit row should describe.

    The re-review's, because that is the draft that shipped — unless it
    fired nothing, in which case it has nothing to say and the first
    verdict is the only record of *why* this tick regenerated at all. The
    same "hard explains the disposal, the rest explains the feedback"
    rule the orchestrator's own log line follows, and what keeps a
    ``soft_recovered`` row still naming the axis that forced the retry
    instead of reading like an untouched pass.
    """
    final = review.final_verdict
    if final is not None and fired_axes(final):
        return final
    return review.first_verdict or final


def _quality_gate_metadata(
    verdict: NoveltyVerdict | None,
    *,
    enabled: bool,
    retry_count: int,
    outcome: str = "",
) -> dict[str, object]:
    """The gate's own row on the attempt / turn record.

    ``outcome`` and the four hard axes are what make this row answer the
    question an operator actually arrives with — *did this tick send, and
    if not, why* — rather than only "which soft axis was unhappy". Without
    them a ``hard_skipped`` push and a soft best-effort one look alike in
    the audit trail, which is how the 2026-08-26 defect stayed invisible.
    """
    metadata = dict(verdict.gate_metadata) if verdict is not None else {}
    return {
        "enabled": enabled,
        "evaluated": verdict is not None,
        "outcome": outcome,
        "passes": True if verdict is None else verdict.passes,
        # Every axis the contract declares, soft and hard alike. Written by
        # iteration rather than by hand because this row is silent on
        # omission: an axis the judge fires but this dict forgot simply
        # never reaches the audit trail, and nothing goes red.
        **{
            axis: False if verdict is None else getattr(verdict, axis)
            for axis in ALL_AXES
        },
        "hard_fail": False if verdict is None else verdict.hard_fail,
        "feedback": "" if verdict is None else verdict.feedback,
        "retry_count": retry_count,
        "provider_id": metadata.get("provider_id", ""),
        "model_id": metadata.get("model_id", ""),
        "latency_ms": metadata.get("latency_ms"),
        "prompt_tokens": metadata.get("prompt_tokens"),
        "completion_tokens": metadata.get("completion_tokens"),
        "error": metadata.get("error"),
    }


def _register_profile_metadata(profile, *, enabled: bool) -> dict[str, object] | None:
    if not enabled and profile is None:
        return None
    metadata = dict(profile.metadata) if profile is not None else {}
    return {
        "enabled": enabled,
        "applied": profile is not None,
        "axes": dict(profile.axes) if profile is not None else {},
        "confidence": 0.0 if profile is None else profile.confidence,
        "vulnerable_disclosure": (
            False if profile is None else profile.vulnerable_disclosure
        ),
        "note": "" if profile is None else profile.note,
        "provider_id": metadata.get("provider_id", ""),
        "model_id": metadata.get("model_id", ""),
        "error": metadata.get("error"),
    }


def _diversity_metadata(
    evidence: ReplyDiversityEvidence | None,
) -> dict[str, object] | None:
    if evidence is None:
        return None
    return {
        "assistant_line_count": evidence.assistant_line_count,
        "max_self_similarity": evidence.max_self_similarity,
        "mean_self_similarity": evidence.mean_self_similarity,
        "has_frequency_evidence": evidence.has_frequency_evidence,
        "metadata": dict(evidence.metadata),
    }


# Type alias for the optional schedule resolver callback.
# Defined here (not as a Protocol import) to keep the file self-contained.
from typing import Awaitable, Callable  # noqa: E402

ScheduleResolver = Callable[
    [Character, datetime],
    Awaitable[tuple[object, list, object]],
]
