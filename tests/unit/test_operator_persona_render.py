"""Unit tests for the prompt-rendering side of OperatorPersonaService.

We don't need a DB or a real LLM here — the renderer is a pure
function on the aggregate. The tests confirm:

- low-confidence fields are filtered out per layer threshold
- Layer 4 surfaces qualitative interaction-heat labels, never raw counts
- Layer 5 carries the "信任是雙向" reminder when any trust field
  passes the threshold
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from kokoro_link.application.services.operator_persona_service import (
    OperatorPersonaService,
)
from kokoro_link.bootstrap.settings import PersonaSettings
from kokoro_link.domain.entities.operator_persona import (
    InteractionStrength,
    OperatorPersona,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.value_objects.familiarity import Familiarity
from kokoro_link.domain.value_objects.profile_field import (
    EvidenceRef,
    ProfileField,
)


_CHAR_ID = "char-test"
_OP_ID = "default"


def _field(
    field_key: str,
    layer: int,
    value: str,
    *,
    confidence: float = 0.8,
    source: str = "extraction",
    content_mode: MessageContentMode = MessageContentMode.NORMAL,
) -> ProfileField:
    return ProfileField(
        field_key=field_key,
        layer=layer,
        value=value,
        confidence=confidence,
        evidence_refs=(
            EvidenceRef(
                turn_id="t",
                conversation_id="c",
                quote="quote",
                extracted_at=datetime.now(timezone.utc),
            ),
        ),
        last_updated=datetime.now(timezone.utc),
        update_count=2,
        source=source,
        character_id=_CHAR_ID,
        content_mode=content_mode,
    )


def _service() -> OperatorPersonaService:
    return OperatorPersonaService(
        repository=AsyncMock(),
        strength_calculator=AsyncMock(),
        settings=PersonaSettings(),
    )


def test_empty_persona_renders_nothing():
    svc = _service()
    persona = OperatorPersona.empty(_CHAR_ID, _OP_ID)
    assert svc.render_for_prompt(persona) == []


def test_low_confidence_field_filtered_out():
    svc = _service()
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer1_identity={
            # Below 0.7 threshold — must not appear.
            "occupation": _field("occupation", 1, "engineer", confidence=0.6),
        },
    )
    assert svc.render_for_prompt(persona) == []


def test_layer1_renders_compact_clause():
    svc = _service()
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer1_identity={
            "name": _field("name", 1, "丹尼"),
            "occupation": _field("occupation", 1, "後端工程師"),
        },
    )
    lines = svc.render_for_prompt(persona)
    body = "\n".join(lines)
    assert "丹尼" in body
    assert "後端工程師" in body
    assert "關於對方" in body
    assert "不要每一輪都提起" in body
    assert "不要裝熟" in body


def test_peer_gossip_low_tier_omits_sensitive_layers():
    svc = _service()
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer1_identity={"occupation": _field("occupation", 1, "後端工程師")},
        layer2_life={"interests": _field("interests", 2, "咖啡")},
        layer3_emotional={
            "traumas": _field("traumas", 3, "不想公開的傷"),
            "secrets": _field("secrets", 3, "秘密計畫"),
            "values": _field("values", 3, "重視誠實"),
        },
        layer5_trust={
            "secret_kept": _field("secret_kept", 5, "曾保守秘密"),
        },
    )

    body = "\n".join(svc.render_for_peer_gossip(persona, closeness_tier="low"))

    assert "後端工程師" in body
    assert "咖啡" in body
    assert "不想公開的傷" not in body
    assert "秘密計畫" not in body
    assert "曾保守秘密" not in body


def test_peer_gossip_high_tier_can_include_deeper_trust_context():
    svc = _service()
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer3_emotional={
            "vulnerabilities": _field("vulnerabilities", 3, "怕被拋下"),
        },
        layer5_trust={
            "secret_kept": _field("secret_kept", 5, "曾保守秘密"),
        },
    )

    body = "\n".join(svc.render_for_peer_gossip(persona, closeness_tier="high"))

    assert "怕被拋下" in body
    assert "曾經願意託付秘密" in body


def test_nsfw_persona_fields_do_not_render_into_prompt():
    svc = _service()
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer1_identity={
            "occupation": _field(
                "occupation", 1, "後端工程師",
                content_mode=MessageContentMode.NSFW,
            ),
        },
        layer2_life={
            "interests": _field(
                "interests", 2, "私密偏好",
                content_mode=MessageContentMode.NSFW,
            ),
        },
    )

    body = "\n".join(svc.render_for_prompt(persona))

    assert "後端工程師" not in body
    assert "私密偏好" not in body


def test_layer3_threshold_is_stricter_than_layer1():
    """A 0.75 Layer-1 field renders; the same confidence on Layer 3
    must NOT render — emotional inferences require a tighter bar."""
    svc = _service()
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer1_identity={
            "occupation": _field("occupation", 1, "工程師", confidence=0.75),
        },
        layer3_emotional={
            "anxieties": _field("anxieties", 3, "工作焦慮", confidence=0.75),
        },
    )
    body = "\n".join(svc.render_for_prompt(persona))
    assert "工程師" in body
    assert "工作焦慮" not in body


def test_layer3_at_threshold_shows_caution_reminder():
    svc = _service()
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer3_emotional={
            "anxieties": _field("anxieties", 3, "工作焦慮", confidence=0.85),
        },
    )
    body = "\n".join(svc.render_for_prompt(persona))
    assert "工作焦慮" in body
    assert "信任你才透露" in body


def test_layer3_dream_inference_prefix():
    svc = _service()
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer3_emotional={
            "anxieties": _field(
                "anxieties", 3, "工作焦慮",
                confidence=0.85, source="dream_inference",
            ),
        },
    )
    body = "\n".join(svc.render_for_prompt(persona))
    assert "（dream 推論）" in body


def test_layer4_renders_interaction_heat_not_relationship_stage():
    """Raw message counts should never bleed into the prompt, and Layer 4
    must not describe the relationship stage."""
    svc = _service()
    strength = InteractionStrength(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        first_message_at=datetime.now(timezone.utc),
        total_user_messages=200,
        days_since_first_contact=35,
        messages_last_7_days=80,
        messages_last_30_days=200,
        longest_session_minutes=45,
        shared_arc_realized_count=1,
        shared_drama_count=0,
        familiarity_band=Familiarity.CLOSE,
        computed_at=datetime.now(timezone.utc),
    )
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer4_interaction=strength,
    )
    body = "\n".join(svc.render_for_prompt(persona))
    assert "互動很密切" in body
    assert "互動已持續 35 天" in body
    assert "close" not in body  # raw band string
    assert "認識 35 天" not in body
    assert "階段" not in body
    assert "200" not in body
    assert "80" not in body


def test_layer4_stranger_band_is_rendered_as_low_interaction_volume():
    svc = _service()
    strength = InteractionStrength(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        first_message_at=datetime.now(timezone.utc),
        total_user_messages=1,
        days_since_first_contact=0,
        messages_last_7_days=1,
        messages_last_30_days=1,
        longest_session_minutes=3,
        shared_arc_realized_count=0,
        shared_drama_count=0,
        familiarity_band=Familiarity.STRANGER,
        computed_at=datetime.now(timezone.utc),
    )
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer4_interaction=strength,
    )

    body = "\n".join(svc.render_for_prompt(persona))

    assert "互動還很少" in body
    assert "初識" not in body
    assert "剛認識" not in body


def test_layer5_renders_trust_summary_with_reminder():
    svc = _service()
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer5_trust={
            "money_borrowed": _field(
                "money_borrowed", 5, "借過 5000", confidence=0.85,
            ),
        },
    )
    body = "\n".join(svc.render_for_prompt(persona))
    assert "曾經借過錢" in body
    assert "信任是雙向的" in body


def test_world_event_relevance_uses_only_low_risk_profile_fields():
    svc = _service()
    strength = InteractionStrength(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        first_message_at=datetime.now(timezone.utc),
        total_user_messages=50,
        days_since_first_contact=12,
        messages_last_7_days=8,
        messages_last_30_days=30,
        longest_session_minutes=30,
        shared_arc_realized_count=0,
        shared_drama_count=0,
        familiarity_band=Familiarity.FAMILIAR,
        computed_at=datetime.now(timezone.utc),
    )
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer1_identity={
            "occupation": _field("occupation", 1, "後端工程師"),
            "family": _field("family", 1, "有一個妹妹"),
        },
        layer2_life={
            "interests": _field("interests", 2, "3C 發表會、網路迷因"),
            "relationship_status": _field("relationship_status", 2, "單身"),
        },
        layer3_emotional={
            "anxieties": _field("anxieties", 3, "工作焦慮", confidence=0.9),
        },
        layer5_trust={
            "secret_kept": _field("secret_kept", 5, "託付過秘密", confidence=0.9),
        },
        layer4_interaction=strength,
    )

    body = "\n".join(svc.render_world_event_relevance(persona))

    assert "後端工程師" in body
    assert "3C 發表會" in body
    assert "網路迷因" in body
    assert "互動頻繁" in body
    assert "熟悉度" not in body
    assert "妹妹" not in body
    assert "單身" not in body
    assert "工作焦慮" not in body
    assert "秘密" not in body


def test_world_event_relevance_excludes_nsfw_mode_profile_fields():
    svc = _service()
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer1_identity={
            "occupation": _field(
                "occupation", 1, "後端工程師",
                content_mode=MessageContentMode.NSFW,
            ),
        },
        layer2_life={
            "interests": _field(
                "interests", 2, "3C 發表會",
                content_mode=MessageContentMode.NSFW,
            ),
        },
    )

    body = "\n".join(svc.render_world_event_relevance(persona))

    assert "後端工程師" not in body
    assert "3C 發表會" not in body
