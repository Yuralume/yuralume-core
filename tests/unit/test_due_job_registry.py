"""Phase 5 per-kind registry: kind × priority × capability × handler mapping."""

from __future__ import annotations

from kokoro_link.contracts.due_jobs import (
    BEAT_DUE_KIND,
    CHARACTER_UPKEEP_KIND,
    CHARACTER_KIND_REGISTRY,
    DEFAULT_CAPABILITY_CAPS,
    FEED_COMMENT_REPLY_KIND,
    FEED_COMPOSE_KIND,
    FEED_VIDEO_POLL_KIND,
    POST_TURN_KIND,
    capability_kind_sets,
    kinds_for_capability,
    GOAL_REVIEW_KIND,
    JobCapability,
    KnobGate,
    MEMORIALIZE_KIND,
    PENDING_FOLLOW_UP_RELEASE_KIND,
    PROACTIVE_EVALUATE_KIND,
    SCHEDULE_MAINTENANCE_KIND,
    SCHEDULE_WEATHER_VET_KIND,
    STORY_SCENE_TIMEOUT_KIND,
    character_chain_kinds,
    is_character_chain_kind,
    kind_spec,
)


def test_registry_covers_all_character_kinds() -> None:
    assert set(character_chain_kinds()) == set(CHARACTER_KIND_REGISTRY)
    assert set(CHARACTER_KIND_REGISTRY) == {
        BEAT_DUE_KIND,
        SCHEDULE_MAINTENANCE_KIND,
        SCHEDULE_WEATHER_VET_KIND,
        MEMORIALIZE_KIND,
        FEED_COMPOSE_KIND,
        FEED_COMMENT_REPLY_KIND,
        PROACTIVE_EVALUATE_KIND,
        GOAL_REVIEW_KIND,
        STORY_SCENE_TIMEOUT_KIND,
        CHARACTER_UPKEEP_KIND,
    }


def test_priorities_match_section_5() -> None:
    # §5: beat / schedule repair = 2, proactive = 3, feed = 4, upkeep / maintenance = 5.
    assert kind_spec(BEAT_DUE_KIND).priority == 2
    assert kind_spec(SCHEDULE_MAINTENANCE_KIND).priority == 2
    assert kind_spec(SCHEDULE_WEATHER_VET_KIND).priority == 2
    # SC1-E rides the precisely-timed tier: an open scene's idle deadline
    # is exact, and the wrap-up is output the player already paid for.
    assert kind_spec(STORY_SCENE_TIMEOUT_KIND).priority == 2
    assert kind_spec(PROACTIVE_EVALUATE_KIND).priority == 3
    assert kind_spec(FEED_COMPOSE_KIND).priority == 4
    assert kind_spec(FEED_COMMENT_REPLY_KIND).priority == 4
    assert kind_spec(MEMORIALIZE_KIND).priority == 5
    assert kind_spec(CHARACTER_UPKEEP_KIND).priority == 5
    assert kind_spec(GOAL_REVIEW_KIND).priority == 5


def test_capability_mapping() -> None:
    assert kind_spec(FEED_COMPOSE_KIND).capability is JobCapability.IMAGE
    assert kind_spec(CHARACTER_UPKEEP_KIND).capability is JobCapability.NONE
    for llm_kind in (
        BEAT_DUE_KIND,
        SCHEDULE_MAINTENANCE_KIND,
        SCHEDULE_WEATHER_VET_KIND,
        MEMORIALIZE_KIND,
        FEED_COMMENT_REPLY_KIND,
        PROACTIVE_EVALUATE_KIND,
        GOAL_REVIEW_KIND,
        STORY_SCENE_TIMEOUT_KIND,
    ):
        assert kind_spec(llm_kind).capability is JobCapability.LLM


def test_knob_gate_mapping() -> None:
    assert kind_spec(PROACTIVE_EVALUATE_KIND).knob_gate is KnobGate.PROACTIVE
    assert kind_spec(FEED_COMPOSE_KIND).knob_gate is KnobGate.BACKGROUND
    assert kind_spec(SCHEDULE_MAINTENANCE_KIND).knob_gate is KnobGate.BACKGROUND
    assert kind_spec(SCHEDULE_WEATHER_VET_KIND).knob_gate is KnobGate.BACKGROUND
    assert kind_spec(GOAL_REVIEW_KIND).knob_gate is KnobGate.BACKGROUND
    # Cheap DB-only + precisely-timed kinds are never down-shifted.
    for none_kind in (
        BEAT_DUE_KIND,
        MEMORIALIZE_KIND,
        CHARACTER_UPKEEP_KIND,
        # SC1-E: the idle window is the promise that a paid opening always
        # produces something — a down-shifted account must not get a longer
        # one, or 起幕 stays blocked for the player most likely to return.
        STORY_SCENE_TIMEOUT_KIND,
    ):
        assert kind_spec(none_kind).knob_gate is KnobGate.NONE


def test_feed_comment_reply_is_event_driven() -> None:
    assert kind_spec(FEED_COMMENT_REPLY_KIND).event_driven is True
    # The rest fire on their chain cadence, not events.
    assert kind_spec(PROACTIVE_EVALUATE_KIND).event_driven is False


def test_every_kind_is_chained_and_character_scoped() -> None:
    for spec in CHARACTER_KIND_REGISTRY.values():
        assert spec.chained is True
        assert spec.character_scoped is True
        assert spec.handler  # non-empty handler binding


