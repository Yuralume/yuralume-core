"""Step 6 tails — the remaining render points consume the resolved
address instead of the raw display name / observed salutation, and the
per-pair rename log renders as a relationship event.

These are the "no residual raw render" regressions: once a resolver
result is supplied, a raw platform/OAuth display name must not leak into
memory content, persona extraction, or the proactive register block.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.address_change_event import (
    AddressChangeEvent,
)
from kokoro_link.domain.value_objects.resolved_address import (
    AddressProvenance,
    ResolvedAddress,
)
from kokoro_link.infrastructure.persona.llm_extractor import (
    _build_prompt as _persona_build_prompt,
)
from kokoro_link.infrastructure.post_turn.llm_processor import (
    _render_operator_context,
)
from kokoro_link.infrastructure.proactive.llm_intention_judge import (
    _render_address_preference_lines,
)
from kokoro_link.infrastructure.prompt.address_change import (
    render_address_change_lines,
)


# --- post-turn operator context (direction A, memory-content naming) ----


def test_post_turn_operator_context_prefers_resolved_primary() -> None:
    operator = OperatorProfile(id="op1", display_name="alex_4821")
    resolved = ResolvedAddress(
        primary="阿力",
        aliases=("alex_4821",),
        provenance=AddressProvenance.EXPLICIT_SEED,
    )
    text = "\n".join(_render_operator_context(operator, resolved))
    assert "稱呼：阿力" in text  # seed name names memories
    assert "稱呼：alex_4821" not in text  # raw platform label demoted
    assert "別稱：alex_4821" in text  # kept so old memories still resolve


def test_post_turn_operator_context_legacy_without_resolver() -> None:
    operator = OperatorProfile(id="op1", display_name="艾力")
    text = "\n".join(_render_operator_context(operator))
    assert "稱呼：艾力" in text


def test_post_turn_operator_context_fallback_renders_nothing() -> None:
    operator = OperatorProfile.default()
    fallback = ResolvedAddress(
        primary="對方", provenance=AddressProvenance.FALLBACK,
    )
    assert _render_operator_context(operator, fallback) == []


# --- persona extractor operator label (direction A) ---------------------


def test_persona_extractor_label_uses_resolved_primary() -> None:
    operator = OperatorProfile(id="op1", display_name="alex_4821")
    resolved = ResolvedAddress(
        primary="阿力", provenance=AddressProvenance.LEARNED_PERSONA,
    )
    prompt = _persona_build_prompt(
        operator=operator,
        current_persona=OperatorPersona.empty("c1", "op1"),
        user_message="嗨",
        assistant_message="嗨～",
        recent_messages=[],
        resolved_player_address=resolved,
    )
    assert "阿力" in prompt
    assert "alex_4821" not in prompt  # raw label must not leak


def test_persona_extractor_label_legacy_without_resolver() -> None:
    operator = OperatorProfile(id="op1", display_name="艾力")
    prompt = _persona_build_prompt(
        operator=operator,
        current_persona=OperatorPersona.empty("c1", "op1"),
        user_message="嗨",
        assistant_message="嗨～",
        recent_messages=[],
    )
    assert "艾力" in prompt


# --- proactive intention judge salutation (direction B) -----------------


def test_intention_judge_uses_resolved_salutation_without_observation() -> None:
    lines = _render_address_preference_lines(
        None, resolved_salutation="美緒姐",
    )
    assert any("對方稱呼這個角色：美緒姐" in line for line in lines)


def test_intention_judge_resolved_salutation_outranks_observed() -> None:
    pref = SimpleNamespace(
        is_empty=False,
        salutation="美緒",
        formality_level="",
        response_length_pref="",
    )
    lines = _render_address_preference_lines(
        pref, resolved_salutation="美緒姐",
    )
    assert any("對方稱呼這個角色：美緒姐" in line for line in lines)
    assert not any("美緒\n" in line or line.endswith("美緒") for line in lines)


def test_intention_judge_no_salutation_stays_quiet() -> None:
    assert _render_address_preference_lines(None, resolved_salutation=None) == []


# --- rename-log relationship-event rendering ----------------------------


def _event(direction: str, *, old: str, new: str) -> AddressChangeEvent:
    return AddressChangeEvent(
        character_id="c1",
        operator_id="op1",
        direction=direction,
        old_value=old,
        new_value=new,
        effective_at=datetime(2026, 6, 29, tzinfo=timezone.utc),
    )


def test_rename_log_renders_player_direction() -> None:
    lines = render_address_change_lines(
        player_event=_event("player", old="艾力", new="阿力"),
    )
    text = "\n".join(lines)
    assert "阿力" in text
    assert "艾力" in text  # old name referenced so memories still link
    assert "6/29" in text


def test_rename_log_renders_both_directions() -> None:
    lines = render_address_change_lines(
        player_event=_event("player", old="艾力", new="阿力"),
        character_event=_event("character", old="美緒", new="美緒姐"),
    )
    text = "\n".join(lines)
    assert "阿力" in text
    assert "美緒姐" in text


def test_rename_log_empty_when_no_events() -> None:
    assert render_address_change_lines() == []


def test_rename_log_ignores_mismatched_direction() -> None:
    # A character-direction event passed in the player slot is ignored —
    # the renderer only trusts events whose direction matches the slot.
    assert render_address_change_lines(
        player_event=_event("character", old="美緒", new="美緒姐"),
    ) == []
