"""Regression coverage for player-visible LLM output language.

The operator's ``primary_language`` is the source of truth for content
the player sees. Chinese prompt scaffolding is fine for internal jobs,
but visible prose must not be pinned to Traditional Chinese.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from kokoro_link.contracts.character_draft import CompanionGenerationContext
from kokoro_link.contracts.feed import FeedComposerInput
from kokoro_link.contracts.feed_comment_reply import FeedCommentReplyInput
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeInput,
)
from kokoro_link.contracts.proactive import ProactiveContext
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
)
from kokoro_link.contracts.scene_access import SceneAccessContext
from kokoro_link.contracts.self_reflection import ReflectionGeneratorInput
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.branching_drama import DramaNode
from kokoro_link.domain.entities.feed_comment import FeedComment
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.pending_follow_up import PendingFollowUpMessage
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.drama_critique import (
    DramaCritique,
    DramaCritiqueFinding,
)
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.domain.value_objects.fusion_critique import (
    FusionCritiqueFinding,
)
from kokoro_link.domain.value_objects.fusion_outline import (
    ACT_OPENING,
    FusionBeatPlan,
    FusionOutline,
)
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.application.services.fusion_character_brief import CharacterBrief
from kokoro_link.application.services.fusion_story_planner import (
    _build_prompt as build_fusion_planner_prompt,
)
from kokoro_link.application.services.fusion_story_polisher import (
    _build_spot_prompt as build_fusion_polisher_spot_prompt,
    _build_whole_prompt as build_fusion_polisher_whole_prompt,
)
from kokoro_link.application.services.fusion_story_writer import (
    _build_prompt as build_fusion_writer_prompt,
)
from kokoro_link.application.services.branching_drama_director import (
    _build_narrate_prompt as build_branching_narrate_prompt,
    _build_scene_response_prompt as build_branching_response_prompt,
)
from kokoro_link.application.services.branching_drama_planner import (
    _build_children_prompt as build_branching_children_prompt,
    _build_root_prompt as build_branching_root_prompt,
)
from kokoro_link.application.services.branching_drama_polisher import (
    _build_prompt as build_branching_polisher_prompt,
)
from kokoro_link.infrastructure.reflection.llm_generator import (
    _build_prompt as build_reflection_prompt,
)
from kokoro_link.infrastructure.story.llm_arc_planner import (
    _build_prompt as build_story_arc_prompt,
    _synthetic_arc as build_synthetic_arc,
)
from kokoro_link.infrastructure.post_turn.llm_processor import (
    _build_prompt as build_post_turn_prompt,
)
from kokoro_link.infrastructure.state.llm_idle_drift import (
    _build_prompt as build_idle_drift_prompt,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.story.llm_expander import (
    _build_prompt as build_story_expander_prompt,
)
from kokoro_link.infrastructure.busy.llm_follow_up_composer import (
    _build_prompt as build_follow_up_prompt,
)
from kokoro_link.infrastructure.busy.llm_scheduled_promise_composer import (
    _build_prompt as build_scheduled_promise_prompt,
)
from kokoro_link.infrastructure.character_draft.llm_companion_generator import (
    _build_instruction as build_companion_draft_instruction,
)
from kokoro_link.infrastructure.character_draft.llm_generator import (
    _build_instruction as build_character_draft_instruction,
)
from kokoro_link.infrastructure.feed.llm_comment_reply import (
    _build_prompt as build_feed_comment_reply_prompt,
)
from kokoro_link.infrastructure.feed.llm_composer import (
    _build_prompt as build_feed_prompt,
)
from kokoro_link.infrastructure.proactive.llm_decider import (
    _build_prompt as build_proactive_prompt,
)
from kokoro_link.infrastructure.proactive.llm_intention_judge import (
    _build_prompt as build_intention_judge_prompt,
)
from kokoro_link.infrastructure.busy.llm_decider import (
    _build_prompt as build_busy_decider_prompt,
)
from kokoro_link.infrastructure.schedule.llm_aftermath import (
    _build_prompt as build_aftermath_prompt,
)
from kokoro_link.infrastructure.goal.llm_reviewer import (
    _build_prompt as build_goal_reviewer_prompt,
)
from kokoro_link.infrastructure.memory.llm_consolidator import (
    _build_prompt as build_memory_consolidator_prompt,
)
from kokoro_link.infrastructure.persona.llm_extractor import (
    _build_prompt as build_persona_extractor_prompt,
)
from kokoro_link.application.services.experiment_analysis_service import (
    _build_prompt as build_experiment_analysis_prompt,
)
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.infrastructure.scene_access.llm_judge import (
    _build_prompt as build_scene_access_prompt,
)
from kokoro_link.infrastructure.prompts import get_default_loader


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )


def _fusion_brief() -> CharacterBrief:
    return CharacterBrief(
        character_id="char-1",
        name="Mio",
        summary="A quiet barista.",
        text="Mio: A quiet barista who notices small changes.",
    )


def _fusion_outline() -> FusionOutline:
    beat = FusionBeatPlan.create(
        sequence=0,
        act=ACT_OPENING,
        title="First Signal",
        hook="Mio notices a message hidden in the morning routine.",
        dramatic_question="Will she answer it?",
        target_chars=600,
        focus_character_ids=("char-1",),
    )
    return FusionOutline.create(
        title="Morning Static",
        premise="A small signal changes an ordinary day.",
        theme="quiet connection",
        beats=(beat,),
    )


def _branching_node() -> DramaNode:
    return DramaNode.create_root(
        drama_id="drama-1",
        title="First Signal",
        summary="Mio notices a message hidden in the morning routine.",
        appearing_character_ids=("char-1",),
    )


def _assert_visible_language_rule(prompt: str) -> None:
    assert "玩家可見自然語言輸出語言" in prompt
    assert "en-US" in prompt


def test_chat_footer_uses_primary_language_fact_not_current_message_language() -> None:
    footer = get_default_loader().render(
        "chat/instructions_footer",
        response_format_instruction=(
            "回覆格式慣例：口語台詞直接寫，不要加引號；"
            "星號 `*...*` 內的動作、表情與狀態描寫也屬於玩家可見自然語言，"
            "不要因為下方格式範例是中文就把動作描寫寫成中文。"
        ),
    )

    assert "語言需與使用者相同" not in footer
    assert "玩家可見自然語言輸出語言" in footer
    assert "星號 `*...*` 內的動作、表情與狀態描寫也屬於玩家可見自然語言" in footer
    assert "不要因為下方格式範例是中文就把動作描寫寫成中文" in footer


def test_proactive_prompt_pins_visible_message_language() -> None:
    prompt = build_proactive_prompt(
        ProactiveContext(
            character=_character(),
            trigger=ProactiveTrigger.TICK,
            now=datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc),
            current_activity=None,
            upcoming_activities=[],
            schedule=None,
            idle_minutes=120.0,
            sent_today=0,
            last_proactive_at=None,
            operator_primary_language="en-US",
        ),
    )

    _assert_visible_language_rule(prompt)
    assert "用繁體中文" not in prompt


def test_feed_post_prompt_pins_visible_content_text_language() -> None:
    prompt = build_feed_prompt(
        FeedComposerInput(
            character=_character(),
            kind=FeedKind.MOOD,
            source=FeedSource.silence(),
            hint="Share a small thought.",
            context_snippets=("The user has been away for two hours.",),
            image_required=True,
            operator_primary_language="en-US",
        ),
    )

    _assert_visible_language_rule(prompt)
    assert "繁體中文" not in prompt
    assert "content_text" in prompt


def test_feed_comment_reply_prompt_pins_visible_reply_language() -> None:
    post = FeedPost.create(
        character_id="char-1",
        kind=FeedKind.MOOD,
        content_text="I kept thinking about yesterday.",
        source=FeedSource.manual(),
        id="post-1",
    )
    comment = FeedComment.create(
        post_id=post.id,
        content_text="Tell me more.",
        id="comment-1",
    )

    prompt = build_feed_comment_reply_prompt(
        FeedCommentReplyInput(
            character=_character(),
            post=post,
            user_comments=(comment,),
            operator_primary_language="en-US",
        ),
    )

    _assert_visible_language_rule(prompt)
    assert "繁體中文" not in prompt


def test_busy_follow_up_prompt_pins_visible_reply_language() -> None:
    now = datetime(2026, 5, 26, 9, 30, tzinfo=timezone.utc)
    prompt = build_follow_up_prompt(
        PendingFollowUpComposeInput(
            character=_character(),
            queued_messages=(
                PendingFollowUpMessage.new(
                    content="Can we talk later?",
                    queued_at=datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc),
                ),
            ),
            brief_reply="Give me a minute.",
            defer_reason="in a meeting",
            queued_at=datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc),
            just_finished_activity=None,
            current_activity=None,
            recent_dialogue_summary=None,
            now=now,
            operator_primary_language="en-US",
        ),
    )

    _assert_visible_language_rule(prompt)


def test_scheduled_promise_prompt_pins_visible_message_language() -> None:
    now = datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc)
    prompt = build_scheduled_promise_prompt(
        ScheduledPromiseComposeInput(
            character=_character(),
            promise_intent="Remind the user to get up.",
            promise_text="Wake me up at 9.",
            scheduled_for=now,
            current_activity=None,
            just_finished_activity=None,
            recent_dialogue_summary=None,
            now=now,
            operator_primary_language="en-US",
        ),
    )

    _assert_visible_language_rule(prompt)


def test_scene_access_prompt_pins_visible_verdict_language() -> None:
    prompt = build_scene_access_prompt(
        SceneAccessContext(
            character_id="char-1",
            operator_id="user-1",
            character_name="Mio",
            operator_primary_language="en-US",
        ),
    )

    _assert_visible_language_rule(prompt)
    assert "reason_for_user" in prompt
    assert "suggested_opener" in prompt


def test_character_draft_prompt_pins_visible_json_text_language() -> None:
    prompt = build_character_draft_instruction(
        "A quiet barista with a secret.",
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(prompt)
    assert "third_person_pronoun" in prompt
    assert "代稱也必須是上方主要語言裡自然會使用的代稱" in prompt
    assert "he / she / they / it" in prompt
    assert "所有內容使用繁體中文" not in prompt


def test_companion_draft_prompt_pins_visible_json_text_language() -> None:
    prompt = build_companion_draft_instruction(
        CompanionGenerationContext(
            character_name="Mio",
            character_summary="A quiet barista.",
            operator_primary_language="en-US",
        ),
        wanted=2,
    )

    _assert_visible_language_rule(prompt)
    assert "全部使用繁體中文" not in prompt


def test_fusion_planner_prompt_pins_visible_outline_language() -> None:
    prompt = build_fusion_planner_prompt(
        prompt="Let these characters share a mysterious morning.",
        briefs=(_fusion_brief(),),
        previous_outline=None,
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(prompt)
    assert "title" in prompt
    assert "premise" in prompt


def test_fusion_writer_prompt_pins_visible_prose_language() -> None:
    outline = _fusion_outline()
    prompt = build_fusion_writer_prompt(
        prompt="Let these characters share a mysterious morning.",
        outline=outline,
        beat=outline.beats[0],
        briefs=(_fusion_brief(),),
        previously_summary="",
        previous_tail="",
        regenerate_hint=None,
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(prompt)
    assert "全篇用中文" not in prompt
    assert "中文短篇" not in prompt


def test_fusion_polisher_prompts_pin_visible_rewrite_language() -> None:
    outline = _fusion_outline()
    whole_prompt = build_fusion_polisher_whole_prompt(
        prompt="Let these characters share a mysterious morning.",
        outline=outline,
        draft_text="Mio found a note.\n\nShe answered it.",
        briefs=(_fusion_brief(),),
        critique=None,
        round_index=0,
        operator_primary_language="en-US",
    )
    spot_prompt = build_fusion_polisher_spot_prompt(
        prompt="Let these characters share a mysterious morning.",
        outline=outline,
        cast="Mio",
        target_index=0,
        target_text="Mio found a note.",
        context_before="",
        context_after="She answered it.",
        findings=(
            FusionCritiqueFinding.create(
                kind="vague",
                issue="The moment is too abstract.",
                suggestion="Ground it in a concrete action.",
                paragraph_index=0,
            ),
        ),
        ambient_findings=(),
        round_index=0,
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(whole_prompt)
    _assert_visible_language_rule(spot_prompt)


def test_branching_planner_prompts_pin_visible_outline_language() -> None:
    root_prompt = build_branching_root_prompt(
        prompt="Start with a quiet message.",
        briefs=(_fusion_brief(),),
        total_segments=6,
        operator_primary_language="en-US",
    )
    children_prompt = build_branching_children_prompt(
        prompt="Start with a quiet message.",
        briefs=(_fusion_brief(),),
        parent_summary="Mio finds a message.",
        path_context="Opening path.",
        depth=1,
        total_segments=6,
        is_ending=False,
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(root_prompt)
    _assert_visible_language_rule(children_prompt)


def test_branching_director_prompts_pin_visible_scene_language() -> None:
    node = _branching_node()
    narrate_prompt = build_branching_narrate_prompt(
        node=node,
        briefs=(_fusion_brief(),),
        previous_turns=(),
        player_input="",
        operator_primary_language="en-US",
    )
    response_prompt = build_branching_response_prompt(
        node=node,
        briefs=(_fusion_brief(),),
        previous_turns=(),
        exchanges=(),
        player_input="I check the message.",
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(narrate_prompt)
    _assert_visible_language_rule(response_prompt)


def test_branching_polisher_prompt_pins_visible_rewrite_language() -> None:
    prompt = build_branching_polisher_prompt(
        node=_branching_node(),
        narration_text="Mio found a note.",
        critique=DramaCritique.create(
            severity=2,
            summary="Too vague.",
            findings=(
                DramaCritiqueFinding.create(
                    kind="vague",
                    issue="The moment is too abstract.",
                    suggestion="Ground it in a concrete action.",
                    paragraph_index=0,
                ),
            ),
        ),
        briefs=(_fusion_brief(),),
        previous_turns=(),
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(prompt)


def test_story_arc_prompt_pins_visible_arc_language() -> None:
    prompt = build_story_arc_prompt(
        character=_character(),
        start_date=date(2026, 5, 26),
        duration_days=21,
        beat_count_hint=5,
        hint="A quiet turning point.",
        recent_dialogue_summary="",
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(prompt)
    assert "title" in prompt
    assert "summary" in prompt


def test_story_expander_prompt_pins_visible_narrative_language() -> None:
    prompt = build_story_expander_prompt(
        seed=StorySeed.create(seed_text="Found a strange note."),
        character_name="Mio",
        character_summary="A quiet barista.",
        speaking_style="soft",
        world_frame="modern",
        scene=None,
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(prompt)
    assert "narrative" in prompt


def test_self_reflection_prompt_pins_visible_memoir_language() -> None:
    prompt = build_reflection_prompt(
        ReflectionGeneratorInput(
            character_id="char-1",
            operator_id="user-1",
            character_name="Mio",
            period="week",
            period_start=date(2026, 5, 19),
            period_end=date(2026, 5, 26),
            high_salience_memories=(
                MemoryItem.create(
                    character_id="char-1",
                    kind=MemoryKind.EPISODIC,
                    content="The user shared a stressful week.",
                    salience=0.8,
                ),
            ),
            operator_primary_language="en-US",
        ),
    )

    _assert_visible_language_rule(prompt)
    assert "memoir" in prompt


def _operator(language: str) -> OperatorProfile:
    return OperatorProfile(
        id="op-1",
        display_name="Alex",
        primary_language=language,
    )


def test_post_turn_prompt_pins_visible_state_language() -> None:
    """The post-turn processor writes two player-visible state fields
    (``emotion`` + ``current_intent``). Regression B2: the baseline
    prompt hardcoded 「中文」 for both, so a non-Chinese operator saw a
    Chinese Current intent. The prompt must now carry the operator
    language fact and must NOT force Chinese for those fields."""
    prompt = build_post_turn_prompt(
        character=_character(),
        user_message="I live in Tokyo.",
        assistant_message="That sounds lovely.",
        recent_messages=[],
        operator=_operator("en-US"),
    )

    _assert_visible_language_rule(prompt)
    # The emotion word and current_intent must defer to the operator
    # language, not be pinned to Chinese.
    assert "情緒詞（中文" not in prompt
    assert "一句話、中文、第一人稱" not in prompt


def test_idle_drift_prompt_pins_visible_state_language() -> None:
    """Idle drift is the second writer of ``current_intent``. Same B2
    gap: no operator-language fact, fully Chinese scaffold. It must now
    carry the language fact so English operators get an English intent."""
    prompt = build_idle_drift_prompt(
        character=_character(),
        idle_minutes=4320.0,
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(prompt)


def test_synthetic_arc_localizes_fallback_template_english() -> None:
    """The LLM-free synthetic fallback arc (used on provider=fake, LLM
    error, or unparseable output) hardcoded a zh-TW template. For an
    en-US operator it must emit English static template strings."""
    arc = build_synthetic_arc(
        character=_character(),
        start_date=date(2026, 5, 26),
        duration_days=21,
        beat_count=5,
        hint=None,
        operator_primary_language="en-US",
    )

    blob = arc.title + " " + arc.premise + " " + " ".join(
        b.title + " " + b.summary for b in arc.beats
    )
    # No Han characters anywhere in the English fallback.
    assert not any("一" <= ch <= "鿿" for ch in blob), blob
    assert "next chapter" in arc.premise.lower() or "chapter" in arc.premise.lower()


def test_synthetic_arc_localizes_fallback_template_japanese() -> None:
    arc = build_synthetic_arc(
        character=_character(),
        start_date=date(2026, 5, 26),
        duration_days=21,
        beat_count=5,
        hint=None,
        operator_primary_language="ja-JP",
    )

    blob = arc.premise + " " + " ".join(b.summary for b in arc.beats)
    # Japanese template must contain kana (hiragana/katakana range),
    # proving it is not the zh-TW template (which is kana-free).
    assert any("぀" <= ch <= "ヿ" for ch in blob), blob


def test_synthetic_arc_falls_back_to_zh_tw_for_unknown_language() -> None:
    """Unknown / unsupported tags fall back to the zh-TW template — the
    documented default so the path never crashes."""
    arc = build_synthetic_arc(
        character=_character(),
        start_date=date(2026, 5, 26),
        duration_days=21,
        beat_count=5,
        hint=None,
        operator_primary_language="fr-FR",
    )

    blob = arc.premise + " " + " ".join(b.summary for b in arc.beats)
    assert any("一" <= ch <= "鿿" for ch in blob), blob


# ---------------------------------------------------------------------------
# Q2 batch: remaining player/operator-visible LLM jobs. Each writes text a
# non-Chinese operator can see (chat ack, memory content, goals panel,
# persona projection, admin report, proactive-attempt log). The prompt must
# carry the operator-language fact and drop any hardcoded Chinese mandate.
# ---------------------------------------------------------------------------


def _activity() -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 5, 26, 10, 0, tzinfo=timezone.utc),
        description="Team meeting",
        category="work",
        busy_score=0.9,
    )


def test_busy_decider_prompt_pins_visible_brief_ack_language() -> None:
    """The 短回覆 (brief ack) is sent straight to the player in chat, so it
    must follow the operator's content language, not be Chinese-pinned."""
    prompt = build_busy_decider_prompt(
        character=_character(),
        user_message="Can you talk?",
        current_activity=_activity(),
        recent_dialogue_summary=None,
        recent_proactive_attempts=(),
        relationship_context_lines=(),
        interaction_context_lines=(),
        now=datetime(2026, 5, 26, 9, 30, tzinfo=timezone.utc),
        local_tz=timezone.utc,
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(prompt)
    # The label lines stay Chinese protocol markers, but the ack value must
    # be pinned to the operator language.
    assert "短回覆" in prompt


def test_aftermath_prompt_pins_visible_residue_language() -> None:
    """The residue summary is folded verbatim into player-visible memory
    content, so it must not mandate a Chinese short sentence."""
    prompt = build_aftermath_prompt(
        character=_character(),
        activity=_activity(),
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(prompt)
    assert "中文短句" not in prompt


def test_goal_reviewer_prompt_pins_visible_notes_and_goals_language() -> None:
    """new_goals.content and review notes render in PlayerGoalsPanel, so
    they must not be pinned to Chinese."""
    prompt = build_goal_reviewer_prompt(
        character=_character(),
        active_goals=[],
        recent_messages=[],
        max_new_goals=3,
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(prompt)
    assert "不超過一句中文" not in prompt


def test_memory_consolidator_prompt_pins_merged_content_language() -> None:
    """Merged memory content shows in MemoryBrowserPanel — drop the
    （中文為主） bias and pin to the operator language."""
    cluster = [
        MemoryItem.create(
            character_id="char-1",
            kind=MemoryKind.SEMANTIC,
            content="The user lives in Tokyo.",
            salience=0.8,
        ),
        MemoryItem.create(
            character_id="char-1",
            kind=MemoryKind.SEMANTIC,
            content="The user moved to Tokyo last year.",
            salience=0.7,
        ),
    ]
    prompt = build_memory_consolidator_prompt(
        cluster, operator_primary_language="en-US",
    )

    _assert_visible_language_rule(prompt)
    assert "中文為主" not in prompt


def test_persona_extractor_prompt_pins_visible_fact_value_language() -> None:
    """Extracted fact values surface in the persona projection panel, so the
    value must follow the operator language (the quote stays verbatim)."""
    prompt = build_persona_extractor_prompt(
        operator=_operator("en-US"),
        current_persona=OperatorPersona.empty(
            character_id="char-1", operator_id="op-1",
        ),
        user_message="I'm a backend engineer.",
        assistant_message="That's cool.",
        recent_messages=[],
    )

    _assert_visible_language_rule(prompt)
    # The only example value must no longer be a hardcoded Chinese fact.
    assert "後端工程師" not in prompt


def test_experiment_analysis_prompt_pins_visible_report_language() -> None:
    """The operator-triggered A/B analysis report renders in the admin UI,
    so replace the hardcoded 繁體中文 mandate with the operator-language
    fact."""
    prompt = build_experiment_analysis_prompt(
        {"experiment_id": "exp-1", "buckets": []},
        operator_primary_language="en-US",
    )

    _assert_visible_language_rule(prompt)
    assert "請用繁體中文輸出" not in prompt


def test_intention_judge_prompt_pins_visible_reason_language() -> None:
    """The proactive intention judge's ``reason`` renders in
    ChannelProactiveAttemptLog.vue, so it must follow the operator
    language."""
    prompt = build_intention_judge_prompt(
        ProactiveContext(
            character=_character(),
            trigger=ProactiveTrigger.TICK,
            now=datetime(2026, 5, 26, 9, 0, tzinfo=timezone.utc),
            current_activity=None,
            upcoming_activities=[],
            schedule=None,
            idle_minutes=120.0,
            sent_today=0,
            last_proactive_at=None,
            operator_primary_language="en-US",
        ),
    )

    _assert_visible_language_rule(prompt)


# --- I18N_HARDENING plan Phase 4 additions --------------------------------
# These builders produce player-visible content (arc-template titles/summary,
# creation-intake follow-ups, persona projection narrative) and must carry
# the same operator-language fact so en/ja operators don't get zh output.

from kokoro_link.application.services.arc_template_intake_service import (
    BeatContext,
    BeatDraft,
    _build_beat_options_prompt,
    _build_beat_summary_prompt,
    _build_full_draft_prompt,
    _build_meta_prompt,
    _build_premise_prompt,
)
from kokoro_link.application.services.character_creation_intake_service import (
    CharacterCreationDraftContext,
    _build_prompt as build_creation_intake_prompt,
)
from kokoro_link.application.dto.character import InitialRelationshipPayload
from kokoro_link.application.services.operator_persona_projection_service import (
    _ProjectionFact,
    _build_projection_prompt,
)
from kokoro_link.infrastructure.prompt.default import _render_presence_frame_block
from kokoro_link.domain.value_objects.presence_frame import PresenceFrame


def _beat_context() -> BeatContext:
    return BeatContext(
        template_title="T",
        premise="p",
        theme="ambition",
        tone="daily",
        duration_days=14,
        world_frames=("modern",),
        beat_position=0,
        total_beats=6,
        day_offset=0,
        tension="rising",
    )


def test_arc_intake_builders_pin_visible_language() -> None:
    lang = "en-US"
    for prompt in (
        _build_meta_prompt("a pitch", lang),
        _build_premise_prompt(
            logline="l", start_state="s", end_state="e",
            tone="daily", operator_primary_language=lang,
        ),
        _build_beat_options_prompt(_beat_context(), lang),
        _build_beat_summary_prompt(
            beat=BeatDraft(sequence=0, day_offset=0, title="t"),
            context=_beat_context(),
            operator_primary_language=lang,
        ),
        _build_full_draft_prompt(pitch="p", hint="", operator_primary_language=lang),
    ):
        _assert_visible_language_rule(prompt)


def test_arc_intake_builders_drop_hardcoded_chinese_length_mandate() -> None:
    # The three "中文 …字" mandates are replaced with language-neutral
    # length guidance so titles/summaries follow the operator language.
    meta = _build_meta_prompt("p", "en-US")
    full = _build_full_draft_prompt(pitch="p", hint="", operator_primary_language="en-US")
    assert "簡短中文標題" not in meta
    assert "中文 8–14 字" not in full


def test_creation_intake_prompt_pins_visible_language() -> None:
    prompt = build_creation_intake_prompt(
        draft=CharacterCreationDraftContext(name="Mio"),
        relationship=InitialRelationshipPayload(),
        current_locale="en-US",
        round_index=0,
    )
    _assert_visible_language_rule(prompt)


def test_persona_projection_prompt_pins_visible_language() -> None:
    fact = _ProjectionFact(
        field_id="f1",
        layer=1,
        field_key="name",
        label="名字",
        value="Lex",
        confidence=0.9,
        last_updated="2026-01-01T00:00:00+00:00",
    )
    prompt = _build_projection_prompt(_character(), (fact,), language="en-US")
    _assert_visible_language_rule(prompt)


def test_presence_block_derives_channel_label_in_operator_language() -> None:
    # Plan #1 / D4: the "current interface" line must follow the operator
    # language, not a client-sent zh display_name.
    en = _render_presence_frame_block(
        PresenceFrame.web_dm(), operator_language="en-US",
    )
    joined = "\n".join(en)
    assert "站內私訊" not in joined
    assert "in-app direct message" in joined
    ja = _render_presence_frame_block(
        PresenceFrame.web_dm(), operator_language="ja-JP",
    )
    assert "站內私訊" not in "\n".join(ja)
