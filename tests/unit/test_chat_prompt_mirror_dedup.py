"""Cross-channel mirror de-duplication for chat prompt material (P7a).

A proactive push is delivered to the web thread *and* to the bound
messaging thread, so the same sentence exists as two rows under two
conversations. The prompt is built from the merged cross-source timeline
(``recent_messages_for_character``), which used to hand both copies to
every downstream block: the observed 2026-07-29 dump showed one line
twice in 「近期對話」 and twice again in the 「你本對話最近自己說過的話」
rail — four appearances of one sentence — and the diversity statistics
scored 1.000 self-similarity against the line's own mirror.

The characterization tests below reproduce that (4 → 2, one appearance
per intentional rail) and pin the structural rule: exact match after
whitespace normalisation, inside a short window, nothing fuzzy.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageAttachment,
    MessageKind,
    MessageRole,
)
from kokoro_link.domain.services.mirrored_message_dedup import (
    dedupe_mirrored_messages,
)
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

NOW = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)

# Short enough to survive the self-lines rail's snippet clip verbatim, so
# the whole prompt can be searched for it as an exact substring.
MIRRORED_LINE = "今天午後的雲層特別厚，我在窗邊看了好一陣子。"


def _at(minutes_before: int, seconds_before: int = 0) -> datetime:
    return NOW - timedelta(minutes=minutes_before, seconds=seconds_before)


def _assistant(content: str, when: datetime) -> Message:
    return Message(
        role=MessageRole.ASSISTANT, content=content, created_at=when,
    )


def _user(content: str, when: datetime) -> Message:
    return Message(role=MessageRole.USER, content=content, created_at=when)


# --------------------------------------------------------------------------
# structural rule (pure domain service)
# --------------------------------------------------------------------------


def test_mirror_pair_collapses_to_one_copy() -> None:
    web_copy = _assistant(MIRRORED_LINE, _at(70))
    telegram_copy = _assistant(MIRRORED_LINE, _at(69, 7))
    merged = [_user("早安", _at(180)), web_copy, telegram_copy]

    result = dedupe_mirrored_messages(merged)

    assert [m.content for m in result] == ["早安", MIRRORED_LINE]
    assert result[1].created_at == web_copy.created_at


def test_the_survivor_does_not_depend_on_who_is_asking() -> None:
    """The earliest copy wins, whichever thread is being rendered.

    This reverses an earlier rule (the rendered conversation's own row
    survived, so a transcript kept its own timestamps) and the reversal
    is the point. The dialogue checkpoint identifies its coverage
    boundary by the surviving copy's timestamp plus a fingerprint of it;
    the prompt named a preferred conversation and the background
    checkpoint updater — which has no conversation in hand — could not,
    so the two picked different rows for the same sentence and then
    disagreed about whether the boundary was covered. The visible
    symptom is a line appearing inside the summary *and* verbatim in the
    transcript under it.

    So the rule takes no caller-supplied input at all: same list in,
    same survivor out, on both sides of the feature.
    """
    telegram_copy = _assistant(MIRRORED_LINE, _at(70))
    web_copy = _assistant(MIRRORED_LINE, _at(69, 7))
    web_conversation = Conversation.start(character_id="c1", source="web")
    web_conversation = web_conversation.append(web_copy)

    result = dedupe_mirrored_messages([telegram_copy, web_copy])

    assert len(result) == 1
    # The telegram row landed first, and it survives even though the web
    # conversation is the one a prompt would be rendering.
    assert result[0].created_at == telegram_copy.created_at
    assert web_conversation.messages[-1].created_at == web_copy.created_at


def test_whitespace_only_differences_are_the_same_message() -> None:
    web_copy = _assistant(MIRRORED_LINE, _at(70))
    rewrapped = _assistant(f"  {MIRRORED_LINE}\n ", _at(69, 7))

    result = dedupe_mirrored_messages([web_copy, rewrapped])

    assert len(result) == 1


def test_repeat_outside_the_window_is_kept() -> None:
    """Only fan-out collapses. A line the character genuinely said again
    hours later is real self-repetition — the anti-repetition rails exist
    precisely to see it."""
    earlier = _assistant(MIRRORED_LINE, _at(300))
    later = _assistant(MIRRORED_LINE, _at(70))

    result = dedupe_mirrored_messages([earlier, later])

    assert len(result) == 2


def test_only_exact_matches_collapse() -> None:
    """No fuzzy similarity: one extra clause makes it a different message."""
    original = _assistant(MIRRORED_LINE, _at(70))
    near_miss = _assistant(MIRRORED_LINE + "後來還下了一點雨。", _at(69, 7))

    result = dedupe_mirrored_messages([original, near_miss])

    assert len(result) == 2


def test_same_text_from_different_roles_is_kept() -> None:
    assistant_line = _assistant("好啊", _at(70))
    user_line = _user("好啊", _at(69, 7))

    result = dedupe_mirrored_messages([assistant_line, user_line])

    assert len(result) == 2


def test_payloadless_tool_rows_are_never_collapsed() -> None:
    """Two empty tool-only rows carry nothing comparable — merging them
    would silently drop an artifact from the vision inventory."""
    first = Message(
        role=MessageRole.ASSISTANT,
        content="",
        kind=MessageKind.TOOL_ONLY,
        created_at=_at(70),
    )
    second = Message(
        role=MessageRole.ASSISTANT,
        content="",
        kind=MessageKind.TOOL_ONLY,
        created_at=_at(69, 7),
    )

    result = dedupe_mirrored_messages([first, second])

    assert len(result) == 2


def test_same_caption_with_different_attachments_is_kept() -> None:
    def _with_url(url: str, when: datetime) -> Message:
        return Message(
            role=MessageRole.ASSISTANT,
            content="拍好了",
            attachments=(MessageAttachment(kind="image", url=url),),
            created_at=when,
        )

    result = dedupe_mirrored_messages([
        _with_url("/uploads/a.png", _at(70)),
        _with_url("/uploads/b.png", _at(69, 7)),
    ])

    assert len(result) == 2


def test_order_is_preserved_and_unrelated_messages_untouched() -> None:
    messages = [
        _user("早安", _at(180)),
        _assistant("早安呀", _at(179)),
        _assistant(MIRRORED_LINE, _at(70)),
        _assistant(MIRRORED_LINE, _at(69, 7)),
        _user("剛看到你的訊息", _at(5)),
    ]

    result = dedupe_mirrored_messages(messages)

    assert [m.content for m in result] == [
        "早安", "早安呀", MIRRORED_LINE, "剛看到你的訊息",
    ]


# --------------------------------------------------------------------------
# characterization: the assembled chat prompt
# --------------------------------------------------------------------------


class _PromptRecordingModel:
    provider_id = "recording"
    supports_vision = False

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        self.prompts.append(prompt)
        return "嗯，我在聽。"

    async def generate_stream(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        self.prompts.append(prompt)
        yield "嗯，我在聽。"

    async def list_models(self) -> list[str]:
        return ["recording"]


class _RecordingPromptBuilder:
    def __init__(self) -> None:
        self.last_recent_messages: list[Message] = []

    def build(self, **kwargs) -> str:  # noqa: ANN003
        self.last_recent_messages = list(kwargs["recent_messages"])
        return "prompt"


def _build_service(
    *, prompt_builder=None,  # noqa: ANN001
) -> tuple[ChatService, CharacterService, InMemoryConversationRepository, _PromptRecordingModel]:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    model = _PromptRecordingModel()
    registry = InMemoryChatModelRegistry(default_provider_id="recording")
    registry.register(model)
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder or DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    return chat_service, character_service, conversation_repository, model


async def _seed_mirrored_history(
    character_service: CharacterService,
    conversation_repository: InMemoryConversationRepository,
    *,
    web_first: bool = True,
) -> tuple[str, Conversation]:
    created = await character_service.create_character(
        CreateCharacterRequest(name="芊璃", personality=["calm"], interests=[]),
    )
    web_at = _at(70) if web_first else _at(69, 7)
    telegram_at = _at(69, 7) if web_first else _at(70)

    web = Conversation.start(character_id=created.id, source="web")
    web = web.append(_user("早安", _at(180)))
    web = web.append(_assistant(MIRRORED_LINE, web_at))
    await conversation_repository.save(web)

    telegram = Conversation.start(character_id=created.id, source="telegram")
    telegram = telegram.append(_assistant(MIRRORED_LINE, telegram_at))
    await conversation_repository.save(telegram)
    return created.id, web


def _section(prompt: str, start: str, end: str | None) -> str:
    body = prompt.split(start, maxsplit=1)[1]
    return body if end is None else body.split(end, maxsplit=1)[0]


def _count(haystack: str, needle: str) -> int:
    return haystack.count(needle)


@pytest.mark.asyncio
async def test_mirrored_line_appears_once_per_prompt_rail() -> None:
    """Characterization of the芊璃 dump: 4 appearances → 1 per rail.

    Two of the four were mirror copies inside 「近期對話」 and the
    「你本對話最近自己說過的話」 rail; the two that remain are the two
    *different* rails deliberately surfacing the same line once each.
    """
    chat_service, character_service, conversations, model = _build_service()
    character_id, web = await _seed_mirrored_history(
        character_service, conversations,
    )

    await chat_service.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=web.id,
            message="剛剛在忙，現在有空了",
        ),
    )

    prompt = model.prompts[0]
    assert _count(prompt, MIRRORED_LINE) == 2

    self_lines = _section(
        prompt, "你本對話最近自己說過的話", "近期對話：",
    )
    assert _count(self_lines, MIRRORED_LINE) == 1

    transcript = _section(prompt, "近期對話：", "最新使用者訊息")
    assert _count(transcript, MIRRORED_LINE) == 1


@pytest.mark.asyncio
async def test_prompt_material_keeps_the_copy_that_was_delivered_first(
) -> None:
    """The prompt keeps the original delivery even when it landed on the
    *other* channel — the same copy the background checkpoint updater
    keeps, which is the whole reason the choice is not the caller's."""
    prompt_builder = _RecordingPromptBuilder()
    chat_service, character_service, conversations, _ = _build_service(
        prompt_builder=prompt_builder,
    )
    # ``web_first=False``: the telegram row is the earlier one, and the
    # conversation being rendered is the web one.
    character_id, web = await _seed_mirrored_history(
        character_service, conversations, web_first=False,
    )

    await chat_service.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=web.id,
            message="剛剛在忙，現在有空了",
        ),
    )

    contents = [m.content for m in prompt_builder.last_recent_messages]
    assert contents == ["早安", MIRRORED_LINE]
    survivor = prompt_builder.last_recent_messages[-1]
    assert survivor.created_at < web.messages[-1].created_at


@pytest.mark.asyncio
async def test_diversity_evidence_no_longer_self_matches_the_mirror() -> None:
    """The 1.000 self-similarity false signal was a line embedded against
    its own mirror copy — after the collapse there is nothing to pair."""
    from kokoro_link.infrastructure.diversity.reply_evidence import (
        build_reply_diversity_evidence,
    )

    class _ConstantEmbedder:
        is_operational = True

        async def embed_many(self, texts: Sequence[str]):  # noqa: ANN001
            return [[1.0, 0.0] for _ in texts]

    merged = [
        _user("早安", _at(180)),
        _assistant(MIRRORED_LINE, _at(70)),
        _assistant(MIRRORED_LINE, _at(69, 7)),
    ]

    before = await build_reply_diversity_evidence(
        recent_messages=merged, embedder=_ConstantEmbedder(),
    )
    assert before.assistant_line_count == 2
    assert before.max_self_similarity == pytest.approx(1.0)

    after = await build_reply_diversity_evidence(
        recent_messages=dedupe_mirrored_messages(merged),
        embedder=_ConstantEmbedder(),
    )
    assert after.assistant_line_count == 1
    assert after.max_self_similarity is None
