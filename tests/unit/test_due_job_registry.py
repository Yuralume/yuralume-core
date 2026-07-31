"""Phase 5 per-kind registry: kind × priority × capability × handler mapping."""

from __future__ import annotations

from kokoro_link.contracts.due_jobs import (
    BEAT_DUE_KIND,
    CHARACTER_UPKEEP_KIND,
    CHARACTER_KIND_REGISTRY,
    FEED_COMMENT_REPLY_KIND,
    FEED_COMPOSE_KIND,
    GOAL_REVIEW_KIND,
    JobCapability,
    KnobGate,
    MEMORIALIZE_KIND,
    PENDING_FOLLOW_UP_RELEASE_KIND,
    PROACTIVE_EVALUATE_KIND,
    SCHEDULE_MAINTENANCE_KIND,
    SCHEDULE_WEATHER_VET_KIND,
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
        CHARACTER_UPKEEP_KIND,
    }


def test_priorities_match_section_5() -> None:
    # §5: beat / schedule repair = 2, proactive = 3, feed = 4, upkeep / maintenance = 5.
    assert kind_spec(BEAT_DUE_KIND).priority == 2
    assert kind_spec(SCHEDULE_MAINTENANCE_KIND).priority == 2
    assert kind_spec(SCHEDULE_WEATHER_VET_KIND).priority == 2
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
    ):
        assert kind_spec(llm_kind).capability is JobCapability.LLM


def test_knob_gate_mapping() -> None:
    assert kind_spec(PROACTIVE_EVALUATE_KIND).knob_gate is KnobGate.PROACTIVE
    assert kind_spec(FEED_COMPOSE_KIND).knob_gate is KnobGate.BACKGROUND
    assert kind_spec(SCHEDULE_MAINTENANCE_KIND).knob_gate is KnobGate.BACKGROUND
    assert kind_spec(SCHEDULE_WEATHER_VET_KIND).knob_gate is KnobGate.BACKGROUND
    assert kind_spec(GOAL_REVIEW_KIND).knob_gate is KnobGate.BACKGROUND
    # Cheap DB-only + precisely-timed kinds are never down-shifted.
    for none_kind in (BEAT_DUE_KIND, MEMORIALIZE_KIND, CHARACTER_UPKEEP_KIND):
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