def test_unknown_kind_returns_none() -> None:
    assert kind_spec("no_such_kind") is None


def test_pending_follow_up_release_is_one_shot_event_kind() -> None:
    # The one-shot follow-up release is registered (so kind_spec reports its
    # priority / capability) but expressed as NOT chained + NOT character-scoped,
    # and is excluded from the per-character reconcile set + the routing predicate
    # for the chain handler.
    spec = kind_spec(PENDING_FOLLOW_UP_RELEASE_KIND)
    assert spec is not None
    assert spec.priority == 1
    assert spec.capability is JobCapability.LLM
    assert spec.chained is False
    assert spec.character_scoped is False
    assert spec.event_driven is True
    assert PENDING_FOLLOW_UP_RELEASE_KIND not in CHARACTER_KIND_REGISTRY
    assert PENDING_FOLLOW_UP_RELEASE_KIND not in character_chain_kinds()
    assert is_character_chain_kind(PENDING_FOLLOW_UP_RELEASE_KIND) is False
    assert is_character_chain_kind(BEAT_DUE_KIND) is True


def test_feed_video_poll_never_competes_for_the_image_slot() -> None:
    """CV4 red line: the deferred video poll must NOT ride ``image``.

    ``image`` is capped at one in-flight job because it models a GPU. A
    poll that took that slot would let one fifteen-minute clip block every
    feed picture on the deployment — the exact failure the asynchronous
    pipeline exists to remove.
    """
    spec = kind_spec(FEED_VIDEO_POLL_KIND)
    assert spec is not None
    assert spec.capability is JobCapability.VIDEO_POLL
    assert spec.capability is not JobCapability.IMAGE
    assert spec.chained is False
    assert spec.character_scoped is False
    assert spec.event_driven is True
    assert FEED_VIDEO_POLL_KIND not in CHARACTER_KIND_REGISTRY
    assert is_character_chain_kind(FEED_VIDEO_POLL_KIND) is False

    # The capability the claim filter counts by kind: image stays exactly
    # the set it was, and the poll gets its own, generously-capped one.
    assert kinds_for_capability(JobCapability.IMAGE.value) == (FEED_COMPOSE_KIND,)
    assert kinds_for_capability(JobCapability.VIDEO_POLL.value) == (
        FEED_VIDEO_POLL_KIND,
    )
    caps = DEFAULT_CAPABILITY_CAPS
    assert caps[JobCapability.VIDEO_POLL.value] > caps[JobCapability.IMAGE.value]
    triples = dict(
        (capability, kinds) for capability, _cap, kinds in capability_kind_sets()
    )
    assert triples[JobCapability.VIDEO_POLL.value] == frozenset(
        {FEED_VIDEO_POLL_KIND},
    )
    assert FEED_VIDEO_POLL_KIND not in triples[JobCapability.IMAGE.value]


def test_player_owed_one_shots_stay_exempt_from_the_llm_ceiling() -> None:
    """The opt-in cap flag must not have quietly enrolled the existing
    one-shot kinds: a follow-up release and a post-turn extraction are
    player-owed and must never queue behind the background LLM ceiling."""
    for kind in (PENDING_FOLLOW_UP_RELEASE_KIND, POST_TURN_KIND):
        spec = kind_spec(kind)
        assert spec is not None
        assert spec.capability is JobCapability.LLM
        assert spec.counts_toward_capability_cap is False
        assert kind not in kinds_for_capability(JobCapability.LLM.value)


def test_goal_review_is_a_daily_character_chain() -> None:
    # CF2: goal review must reach a character whose player never chats, so it
    # is a per-character self-continuing chain on the same daily cadence as
    # schedule maintenance — not an event-driven one-shot off a chat turn.
    spec = kind_spec(GOAL_REVIEW_KIND)
    assert spec is not None
    assert spec.base_interval_seconds == 86_400.0
    assert spec.chained is True
    assert spec.character_scoped is True
    assert spec.event_driven is False
    assert GOAL_REVIEW_KIND in character_chain_kinds()
    assert is_character_chain_kind(GOAL_REVIEW_KIND) is True


def test_weather_vet_runs_on_a_sub_daily_cadence() -> None:
    # The whole point of the intra-day drift correction is that it happens
    # INTRA-DAY. Riding the daily schedule_maintenance chain would hand the
    # distributed topology the once-a-midnight cadence it exists to replace.
    weather_vet = kind_spec(SCHEDULE_WEATHER_VET_KIND)
    maintenance = kind_spec(SCHEDULE_MAINTENANCE_KIND)
    assert weather_vet.base_interval_seconds <= 1_800.0
    assert weather_vet.base_interval_seconds < maintenance.base_interval_seconds


def test_story_scene_timeout_is_a_character_chain_with_a_cheap_recheck() -> None:
    # SC1-E. The chain exists for a state almost no character is in, so its
    # base cadence is only the fallback recheck (one indexed read per hour);
    # a live scene's exact deadline arrives as ``explicit_next_due`` instead.
    spec = kind_spec(STORY_SCENE_TIMEOUT_KIND)
    assert spec is not None
    assert spec.base_interval_seconds == 3_600.0
    assert spec.handler == "story_scene_timeout"
    assert spec.chained is True
    assert spec.character_scoped is True
    assert spec.event_driven is False
    assert STORY_SCENE_TIMEOUT_KIND in character_chain_kinds()
    assert is_character_chain_kind(STORY_SCENE_TIMEOUT_KIND) is True
