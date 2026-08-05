"""Phase 5 per-kind due-time job registry (HOSTED_CORE_SCALING §4.2 / §4.3 / §5).

Phase 3 packaged all per-character background work into a single ``character_tick``
job the coordinator enqueued for *every* active character *every* bucket (a full
table scan). Phase 5 replaces that with **per-kind self-continuing due jobs**: each
class of activity carries its own ``due_at`` cadence, and each job — on completion —
computes its own next due time and enqueues the next occurrence (a self-chain). The
coordinator no longer scans; it only runs a low-frequency *reconcile* that reseeds a
chain when one is missing (new character, thaw, or a broken chain).

This module is the **registry** only: it declares each character-scoped kind with

* its **priority** (§5: pending follow-up=1〔next 段〕, beat / schedule repair=2,
  proactive=3, feed=4, upkeep / maintenance=5),
* its **capability** (``llm`` / ``image`` / ``none``) so the next 段 can apply
  capability-specific concurrency caps (§5) without re-deriving it,
* its **base cadence** (seconds) — the interval the self-chain advances by before the
  B1 knob multiplier is applied (:mod:`due_job_scheduler`),
* which **knob multiplier** gate scales that cadence (``proactive`` /
  ``background`` / ``none`` — §4.3), and the optional
  :class:`BackgroundActivityClass` a handler re-checks at apply time,
* whether it is **event-driven** (``feed_comment_reply`` prefers a high-priority job
  enqueued at the player-comment write point; the chain is only a fallback recheck).

Nothing here runs providers or mutates state — it is a pure, importable declaration
consumed by the calculator, the reconciler and the handler dispatcher. It is
**distributed-only** by construction: the embedded (self-host) scheduler never reads
this registry (§ red line — embedded behaviour is byte-identical).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from kokoro_link.contracts.background_activity_gate import BackgroundActivityClass


class JobCapability(StrEnum):
    """Provider capability a kind's handler predominantly consumes.

    Drives the next 段's capability-specific concurrency caps (§5:
    background LLM=3, background image/video=1). ``none`` = DB-only work
    (subscription / freeze recheck, rest recovery) that needs no provider slot.
    """

    LLM = "llm"
    IMAGE = "image"
    NONE = "none"


class KnobGate(StrEnum):
    """Which B1 runtime-profile multiplier scales a kind's base cadence (§4.3).

    ``PROACTIVE`` → ``effective_proactive_multiplier``; ``BACKGROUND`` →
    ``effective_background_multiplier``; ``NONE`` → no scaling (multiplier 1,
    cheap DB-only cadence that should not be down-shifted)."""

    PROACTIVE = "proactive"
    BACKGROUND = "background"
    NONE = "none"


# -- event-driven one-shot kinds (not per-character self-continuing chains) --- #

#: A busy-defer / scheduled-promise follow-up whose release instant
#: (``PendingFollowUp.scheduled_for``) is precisely known at write time, so the
#: chat write point enqueues a single high-priority (priority 1) one-shot job due
#: at that instant instead of a whole-table tick scanning ``list_due`` every
#: bucket. This kind is **not chained** (``chained=False``): a release either
#: fires once or — when the character is still busy / frozen — leaves the row
#: queued for the low-frequency reconcile to re-enqueue; there is no self-chain.
PENDING_FOLLOW_UP_RELEASE_KIND = "pending_follow_up_release"


#: The post-turn processor (memory / state / schedule / arc / promise extraction)
#: that runs after a chat turn completes. In embedded mode it stays an in-process
#: fire-and-forget task off the chat write point (§ red line); in distributed mode
#: the chat write point enqueues ONE one-shot job due immediately, so the LLM
#: extraction is offloaded to a worker instead of running inside the API request.
#: Not chained (``chained=False``): each chat turn mints its own job keyed by the
#: turn record id. The payload carries ids / flags ONLY — the worker re-reads the
#: conversation to rebuild the turn text (§3.1 / §11 payload red line).
POST_TURN_KIND = "post_turn"


# -- character-scoped kinds (the character_tick 8-step split) ---------------- #

BEAT_DUE_KIND = "beat_due"
SCHEDULE_MAINTENANCE_KIND = "schedule_maintenance"
SCHEDULE_WEATHER_VET_KIND = "schedule_weather_vet"
MEMORIALIZE_KIND = "memorialize"
FEED_COMPOSE_KIND = "feed_compose"
FEED_COMMENT_REPLY_KIND = "feed_comment_reply"
PROACTIVE_EVALUATE_KIND = "proactive_evaluate"
CHARACTER_UPKEEP_KIND = "character_upkeep"
GOAL_REVIEW_KIND = "goal_review"
STORY_SCENE_TIMEOUT_KIND = "story_scene_timeout"


# -- social (cross-character) kinds — the social_tick split (§13) ------------ #
#
# The global ``social_tick`` scanned the whole working set every bucket for three
# distinct cross-character activities. §13 replaces that with per-target self
# -continuing chains so the last whole-table sweep disappears:
#
# * ``encounter_tick`` — the schedule unit is a *relationship* (canonical pair);
#   its chain key is the canonical pair id, and its handler takes an atomic
#   two-character pair lease before any LLM (§6.1).
# * ``persona_dream`` / ``peer_knowledge`` — the unit is a single character
#   (dream is per (character, owner-operator); a character has exactly one owner),
#   so they ride the same per-character chain machinery as the character_tick
#   split but delegate to the SocialTickExecutor steps instead.
ENCOUNTER_TICK_KIND = "encounter_tick"
PERSONA_DREAM_KIND = "persona_dream"
PEER_KNOWLEDGE_KIND = "peer_knowledge"


@dataclass(frozen=True, slots=True)
class KindSpec:
    """Static registration for one per-kind due job.

    ``handler`` is the logical step name the dispatcher (:mod:`due_job_handlers`)
    maps to a :class:`CharacterTickExecutor` step so the tick body stays the single
    source of truth (抽共用而非複製) — no per-kind copy of the step logic.
    """

    kind: str
    priority: int
    capability: JobCapability
    base_interval_seconds: float
    handler: str
    knob_gate: KnobGate = KnobGate.NONE
    activity_class: BackgroundActivityClass | None = None
    character_scoped: bool = True
    chained: bool = True
    event_driven: bool = False
    #: A pair-scoped chain (``encounter_tick``): the chain key is a canonical pair
    #: id (relationship id), not a single character id, and the handler takes an
    #: atomic two-character pair lease. Mutually exclusive with
    #: ``character_scoped`` for the reconciler's diff logic.
    pair_scoped: bool = False


#: The full character-scoped kind registry. Every entry is chained (self-continuing)
#: and reseeded by the reconciler when its chain is missing.
CHARACTER_KIND_REGISTRY: dict[str, KindSpec] = {
    BEAT_DUE_KIND: KindSpec(
        kind=BEAT_DUE_KIND,
        priority=2,
        capability=JobCapability.LLM,
        # Beats fire at precisely-known scheduled times; the chain advance passes
        # the next beat instant as ``explicit_next_due`` and this interval is only
        # the fallback recheck cadence when no future beat is known.
        base_interval_seconds=300.0,
        handler="beat_due",
        knob_gate=KnobGate.NONE,
    ),
    SCHEDULE_MAINTENANCE_KIND: KindSpec(
        kind=SCHEDULE_MAINTENANCE_KIND,
        priority=2,
        capability=JobCapability.LLM,
        # Daily stable jitter + four-day horizon repair (§4.1). The handler is the
        # eager schedule-ensure step; horizon maintenance folds into this chain.
        base_interval_seconds=86_400.0,
        handler="schedule_maintenance",
        knob_gate=KnobGate.BACKGROUND,
    ),
    SCHEDULE_WEATHER_VET_KIND: KindSpec(
        kind=SCHEDULE_WEATHER_VET_KIND,
        priority=2,
        capability=JobCapability.LLM,
        # Intra-day correction of today's planned prose against the sky right
        # now. It CANNOT ride the daily ``schedule_maintenance`` chain: the
        # whole point is catching a sky that turned since planning time, so a
        # once-a-day cadence would reproduce the very bug it fixes. 30 minutes
        # is roughly how fast a coarse WMO condition phrase moves; the service's
        # own (head activity, condition) gate makes every pass in between free,
        # so this cadence bounds latency, not cost.
        base_interval_seconds=1_800.0,
        handler="schedule_weather_vet",
        knob_gate=KnobGate.BACKGROUND,
    ),
    MEMORIALIZE_KIND: KindSpec(
        kind=MEMORIALIZE_KIND,
        priority=5,
        capability=JobCapability.LLM,
        # Schedule-completion window sweep; cheap when nothing is due.
        base_interval_seconds=3_600.0,
        handler="memorialize",
        knob_gate=KnobGate.NONE,
    ),
    FEED_COMPOSE_KIND: KindSpec(
        kind=FEED_COMPOSE_KIND,
        priority=4,
        capability=JobCapability.IMAGE,
        # 90-minute cooldown; the daily cap + background multiplier defer via due_at.
        base_interval_seconds=5_400.0,
        handler="feed_compose",
        knob_gate=KnobGate.BACKGROUND,
        activity_class=BackgroundActivityClass.FEED_COMPOSE,
    ),
    FEED_COMMENT_REPLY_KIND: KindSpec(
        kind=FEED_COMMENT_REPLY_KIND,
        priority=4,
        capability=JobCapability.LLM,
        # Event-driven primary (enqueue at the player-comment write point); the
        # chain is a short fallback recheck so a missed event still gets answered.
        base_interval_seconds=300.0,
        handler="feed_comment_reply",
        knob_gate=KnobGate.NONE,
        event_driven=True,
    ),
    PROACTIVE_EVALUATE_KIND: KindSpec(
        kind=PROACTIVE_EVALUATE_KIND,
        priority=3,
        capability=JobCapability.LLM,
        # Cooldown / quiet-hours / daily-cap / runtime multiplier all apply; the
        # multiplier scales the cadence here, the rest defer via due_at.
        base_interval_seconds=300.0,
        handler="proactive_evaluate",
        knob_gate=KnobGate.PROACTIVE,
    ),
    GOAL_REVIEW_KIND: KindSpec(
        kind=GOAL_REVIEW_KIND,
        priority=5,
        capability=JobCapability.LLM,
        # Daily convergence pass over the character's medium-term goals
        # (CF2 / COMMITMENT_LIFECYCLE §2 P2a). Same cadence and rationale as
        # ``schedule_maintenance``: goal drift is a once-a-day concern, and
        # the review must happen even for an account that never opens the
        # chat — the turn-count trigger cannot reach those. The service's own
        # per-(character, civil day) DB claim makes the pass idempotent, so
        # a re-run inside the same day costs one failed insert, not one LLM
        # call, and a missed day is picked up by the next occurrence.
        base_interval_seconds=86_400.0,
        handler="goal_review",
        knob_gate=KnobGate.BACKGROUND,
    ),
    STORY_SCENE_TIMEOUT_KIND: KindSpec(
        kind=STORY_SCENE_TIMEOUT_KIND,
        # Priority 2 — the "precisely-timed" tier, alongside beat_due, and
        # for the same reason: an idle scene's deadline is known exactly
        # (last activity + the configured window), so the chain carries an
        # ``explicit_next_due`` rather than drifting on rechecks. It also
        # produces player-visible output the player already paid for
        # (STORY_SCENE_PLAN §3.4 #2), which puts it above the deferrable
        # feed / upkeep tiers.
        priority=2,
        # The wrap-up is a model call (narration + the canon summary), so
        # it counts against the §5 background LLM ceiling like beat_due.
        capability=JobCapability.LLM,
        # Fallback recheck only, for the overwhelmingly common case of a
        # character with no live scene: one indexed read per hour. When a
        # scene IS open the explicit due time takes over entirely.
        base_interval_seconds=3_600.0,
        handler="story_scene_timeout",
        # NOT background-gated. The idle window is the promise that a paid
        # opening always produces something; letting a down-shifted idle
        # account stretch that window would leave 起幕 blocked for the one
        # player most likely to come back and press it.
        knob_gate=KnobGate.NONE,
    ),
    CHARACTER_UPKEEP_KIND: KindSpec(
        kind=CHARACTER_UPKEEP_KIND,
        priority=5,
        capability=JobCapability.NONE,
        # DB-only backstop: subscription / freeze recheck + rest recovery. Kept at a
        # steady cheap cadence (never down-shifted) so a frozen/lapsed character is
        # still observed even when every LLM chain has stopped.
        base_interval_seconds=3_600.0,
        handler="character_upkeep",
        knob_gate=KnobGate.NONE,
    ),
}


#: Event-driven one-shot kinds. Registered so :func:`kind_spec` can report their
#: priority / capability, but deliberately **excluded** from
#: :func:`character_chain_kinds` (the reconciler's per-character reseed set) and
#: from :func:`kinds_for_capability` (the §5 background caps) — a one-shot,
#: player-owed release must not be reseeded per-character nor throttled behind the
#: background LLM ceiling. ``chained=False`` + ``character_scoped=False`` is how the
#: registry expresses "fire once at ``due_at``, no self-chain".
EVENT_ONE_SHOT_KIND_REGISTRY: dict[str, KindSpec] = {
    PENDING_FOLLOW_UP_RELEASE_KIND: KindSpec(
        kind=PENDING_FOLLOW_UP_RELEASE_KIND,
        priority=1,
        capability=JobCapability.LLM,
        # One-shot: due exactly at the row's ``scheduled_for``. The interval is
        # unused (the chain never advances) but kept 0 to make that explicit.
        base_interval_seconds=0.0,
        handler="pending_follow_up_release",
        knob_gate=KnobGate.NONE,
        character_scoped=False,
        chained=False,
        event_driven=True,
    ),
    POST_TURN_KIND: KindSpec(
        kind=POST_TURN_KIND,
        # Priority 3 = the "per-interaction LLM aftermath" tier, matching
        # ``proactive_evaluate``: below the player-owed follow-up release (1) and
        # the precise-time beat / schedule repair (2), but ABOVE the deferrable
        # background feed / social (4) and DB upkeep (5). Rationale: post-turn
        # output (memory / state) feeds the quality of the NEXT live turn, so it
        # is latency-sensitive relative to genuinely-background cadences, yet it
        # is neither an owed outbound message nor a scheduled character event.
        priority=3,
        capability=JobCapability.LLM,
        # One-shot: due immediately at the chat write point. The interval is
        # unused (the chain never advances) but kept 0 to make that explicit.
        base_interval_seconds=0.0,
        handler="post_turn",
        knob_gate=KnobGate.NONE,
        character_scoped=False,
        chained=False,
        event_driven=True,
    ),
}


#: The cross-character self-continuing kinds (the ``social_tick`` split). Every
#: entry is chained and reseeded by the social reconciler when its chain is
#: missing (new character / new relationship, thaw, or a broken chain).
#:
#: ``encounter_tick`` is ``pair_scoped`` — its chain key is a canonical pair id
#: and its handler holds a two-character lease across the LLM run. ``persona_dream``
#: and ``peer_knowledge`` are ordinary character-scoped chains. All three ride the
#: BACKGROUND multiplier so an idle / down-shifted account slows every social
#: cadence identically to feed compose.
SOCIAL_KIND_REGISTRY: dict[str, KindSpec] = {
    ENCOUNTER_TICK_KIND: KindSpec(
        kind=ENCOUNTER_TICK_KIND,
        priority=4,
        capability=JobCapability.LLM,
        # Same cadence the embedded scheduler throttled encounter PLANNING at
        # (``_DEFAULT_ENCOUNTER_PLAN_INTERVAL_SECONDS``); the background
        # multiplier scales it and the pair's own 6h min-gap / schedule-slot
        # search decide whether a meetup actually materialises.
        base_interval_seconds=1_800.0,
        handler="encounter_tick",
        knob_gate=KnobGate.BACKGROUND,
        activity_class=BackgroundActivityClass.ENCOUNTER_RUN,
        character_scoped=False,
        pair_scoped=True,
    ),
    PERSONA_DREAM_KIND: KindSpec(
        kind=PERSONA_DREAM_KIND,
        priority=4,
        capability=JobCapability.LLM,
        # A recheck cadence; the dream's own quiet-hours + min-interval +
        # pending-count gate (``should_run_now``) decides whether it runs.
        base_interval_seconds=1_800.0,
        handler="persona_dream",
        knob_gate=KnobGate.BACKGROUND,
        activity_class=BackgroundActivityClass.PERSONA_DREAM,
    ),
    PEER_KNOWLEDGE_KIND: KindSpec(
        kind=PEER_KNOWLEDGE_KIND,
        priority=4,
        capability=JobCapability.LLM,
        # Same cadence the embedded scheduler throttled peer-knowledge
        # consolidation at (``_DEFAULT_PEER_KNOWLEDGE_INTERVAL_SECONDS``).
        base_interval_seconds=3_600.0,
        handler="peer_knowledge",
        knob_gate=KnobGate.BACKGROUND,
        activity_class=BackgroundActivityClass.PEER_KNOWLEDGE,
    ),
}


def kind_spec(kind: str) -> KindSpec | None:
    """Return the :class:`KindSpec` for ``kind`` across all registries.

    Covers the per-character self-continuing chains, the cross-character social
    chains *and* the event-driven one-shot kinds; ``None`` when ``kind`` is
    unregistered."""
    return (
        CHARACTER_KIND_REGISTRY.get(kind)
        or SOCIAL_KIND_REGISTRY.get(kind)
        or EVENT_ONE_SHOT_KIND_REGISTRY.get(kind)
    )


def is_character_chain_kind(kind: str) -> bool:
    """True only for a per-character self-continuing chain kind.

    The distributed runner routes a claimed job to the :class:`CharacterKindHandler`
    (step + self-chain) only for these; one-shot event kinds route elsewhere."""
    return kind in CHARACTER_KIND_REGISTRY


def character_chain_kinds() -> tuple[str, ...]:
    """The character-scoped kinds the reconciler reseeds, in a stable order."""
    return tuple(CHARACTER_KIND_REGISTRY.keys())


def is_social_chain_kind(kind: str) -> bool:
    """True only for a cross-character social self-continuing chain kind.

    The distributed runner routes these to the :class:`SocialKindHandler`
    (pair lease / character step + self-chain) instead of the tick body."""
    return kind in SOCIAL_KIND_REGISTRY


def is_pair_chain_kind(kind: str) -> bool:
    """True only for a pair-scoped social chain (``encounter_tick``).

    A pair chain's scope key is a canonical pair id and its handler holds a
    two-character lease; the single-character path never applies."""
    spec = SOCIAL_KIND_REGISTRY.get(kind)
    return spec is not None and spec.pair_scoped


def social_chain_kinds() -> tuple[str, ...]:
    """Every social self-continuing kind, in a stable order."""
    return tuple(SOCIAL_KIND_REGISTRY.keys())


def social_character_chain_kinds() -> tuple[str, ...]:
    """The character-scoped social kinds the reconciler reseeds per character."""
    return tuple(
        kind for kind, spec in SOCIAL_KIND_REGISTRY.items() if not spec.pair_scoped
    )


def social_pair_chain_kinds() -> tuple[str, ...]:
    """The pair-scoped social kinds the reconciler reseeds per relationship."""
    return tuple(
        kind for kind, spec in SOCIAL_KIND_REGISTRY.items() if spec.pair_scoped
    )


# -- capability concurrency caps (§5) ---------------------------------------- #

#: Worker-layer global concurrency ceiling per provider capability (§5): a
#: background LLM job runs at most 3-wide, a background image/video job 1-wide.
#: ``JobCapability.NONE`` is uncapped (DB-only work). Env-overridable via the
#: ``YURALUME_BG_CAP_LLM`` / ``YURALUME_BG_CAP_IMAGE`` knobs; the self-host
#: (embedded) path never reads these — caps only bind the distributed claim.
DEFAULT_CAPABILITY_CAPS: dict[str, int] = {
    JobCapability.LLM.value: 3,
    JobCapability.IMAGE.value: 1,
}


def kinds_for_capability(capability: str) -> tuple[str, ...]:
    """The registered chain kinds whose handler consumes ``capability``.

    The queue-claim capability filter (:meth:`BackgroundJobQueuePort.claim`)
    counts in-flight jobs of a capped capability by their kind, so it needs the
    concrete kind set for each capability value (``llm`` / ``image``). Covers both
    the character-scoped and cross-character social chains — a background encounter
    / dream / peer consolidation is an LLM job and must count against the §5 LLM
    ceiling just like proactive / beat. Returned in a stable order (character kinds
    first, then social); empty for an unknown / uncapped capability."""
    return tuple(
        kind
        for registry in (CHARACTER_KIND_REGISTRY, SOCIAL_KIND_REGISTRY)
        for kind, spec in registry.items()
        if spec.capability.value == capability
    )


def capability_kind_sets(
    caps: dict[str, int] | None = None,
) -> tuple[tuple[str, int, frozenset[str]], ...]:
    """Materialise ``(capability, cap, kinds)`` triples for the active caps.

    Skips capabilities with no registered kinds or a non-positive cap. Used by
    the queue adapters to build the per-capability in-flight admission filter
    without re-deriving the kind→capability mapping on every claim."""
    resolved = caps if caps is not None else DEFAULT_CAPABILITY_CAPS
    triples: list[tuple[str, int, frozenset[str]]] = []
    for capability, cap in resolved.items():
        if cap < 0:
            continue
        kinds = kinds_for_capability(capability)
        if kinds:
            triples.append((capability, cap, frozenset(kinds)))
    return tuple(triples)
