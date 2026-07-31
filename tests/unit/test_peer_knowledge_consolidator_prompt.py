"""Peer-profile consolidation must merge repeated facts, not stack them (P7b).

The social roster (「你認識的人」) is rendered straight from
``character_peer_profiles``; the profile text itself is written by an LLM
that sees the *previous* profile plus new memories. Without an explicit
merge rule the model treats its own prior output as a log and appends,
so the 2026-07-29 dump had one peer (鈴音) whose profile restated the same
fact — "會分享或轉述見聞、替角色補充故事畫面" — four times inside a single
roster line, burning prompt budget and reading as a stutter.

The render side is deliberately untouched: collapsing prose at render
time would be keyword surgery on model-written text. The fix is a rule in
the consolidation prompt, asserted here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_peer_profile import CharacterPeerProfile
from kokoro_link.domain.entities.character_relationship import CharacterRelationship
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.social.llm_peer_knowledge_consolidator import (
    LLMPeerKnowledgeConsolidator,
)

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

# The observed peak case: one fact, four times, in a single profile.
REPEATED_FACT = "會分享或轉述見聞，替角色補充故事畫面"
BLOATED_SUMMARY = "；".join([REPEATED_FACT] * 4)


class _CapturingModel:
    provider_id = "capturing"
    supports_vision = False

    def __init__(self, reply: dict[str, object] | None = None) -> None:
        self.prompts: list[str] = []
        self._reply = reply if reply is not None else {
            "summary": REPEATED_FACT,
            "occupation": "",
            "haunts": [],
            "habits": [],
            "relationship_note": "",
            "confidence": 0.6,
        }

    async def generate(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        self.prompts.append(prompt)
        return json.dumps(self._reply, ensure_ascii=False)

    async def generate_stream(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        yield await self.generate(prompt, image_urls=image_urls, model=model)

    async def list_models(self) -> list[str]:
        return ["capturing"]


def _character(name: str) -> Character:
    return Character.create(
        name=name,
        summary=f"{name} summary",
        personality=[],
        interests=[],
        speaking_style="natural",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _memory(character_id: str, content: str) -> MemoryItem:
    return MemoryItem.create(
        character_id=character_id,
        kind=MemoryKind.RELATIONSHIP,
        content=content,
        created_at=NOW,
    )


async def _capture_prompt(
    *, existing_profile: CharacterPeerProfile | None,
) -> str:
    observer = _character("芊璃")
    peer = _character("鈴音")
    relationship = CharacterRelationship.create(
        character_a_id=observer.id,
        character_b_id=peer.id,
        relationship_label="同好",
        how_a_sees_b="願意聽她講故事的人",
    )
    model = _CapturingModel()
    consolidator = LLMPeerKnowledgeConsolidator(model=model)
    await consolidator.consolidate(
        observer=observer,
        peer=peer,
        existing_profile=existing_profile,
        relationship=relationship,
        memories=[
            _memory(observer.id, "鈴音又轉述了一段她在車站聽到的閒聊。"),
            _memory(observer.id, "鈴音提到她週末通常待在書店。"),
        ],
    )
    assert model.prompts, "consolidator did not reach the model"
    return model.prompts[0]


@pytest.mark.asyncio
async def test_prompt_orders_a_full_rewrite_not_an_append() -> None:
    prompt = await _capture_prompt(existing_profile=None)

    assert "整份取代" in prompt
    assert "不是接在後面" in prompt
    assert "合併去重" in prompt


@pytest.mark.asyncio
async def test_prompt_forbids_restating_the_same_fact() -> None:
    prompt = await _capture_prompt(existing_profile=None)

    assert "同一件事只講一次" in prompt
    assert "合併成一句" in prompt
    # near-duplicates count as duplicates — this is the case the ×4 bloat
    # slipped through: same fact, slightly different wording each round.
    assert "近重複也要合併" in prompt
    assert "意思相同、只是措辭不同" in prompt


@pytest.mark.asyncio
async def test_prompt_requires_keeping_new_information_when_merging() -> None:
    prompt = await _capture_prompt(existing_profile=None)

    assert "合併時不得丟資訊" in prompt


@pytest.mark.asyncio
async def test_bloated_profile_is_shown_to_the_model_with_the_merge_rule() -> None:
    """Peak case: the ×4 profile is handed back verbatim *together with*
    the merge rule, so the next round has both the evidence of the
    duplication and the instruction to collapse it."""
    existing = CharacterPeerProfile.create(
        character_id="observer",
        peer_character_id="peer",
        peer_name="鈴音",
        summary=BLOATED_SUMMARY,
        habits=[REPEATED_FACT, "常常轉述見聞替角色補畫面"],
        confidence=0.7,
    )

    prompt = await _capture_prompt(existing_profile=existing)

    assert BLOATED_SUMMARY in prompt
    assert "同一件事只講一次" in prompt
    assert "近重複也要合併" in prompt


@pytest.mark.asyncio
async def test_existing_conservatism_rules_are_not_dropped() -> None:
    """The merge rules are additive — hearsay / no-fabrication discipline
    must survive."""
    prompt = await _capture_prompt(existing_profile=None)

    assert "hearsay 可作線索但不可升級成確定事實" in prompt
    assert "不要編造沒有 evidence 的職業、地點、習慣" in prompt
    assert '{"summary": "", "occupation": "", "haunts": [], "habits": [], ' in prompt
