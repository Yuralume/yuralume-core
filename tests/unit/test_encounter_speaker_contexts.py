"""Phase 1 assertions for encounter speaker-context assembly
(ENCOUNTER_CHAT_PARITY_PLAN): life-material bucket, "already discussed"
negative examples, and the planner's anti-convergence injection.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterPlanner,
    CharacterEncounterRunner,
)
from kokoro_link.application.services.character_life_context import (
    CharacterLifeContext,
)

_NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


class _FakeModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, prompt: str, *, model: str | None = None) -> str:
        self.prompts.append(prompt)
        return '{"should_plan": false}'


class _Provider:
    def __init__(self) -> None:
        self.model = _FakeModel()

    async def is_fake(self, feature_key=None, *, character=None) -> bool:
        return False

    async def resolve(self, feature_key=None, *, character=None):
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        return None


class _FakeLifeBuilder:
    def __init__(self, per_character: dict[str, CharacterLifeContext]) -> None:
        self._per_character = per_character

    async def build(self, character, *, now, local_tz=None):
        return self._per_character[character.id]


class _FakeSocialKnowledge:
    def __init__(self) -> None:
        self.seen_operator_summaries: dict[str, str] = {}

    async def render_encounter_context(
        self, observer_id, peer_id, *, now=None, operator_dialogue_summary="",
    ):
        self.seen_operator_summaries[observer_id] = operator_dialogue_summary
        return [f"- 對方：{peer_id}"]


class _FakeEncounterRepo:
    def __init__(self, items) -> None:
        self._items = items

    async def list_for_relationship(self, relationship_id, *, limit=30):
        return [
            item for item in self._items
            if item.relationship_id == relationship_id
        ][:limit]


def _char(cid: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=cid, name=cid.upper(), summary=f"{cid} summary", user_id="u1",
        personality=(), speaking_style="", interests=(), boundaries=(),
    )


def _completed_encounter(
    *,
    relationship_id: str = "rel-1",
    encounter_id: str = "enc-old",
    days_ago: float = 1.0,
    summary_a: str = "聊到亮亮的東西",
    summary_b: str = "被拉去看亮亮的東西",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=encounter_id,
        relationship_id=relationship_id,
        status="completed",
        scheduled_for=_NOW - timedelta(days=days_ago),
        summary_for_a=summary_a,
        summary_for_b=summary_b,
        trigger_reason="路過打招呼",
    )


def _life(operator_summary: str = "") -> CharacterLifeContext:
    return CharacterLifeContext(
        schedule_lines=("- 此刻行程：整理繪馬（社務所）",),
        goal_lines=("- 最近在追求：學會底片沖洗",),
        operator_dialogue_summary=operator_summary,
    )


def _runner(**overrides) -> CharacterEncounterRunner:
    kwargs = dict(
        encounter_repository=MagicMock(),
        character_repository=MagicMock(),
        memory_writer=MagicMock(),
        relationship_service=MagicMock(),
        provider=_Provider(),
        local_tz=timezone.utc,
    )
    kwargs.update(overrides)
    return CharacterEncounterRunner(**kwargs)


@pytest.mark.asyncio
async def test_speaker_contexts_include_life_and_topic_history_buckets() -> None:
    char_a, char_b = _char("a"), _char("b")
    repo = _FakeEncounterRepo([
        _completed_encounter(days_ago=1.0),
        _completed_encounter(encounter_id="enc-old2", days_ago=2.0,
                             summary_a="聊祭典準備", summary_b="聊祭典準備"),
    ])
    runner = _runner(
        encounter_repository=repo,
        social_knowledge_service=_FakeSocialKnowledge(),
        life_context_builder=_FakeLifeBuilder({
            "a": _life(), "b": CharacterLifeContext(),
        }),
    )
    current = SimpleNamespace(id="enc-now", relationship_id="rel-1")
    contexts = await runner._speaker_contexts(
        char_a, char_b, now=_NOW, encounter=current,
    )
    a_text = "\n".join(contexts["a"])
    b_text = "\n".join(contexts["b"])

    # Bucket 1: peer/relationship lines from social knowledge.
    assert "【對方與你們的關係】" in a_text
    # Bucket 2: own life material only for the speaker that has some.
    assert "【自己最近的生活" in a_text
    assert "整理繪馬" in a_text
    assert "【自己最近的生活" not in b_text
    # Bucket 3: per-side negative examples with a relative-time anchor.
    assert "【最近幾次碰面已聊過" in a_text
    assert "聊到亮亮的東西" in a_text
    assert "被拉去看亮亮的東西" in b_text
    assert "約 1 天前" in a_text
    assert "聊祭典準備" in a_text


@pytest.mark.asyncio
async def test_speaker_contexts_exclude_current_encounter_from_history() -> None:
    char_a, char_b = _char("a"), _char("b")
    repo = _FakeEncounterRepo([
        _completed_encounter(encounter_id="enc-now", summary_a="這場自己"),
        _completed_encounter(encounter_id="enc-old", summary_a="上一場"),
    ])
    runner = _runner(encounter_repository=repo)
    current = SimpleNamespace(id="enc-now", relationship_id="rel-1")
    contexts = await runner._speaker_contexts(
        char_a, char_b, now=_NOW, encounter=current,
    )
    a_text = "\n".join(contexts["a"])
    assert "上一場" in a_text
    assert "這場自己" not in a_text


@pytest.mark.asyncio
async def test_operator_summary_flows_into_social_knowledge_gate() -> None:
    # The runner must hand the speaker's own operator-dialogue digest to
    # the social-knowledge renderer (which applies the closeness gate) —
    # never paste it into the generic buckets itself.
    char_a, char_b = _char("a"), _char("b")
    social = _FakeSocialKnowledge()
    runner = _runner(
        encounter_repository=_FakeEncounterRepo([]),
        social_knowledge_service=social,
        life_context_builder=_FakeLifeBuilder({
            "a": _life("主人最近在準備搬家"),
            "b": CharacterLifeContext(),
        }),
    )
    current = SimpleNamespace(id="enc-now", relationship_id="rel-1")
    contexts = await runner._speaker_contexts(
        char_a, char_b, now=_NOW, encounter=current,
    )
    assert social.seen_operator_summaries["a"] == "主人最近在準備搬家"
    assert social.seen_operator_summaries["b"] == ""
    assert "搬家" not in "\n".join(contexts["a"])


@pytest.mark.asyncio
async def test_planner_prompt_injects_recent_topics_as_negative_examples() -> None:
    provider = _Provider()
    planner = CharacterEncounterPlanner(
        relationship_repository=MagicMock(),
        encounter_repository=_FakeEncounterRepo([_completed_encounter()]),
        character_repository=MagicMock(),
        schedule_service=MagicMock(),
        schedule_repository=MagicMock(),
        provider=provider,
        local_tz=timezone.utc,
    )
    relationship = SimpleNamespace(
        id="rel-1",
        relationship_label="朋友",
        how_a_sees_b=None,
        how_b_sees_a=None,
        last_interaction_at=None,
        perspective_for=lambda _cid: SimpleNamespace(
            affection_self_to_peer=60, trust_self_to_peer=60,
        ),
    )
    topic_lines = await planner._recent_pair_topic_lines(relationship, now=_NOW)
    await planner._ask_llm_for_plan(
        relationship=relationship,
        char_a=_char("a"),
        char_b=_char("b"),
        start_at=_NOW + timedelta(hours=1),
        end_at=_NOW + timedelta(hours=2),
        hint_location=None,
        recent_topic_lines=topic_lines,
    )
    prompt = provider.model.prompts[0]
    assert "最近碰面聊過的內容" in prompt
    assert "聊到亮亮的東西" in prompt
    assert "should_plan 應保守給 false" in prompt
    assert "約 1 天前" in prompt


class _ScriptedProvider:
    """Provider whose model returns a scripted response and records prompts."""

    def __init__(self, response: str, *, fake: bool = False) -> None:
        self.fake = fake
        self.model = _FakeModel()
        self.model_response = response
        self.characters_seen: list[object] = []

        async def _generate(prompt: str, *, model: str | None = None) -> str:
            self.model.prompts.append(prompt)
            return self.model_response

        self.model.generate = _generate

    async def is_fake(self, feature_key=None, *, character=None) -> bool:
        self.characters_seen.append(character)
        return self.fake

    async def resolve(self, feature_key=None, *, character=None):
        self.characters_seen.append(character)
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        self.characters_seen.append(character)
        return None


def _encounter_ns() -> SimpleNamespace:
    return SimpleNamespace(
        id="enc-now",
        relationship_id="rel-1",
        location="神社前庭",
        trigger_reason="路過打招呼",
        max_turns=4,
        scheduled_for=_NOW,
    )


@pytest.mark.asyncio
async def test_beats_fake_provider_falls_back_to_trigger_reason() -> None:
    provider = _ScriptedProvider("ignored", fake=True)
    runner = _runner(provider=provider)
    beats = await runner._plan_topic_beats(
        _encounter_ns(), _char("a"), _char("b"),
        speaker_contexts={},
    )
    assert len(beats) == 1
    assert beats[0].topic == "路過打招呼"


@pytest.mark.asyncio
async def test_beats_parses_and_clamps_llm_output() -> None:
    provider = _ScriptedProvider(
        '{"beats": ['
        '{"topic": "河堤拍照的成果", "carrier": "a", "note": "剛拍完"},'
        '{"topic": "主人準備搬家", "carrier": "weird"},'
        '{"topic": "祭典分工"},'
        '{"topic": "多出來的第四拍"}'
        ']}',
    )
    runner = _runner(provider=provider)
    beats = await runner._plan_topic_beats(
        _encounter_ns(), _char("a"), _char("b"),
        speaker_contexts={"a": ["- 此刻行程：整理繪馬"], "b": []},
    )
    assert len(beats) == 3
    assert beats[0].topic == "河堤拍照的成果"
    assert beats[0].carrier == "a"
    assert beats[1].carrier == "both"
    prompt = provider.model.prompts[0]
    assert "話題設計器" in prompt
    assert "整理繪馬" in prompt
    assert "除非有明確新進展" in prompt
    # Cloud attribution: every provider call must forward a character.
    assert provider.characters_seen
    assert all(c is not None for c in provider.characters_seen)


@pytest.mark.asyncio
async def test_beats_bad_json_falls_back() -> None:
    provider = _ScriptedProvider("not json at all")
    runner = _runner(provider=provider)
    beats = await runner._plan_topic_beats(
        _encounter_ns(), _char("a"), _char("b"), speaker_contexts={},
    )
    assert len(beats) == 1
    assert beats[0].topic == "路過打招呼"


@pytest.mark.asyncio
async def test_transcript_prompt_carries_beats_register_and_retry() -> None:
    from kokoro_link.application.services.character_encounter_service import (
        EncounterBeat,
    )
    from kokoro_link.contracts.register_profile import RegisterProfile

    provider = _ScriptedProvider("嗯，我剛好也在這裡。<END>")
    runner = _runner(provider=provider)
    profile = RegisterProfile(
        axes={"emotional_intensity": 0.8, "seriousness": 0.2,
              "intimacy": 0.4, "humor_latitude": 0.6, "help_seeking": 0.1},
        confidence=0.9,
    )
    await runner._generate_transcript(
        _encounter_ns(), _char("a"), _char("b"),
        speaker_contexts={"a": [], "b": []},
        beats=(EncounterBeat(topic="河堤拍照的成果", carrier="a"),),
        register_profile=profile,
        retry_directive="整段都在寒暄，沒有新內容",
    )
    prompt = provider.model.prompts[0]
    assert "話題節拍" in prompt
    assert "河堤拍照的成果" in prompt
    assert "自然帶起：A" in prompt
    assert "不要諮商師或客服腔" in prompt
    assert "情緒較重" in prompt  # warmth earned via emotional_intensity 0.8
    assert "品質檢查退回：整段都在寒暄" in prompt


@pytest.mark.asyncio
async def test_register_profile_anchors_on_meetup_situation() -> None:
    class _RecordingProfiler:
        def __init__(self) -> None:
            self.contexts = []
            self.characters = []

        async def profile(self, context, *, character=None):
            self.contexts.append(context)
            self.characters.append(character)
            return None

    profiler = _RecordingProfiler()
    runner = _runner(register_profiler=profiler)
    char_a, char_b = _char("a"), _char("b")
    from kokoro_link.application.services.character_encounter_service import (
        EncounterBeat,
    )
    result = await runner._profile_register(
        _encounter_ns(), char_a, char_b,
        beats=(EncounterBeat(topic="河堤拍照"),),
    )
    assert result is None
    context = profiler.contexts[0]
    assert "神社前庭" in context.latest_user_message
    assert "路過打招呼" in context.latest_user_message
    assert "河堤拍照" in context.recent_dialogue_summary
    assert profiler.characters[0] is char_a
