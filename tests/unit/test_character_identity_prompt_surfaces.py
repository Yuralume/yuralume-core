"""Character identity facts are injected into LLM prompt surfaces."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.application.services.chat_assist_service import ChatAssistService
from kokoro_link.application.services.fusion_character_brief import (
    FusionCharacterBriefBuilder,
)
from kokoro_link.contracts.feed import FeedComposerInput
from kokoro_link.contracts.feed_comment_reply import FeedCommentReplyInput
from kokoro_link.contracts.proactive import ProactiveContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.feed_comment import FeedComment
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
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
    _build_prompt as build_proactive_intention_prompt,
)
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_identity_lines,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.schedule.llm_planner import (
    _build_prompt as build_schedule_prompt,
)
from kokoro_link.infrastructure.story.llm_arc_planner import (
    _build_prompt as build_story_arc_prompt,
)
from kokoro_link.infrastructure.story.llm_expander import (
    _build_prompt as build_story_expander_prompt,
)


def _character(**overrides) -> Character:  # noqa: ANN003
    base = dict(
        name="Ren",
        summary="A quiet archivist.",
        personality=["calm"],
        interests=["old maps"],
        speaking_style="soft and precise",
        boundaries=[],
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation="androgynous archivist",
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )
    base.update(overrides)
    return Character.create(**base)


def _assert_identity(prompt: str) -> None:
    assert "- 性別身份：非二元" in prompt
    assert "- 第三人稱代稱：TA" in prompt
    assert "- 視覺性別呈現：androgynous archivist" in prompt


def test_identity_renderer_keeps_unset_as_no_guessing_fact() -> None:
    prompt = "\n".join(
        render_character_identity_lines(
            _character(
                gender_identity="",
                third_person_pronoun="",
                visual_gender_presentation="",
            )
        )
    )

    assert "不要從名字、簡介或外觀推斷" in prompt
    assert "優先使用角色名或中立表述" in prompt
    assert "不要由代稱推斷畫面" in prompt


def test_main_chat_prompt_includes_character_identity_facts() -> None:
    character = _character()
    prompt = DefaultPromptContextBuilder().build(
        character=character,
        conversation=Conversation(
            id="conv-1", character_id=character.id, messages=(),
        ),
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="Hi",
        now=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        today_local=date(2026, 6, 1),
    )

    _assert_identity(prompt)


def test_proactive_prompts_include_character_identity_facts() -> None:
    context = ProactiveContext(
        character=_character(),
        trigger=ProactiveTrigger.TICK,
        now=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        current_activity=None,
        upcoming_activities=[],
        schedule=None,
        idle_minutes=120.0,
        sent_today=0,
        last_proactive_at=None,
    )

    _assert_identity(build_proactive_prompt(context))
    _assert_identity(build_proactive_intention_prompt(context))


def test_schedule_and_feed_prompts_include_character_identity_facts() -> None:
    character = _character()
    schedule_prompt = build_schedule_prompt(
        character=character,
        date_=date(2026, 6, 1),
        local_tz=timezone.utc,
    )
    feed_prompt = build_feed_prompt(
        FeedComposerInput(
            character=character,
            kind=FeedKind.MOOD,
            source=FeedSource.silence(),
            hint="Share something small.",
            context_snippets=(),
            image_required=False,
        ),
    )
    post = FeedPost.create(
        character_id=character.id,
        kind=FeedKind.MOOD,
        content_text="I found an old map today.",
        source=FeedSource.manual(),
    )
    reply_prompt = build_feed_comment_reply_prompt(
        FeedCommentReplyInput(
            character=character,
            post=post,
            user_comments=(
                FeedComment.create(post_id=post.id, content_text="Tell me more."),
            ),
        ),
    )

    _assert_identity(schedule_prompt)
    _assert_identity(feed_prompt)
    _assert_identity(reply_prompt)


@pytest.mark.asyncio
async def test_chat_assist_prompt_includes_character_identity_facts() -> None:
    service = ChatAssistService(
        character_service=object(),  # type: ignore[arg-type]
        active_llm_provider=object(),  # type: ignore[arg-type]
    )

    prompt = await service._build_prompt(_character(), user_id="user-1", count=3)

    _assert_identity(prompt)


def test_story_and_multi_character_prompts_include_identity_facts() -> None:
    character = _character()
    arc_prompt = build_story_arc_prompt(
        character=character,
        start_date=date(2026, 6, 1),
        duration_days=21,
        beat_count_hint=5,
        hint="A map starts changing.",
    )
    expander_prompt = build_story_expander_prompt(
        seed=StorySeed.create(seed_text="Found a map that redraws itself."),
        character_name=character.name,
        character_summary=character.summary,
        speaking_style=character.speaking_style,
        world_frame="modern",
        scene=None,
        character=character,
    )
    brief = FusionCharacterBriefBuilder(
        memory_repository=None,
    ).build_persona_only(character)

    _assert_identity(arc_prompt)
    _assert_identity(expander_prompt)
    _assert_identity(brief.text)
