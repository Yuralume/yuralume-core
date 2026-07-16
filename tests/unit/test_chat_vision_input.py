"""BDD for user image attachments → chat model pipeline.

Key paths:

* Vision-capable model + object exists in storage → URL is base64-encoded
  as a ``data:`` URL (reliable regardless of network reachability).
* Vision-capable model + object missing + ``public_base_url`` → falls
  back to absolute HTTP URL.
* Vision-capable model + object missing + no public_base_url → drops
  URL and adds text placeholder.
* Non-vision model → URLs dropped, prompt gets a text placeholder.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.active_llm_provider import (
    PreferenceBackedActiveLLMProvider,
)
from kokoro_link.application.services.feature_keys import (
    FEATURE_CHAT,
    FEATURE_GROUP_MULTIMODAL_PERCEPTION,
    FEATURE_IMAGE_RECOGNITION,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import (
    ChatService,
    _content_tolerance_for_content_mode,
)
from kokoro_link.application.services.nsfw_mode import NsfwModeService
from kokoro_link.contracts.llm import ChatModelPort, ImageInputRejectedError
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.observability.turn_recorder import (
    BackgroundTurnRecorder,
)
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_records import (
    InMemoryTurnRecordRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage


class _CapturingModel(ChatModelPort):
    def __init__(
        self,
        *,
        provider_id: str = "fake",
        supports_vision: bool,
        reply: str = "收到了",
    ) -> None:
        self.provider_id = provider_id
        self.supports_vision = supports_vision
        self.reply = reply
        self.last_prompt: str | None = None
        self.last_image_urls: tuple[str, ...] | None = None
        self.last_model: str | None = None
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.last_prompt = prompt
        self.last_image_urls = tuple(image_urls)
        self.last_model = model
        self.calls.append(
            {"prompt": prompt, "image_urls": tuple(image_urls), "model": model},
        )
        return self.reply

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        self.last_prompt = prompt
        self.last_image_urls = tuple(image_urls)
        self.last_model = model
        self.calls.append(
            {"prompt": prompt, "image_urls": tuple(image_urls), "model": model},
        )
        yield self.reply

    async def list_models(self) -> list[str]:
        return [self.provider_id]

    def with_supports_vision(self, value: bool) -> "_CapturingModel":
        """Mimic the real adapters' routing-level vision binder. ``copy.copy``
        keeps the shared ``calls`` list so a test can inspect what the
        bound clone received via the base's ``calls``."""
        clone = copy.copy(self)
        clone.supports_vision = value
        return clone


class _RoutingActiveProvider:
    def __init__(
        self,
        *,
        chat_model: _CapturingModel,
        chat_model_id: str = "text-main",
        image_model: _CapturingModel | None = None,
        image_model_id: str = "vision-caption",
    ) -> None:
        self.chat_model = chat_model
        self.chat_model_id = chat_model_id
        self.image_model = image_model
        self.image_model_id = image_model_id
        self.resolve_calls: list[dict[str, Any]] = []
        self.resolve_model_id_calls: list[dict[str, Any]] = []

    async def resolve(
        self,
        feature_key: str | None = None,
        *,
        character=None,
        content_tolerance: str | None = None,
    ) -> _CapturingModel:
        self.resolve_calls.append(
            {
                "feature_key": feature_key,
                "character_id": getattr(character, "id", None),
                "content_tolerance": content_tolerance,
            },
        )
        if feature_key == FEATURE_IMAGE_RECOGNITION and self.image_model is not None:
            return self.image_model
        return self.chat_model

    async def resolve_model_id(
        self,
        feature_key: str | None = None,
        *,
        character=None,
        content_tolerance: str | None = None,
    ) -> str:
        self.resolve_model_id_calls.append(
            {
                "feature_key": feature_key,
                "character_id": getattr(character, "id", None),
                "content_tolerance": content_tolerance,
            },
        )
        if feature_key == FEATURE_IMAGE_RECOGNITION and self.image_model is not None:
            return self.image_model_id
        return self.chat_model_id

    async def is_fake(self, feature_key: str | None = None) -> bool:
        _ = feature_key
        return False


def _build(
    *,
    supports_vision: bool,
    public_base_url: str = "",
    uploads_dir: Path | None = None,
    object_storage: InMemoryObjectStorage | None = None,
    active_llm_provider: _RoutingActiveProvider | None = None,
    model: _CapturingModel | None = None,
    turn_recorder=None,
) -> tuple[ChatService, CharacterService, _CapturingModel]:
    chars = InMemoryCharacterRepository()
    convos = InMemoryConversationRepository()
    mems = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    model = model or _CapturingModel(supports_vision=supports_vision)
    registry.register(model)
    chat = ChatService(
        character_repository=chars,
        conversation_repository=convos,
        memory_repository=mems,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        active_llm_provider=active_llm_provider,
        public_base_url=public_base_url,
        uploads_dir=uploads_dir,
        object_storage=object_storage,
        turn_recorder=turn_recorder,
    )
    return chat, CharacterService(chars), model


@pytest.mark.asyncio
async def test_absolute_url_pointing_at_own_uploads_routes_through_storage_base64(
    tmp_path,
) -> None:
    """Regression: when the conversation history holds an absolute URL
    that points at our own ``public_base_url + /uploads/...`` mount
    (older messaging dispatcher persisted them this way), the vision
    converter must still reverse the URL through Object Storage and
    read the object as base64.

    Otherwise the URL passes through as raw HTTPS and LM Studio
    rejects the request with ``'url' field must be a base64 encoded
    image``, breaking cross-turn vision carry-over.
    """
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    storage = InMemoryObjectStorage(
        public_base_url="https://kokoro.example.com/uploads",
    )
    await storage.put_bytes(
        object_key="characters/char-1/tools/selfie.png",
        content=png_bytes,
        content_type="image/png",
    )

    from kokoro_link.application.services.chat_service import (
        _to_vision_url_with_storage,
    )

    absolute = (
        "https://kokoro.example.com/uploads/characters/char-1/tools/selfie.png"
    )
    out = await _to_vision_url_with_storage(
        absolute,
        uploads_dir=None,
        public_base_url="https://kokoro.example.com",
        object_storage=storage,
    )
    # Must be base64 data URL, NOT pass-through HTTP.
    assert out is not None
    assert out.startswith("data:image/png;base64,")


def test_external_http_url_still_passes_through() -> None:
    """A URL we don't own (Telegram CDN, third party) can't be base64-
    encoded without downloading. Keep pass-through so models that
    accept HTTP (Anthropic, OpenAI cloud) still see the image."""
    from kokoro_link.application.services.chat_service import _to_vision_url
    out = _to_vision_url(
        "https://t.me/external/cdn/abc.jpg",
        uploads_dir=None, public_base_url="https://kokoro.example.com",
    )
    assert out == "https://t.me/external/cdn/abc.jpg"


def test_existing_data_url_passes_through_unchanged() -> None:
    """If the caller already produced a data: URL, don't re-process."""
    from kokoro_link.application.services.chat_service import _to_vision_url
    inline = "data:image/jpeg;base64,ZZZZ"
    out = _to_vision_url(inline, uploads_dir=None, public_base_url="")
    assert out == inline


@pytest.mark.asyncio
async def test_recent_image_carries_into_next_turn_with_marker(tmp_path) -> None:
    """Regression: when the user posted an image last turn and a text
    follow-up this turn, the model should still see that earlier image
    plus a ``[圖 N]`` marker in the history line so it can reason about
    "剛才那張". Capped at ``_VISION_HISTORY_LIMIT`` images total.
    """
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    await storage.put_bytes(
        object_key="chat-uploads/first.png",
        content=png_bytes,
        content_type="image/png",
    )

    chat, chars, model = _build(
        supports_vision=True, object_storage=storage,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    # Turn 1 — user posts an image.
    reply1 = await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="看看這張照片",
        attachment_urls=["/uploads/chat-uploads/first.png"],
    ))

    # Turn 2 — text only follow-up in the SAME conversation.
    await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        conversation_id=reply1.conversation_id,
        message="你覺得如何？",
    ))

    # Model should still see the earlier image (carried forward).
    assert len(model.last_image_urls or ()) == 1
    # History line for turn 1's user message carries ``[圖 1]`` so the
    # model can pair "剛才那張" with image_urls[0].
    prompt = model.last_prompt or ""
    assert "[圖 1]" in prompt
    assert "使用者：[圖 1] 看看這張照片" in prompt
    # Legend line documents what the markers mean.
    assert "圖片標記" in prompt


@pytest.mark.asyncio
async def test_vision_history_caps_at_two_and_drops_oldest(tmp_path) -> None:
    """Three images across three turns → only the newest two survive."""
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    for name in ("a.png", "b.png", "c.png"):
        await storage.put_bytes(
            object_key=f"chat-uploads/{name}",
            content=png_bytes,
            content_type="image/png",
        )

    chat, chars, model = _build(
        supports_vision=True, object_storage=storage,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    # Three turns, each with one image attached.
    reply = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, message="第一張",
        attachment_urls=["/uploads/chat-uploads/a.png"],
    ))
    reply = await chat.send_message(SendChatMessageRequest(
        character_id=created.id, conversation_id=reply.conversation_id,
        message="第二張",
        attachment_urls=["/uploads/chat-uploads/b.png"],
    ))
    await chat.send_message(SendChatMessageRequest(
        character_id=created.id, conversation_id=reply.conversation_id,
        message="第三張",
        attachment_urls=["/uploads/chat-uploads/c.png"],
    ))

    # Oldest (a.png) was dropped; newest two (b + c) survive.
    assert len(model.last_image_urls or ()) == 2
    prompt = model.last_prompt or ""
    # Turn 2 carries [圖 1], turn 3 (the current / latest user) carries [圖 2].
    assert "[圖 1] 第二張" in prompt
    # Current user line uses the ``最新使用者訊息：`` marker.
    assert "最新使用者訊息：[圖 2] 第三張" in prompt
    # First turn's message line should NOT have any marker — its image
    # was evicted.
    assert "[圖 1] 第一張" not in prompt


@pytest.mark.asyncio
async def test_vision_model_gets_base64_data_url_for_storage_uploads(tmp_path) -> None:
    """Stored object → encoded as ``data:`` URL (not HTTP). This
    is the robust path: avoids depending on the model server being
    able to reach our HTTP mount."""
    # Single-pixel PNG header so mimetypes guesses correctly.
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    await storage.put_bytes(
        object_key="chat-uploads/abc.png",
        content=png_bytes,
        content_type="image/png",
    )

    chat, chars, model = _build(
        supports_vision=True, object_storage=storage,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="看看這張",
        attachment_urls=["/uploads/chat-uploads/abc.png"],
    ))

    assert len(model.last_image_urls or ()) == 1
    url = (model.last_image_urls or ("",))[0]
    assert url.startswith("data:image/png;base64,")
    # Prompt unchanged — vision path doesn't need the text placeholder.
    assert "模型不支援視覺" not in (model.last_prompt or "")


@pytest.mark.asyncio
async def test_vision_falls_back_to_absolute_url_when_file_missing(tmp_path) -> None:
    """File not on disk but ``public_base_url`` set → HTTP URL as
    last resort (works when operator has a real public mount)."""
    chat, chars, model = _build(
        supports_vision=True,
        public_base_url="https://kokoro.example.com",
        uploads_dir=tmp_path,  # empty dir, no file
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="看看這張",
        attachment_urls=["/uploads/chat-uploads/abc.png"],
    ))

    assert model.last_image_urls == (
        "https://kokoro.example.com/uploads/chat-uploads/abc.png",
    )


@pytest.mark.asyncio
async def test_non_vision_chat_model_uses_image_recognition_route() -> None:
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    await storage.put_bytes(
        object_key="chat-uploads/cat.png",
        content=png_bytes,
        content_type="image/png",
    )
    main_model = _CapturingModel(
        provider_id="text",
        supports_vision=False,
        reply="主模型回覆",
    )
    recognition_model = _CapturingModel(
        provider_id="vision",
        supports_vision=True,
        reply="[圖 1] 畫面中有一隻黑貓坐在窗邊，背景有白色窗簾。",
    )
    active_provider = _RoutingActiveProvider(
        chat_model=main_model,
        chat_model_id="text-main",
        image_model=recognition_model,
        image_model_id="vision-caption",
    )
    chat, chars, model = _build(
        supports_vision=False,
        object_storage=storage,
        active_llm_provider=active_provider,
        model=main_model,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="看看這張",
        attachment_urls=["/uploads/chat-uploads/cat.png"],
    ))

    assert model is main_model
    assert main_model.last_model == "text-main"
    assert main_model.last_image_urls == ()
    main_prompt = main_model.last_prompt or ""
    assert "圖片識別摘要" in main_prompt
    assert "黑貓坐在窗邊" in main_prompt
    assert "目前模型不支援視覺" not in main_prompt
    # Placement regression (turn record 9b094fad): the summary must sit
    # in the prompt body before the latest user message + instruction
    # footer — never appended as the prompt tail, where its analyst
    # register reads as part of the user's message.
    assert main_prompt.index("圖片識別摘要") < main_prompt.index(
        "最新使用者訊息：",
    )
    assert not main_prompt.rstrip().endswith("[/圖片識別摘要]")
    # Recognition succeeded → no drop placeholder on top of the summary.
    assert "另外附帶了" not in main_prompt

    assert recognition_model.last_model == "vision-caption"
    assert len(recognition_model.last_image_urls or ()) == 1
    assert (recognition_model.last_image_urls or ("",))[0].startswith(
        "data:image/png;base64,",
    )
    recognition_prompt = recognition_model.last_prompt or ""
    assert "逐張描述可見內容" in recognition_prompt
    # OCR-hedge regression: illegible text must be skipped silently, not
    # declared as "無法辨識" — those declarations leaked into replies as
    # "your message is hard to read".
    assert "直接略過" in recognition_prompt
    assert "要明說" not in recognition_prompt
    assert [call["feature_key"] for call in active_provider.resolve_calls] == [
        FEATURE_CHAT,
        FEATURE_IMAGE_RECOGNITION,
    ]


@pytest.mark.asyncio
async def test_non_vision_chat_model_keeps_placeholder_without_vision_recognizer() -> None:
    main_model = _CapturingModel(
        provider_id="text",
        supports_vision=False,
        reply="主模型回覆",
    )
    recognition_model = _CapturingModel(
        provider_id="text-recognizer",
        supports_vision=False,
        reply="不應該被呼叫",
    )
    active_provider = _RoutingActiveProvider(
        chat_model=main_model,
        image_model=recognition_model,
    )
    chat, chars, _model = _build(
        supports_vision=False,
        active_llm_provider=active_provider,
        model=main_model,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="看看這張",
        attachment_urls=["/uploads/chat-uploads/missing.png"],
    ))

    assert recognition_model.calls == []
    prompt = main_model.last_prompt or ""
    assert "圖片識別摘要" not in prompt
    assert "目前模型不支援視覺" in prompt


@pytest.mark.asyncio
async def test_streaming_non_vision_chat_model_uses_image_recognition_route() -> None:
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    await storage.put_bytes(
        object_key="chat-uploads/room.png",
        content=png_bytes,
        content_type="image/png",
    )
    main_model = _CapturingModel(
        provider_id="text",
        supports_vision=False,
        reply="串流主模型回覆",
    )
    recognition_model = _CapturingModel(
        provider_id="vision",
        supports_vision=True,
        reply="[圖 1] 房間裡有一張木桌，桌上放著打開的筆記本。",
    )
    active_provider = _RoutingActiveProvider(
        chat_model=main_model,
        image_model=recognition_model,
    )
    chat, chars, _model = _build(
        supports_vision=False,
        object_storage=storage,
        active_llm_provider=active_provider,
        model=main_model,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    token_stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(
            character_id=created.id,
            message="這張給你看",
            attachment_urls=["/uploads/chat-uploads/room.png"],
        ),
    )
    chunks = [chunk async for chunk in token_stream]
    await finalizer.finish("".join(chunks))

    assert chunks == ["串流主模型回覆"]
    assert main_model.last_image_urls == ()
    stream_prompt = main_model.last_prompt or ""
    assert "圖片識別摘要" in stream_prompt
    assert "木桌" in stream_prompt
    # Same placement pin as the non-stream path.
    assert stream_prompt.index("圖片識別摘要") < stream_prompt.index(
        "最新使用者訊息：",
    )
    assert not stream_prompt.rstrip().endswith("[/圖片識別摘要]")
    assert len(recognition_model.last_image_urls or ()) == 1


@pytest.mark.asyncio
async def test_non_vision_model_drops_urls_and_appends_placeholder() -> None:
    chat, chars, model = _build(supports_vision=False)
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="看看這張",
        attachment_urls=["/uploads/chat-uploads/abc.png"],
    ))

    # URLs stripped from the model call.
    assert model.last_image_urls == ()
    # Prompt got the fallback placeholder so the LLM knows images came in.
    assert "另外附帶了 1 張圖片" in (model.last_prompt or "")


@pytest.mark.asyncio
async def test_vision_without_base_url_downgrades() -> None:
    """If the model claims vision but we have no way to resolve URLs
    to absolute, fall back to the placeholder — a remote model can't
    fetch ``/uploads/...`` server-relative."""
    chat, chars, model = _build(supports_vision=True, public_base_url="")
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="看看這張",
        attachment_urls=["/uploads/chat-uploads/abc.png"],
    ))

    assert model.last_image_urls == ()
    assert "另外附帶了" in (model.last_prompt or "")


@pytest.mark.asyncio
async def test_user_message_persists_with_attachments() -> None:
    chat, chars, model = _build(supports_vision=False)
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    response = await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="看這張",
        attachment_urls=["/uploads/chat-uploads/abc.png"],
    ))

    assert len(response.user_message.attachments) == 1
    assert response.user_message.attachments[0].url == "/uploads/chat-uploads/abc.png"
    assert response.user_message.attachments[0].kind == "image"


# ---- image-input rejection → degrade (retry once without images) -----


class _RejectImageOnceModel(ChatModelPort):
    """Vision-capable stub whose first call rejects the image parts.

    Simulates a mis-set ``supports_vision`` flag pointed at a text-only
    upstream: the first attempt (images attached) raises
    ``ImageInputRejectedError``; the degrade retry (no images, prompt
    carrying the drop placeholder) succeeds. Records every call so tests
    can assert the retry dropped the images and appended the placeholder.
    ``generate`` and ``generate_stream`` track their reject-once state
    independently so one stub serves both the buffered and streaming
    tests.
    """

    def __init__(self, *, provider_id: str = "fake", reply: str = "降級後仍然回覆") -> None:
        self.provider_id = provider_id
        self.supports_vision = True
        self.reply = reply
        self.generate_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.generate_calls.append(
            {"prompt": prompt, "image_urls": tuple(image_urls), "model": model},
        )
        if len(self.generate_calls) == 1:
            raise ImageInputRejectedError(
                status_code=404,
                body="No endpoints found that support image input",
            )
        return self.reply

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        self.stream_calls.append(
            {"prompt": prompt, "image_urls": tuple(image_urls), "model": model},
        )
        if len(self.stream_calls) == 1:
            raise ImageInputRejectedError(
                status_code=404,
                body="No endpoints found that support image input",
            )
        yield self.reply

    async def list_models(self) -> list[str]:
        return [self.provider_id]


def _stored_png_storage_sync() -> InMemoryObjectStorage:
    return InMemoryObjectStorage(public_base_url="/uploads")


_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
)


@pytest.mark.asyncio
async def test_image_rejection_degrades_and_retries_without_images(tmp_path) -> None:
    """A vision-capable model whose upstream rejects the image parts must
    degrade the turn: retry once WITHOUT images (prompt carrying the drop
    placeholder) and return a normal reply — never a 500."""
    storage = _stored_png_storage_sync()
    await storage.put_bytes(
        object_key="chat-uploads/x.png",
        content=_PNG_BYTES,
        content_type="image/png",
    )
    reject_model = _RejectImageOnceModel(reply="降級後仍然回覆")
    chat, chars, _model = _build(
        supports_vision=True,
        object_storage=storage,
        model=reject_model,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    reply = await chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="看看這張",
        attachment_urls=["/uploads/chat-uploads/x.png"],
    ))

    # Turn survived with a normal assistant reply.
    assert reply.assistant_message is not None
    assert "降級後仍然回覆" in reply.assistant_message.content
    # Exactly two model calls: first WITH the image, retry WITHOUT.
    assert len(reject_model.generate_calls) == 2
    first, second = reject_model.generate_calls
    assert len(first["image_urls"]) == 1
    assert first["image_urls"][0].startswith("data:image/png;base64,")
    assert second["image_urls"] == ()
    # Retry prompt carries the drop placeholder so the model knows an
    # image came in that it can't see.
    assert "另外附帶了 1 張圖片" in second["prompt"]


@pytest.mark.asyncio
async def test_streaming_image_rejection_degrades_and_retries(tmp_path) -> None:
    """Same degrade on the true token-streaming path: the 4xx surfaces
    before the first token, so the retry (no images) must happen before
    any chunk reaches the SSE consumer. The recorded turn trace must
    carry the prompt that was ACTUALLY sent (with the drop placeholder),
    not the original pre-degrade prompt."""
    storage = _stored_png_storage_sync()
    await storage.put_bytes(
        object_key="chat-uploads/y.png",
        content=_PNG_BYTES,
        content_type="image/png",
    )
    reject_model = _RejectImageOnceModel(reply="串流降級回覆")
    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    chat, chars, _model = _build(
        supports_vision=True,
        object_storage=storage,
        model=reject_model,
        turn_recorder=turn_recorder,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yuki"))

    token_stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(
            character_id=created.id,
            message="這張給你看",
            attachment_urls=["/uploads/chat-uploads/y.png"],
        ),
    )
    chunks = [chunk async for chunk in token_stream]
    await finalizer.finish("".join(chunks))
    await turn_recorder.flush()

    # Consumer saw only the degraded reply — no leaked partial + no crash.
    assert chunks == ["串流降級回覆"]
    assert len(reject_model.stream_calls) == 2
    first, second = reject_model.stream_calls
    assert len(first["image_urls"]) == 1
    assert second["image_urls"] == ()
    assert "另外附帶了 1 張圖片" in second["prompt"]
    # The persisted turn record's prompt matches what was actually sent
    # on the degrade retry — original prompt + drop placeholder.
    records = await turn_records.list_recent(character_id=created.id)
    chat_record = next(r for r in records if r.kind == "chat")
    assert "另外附帶了 1 張圖片" in chat_record.prompt_assembled


class _ExplodingStreamModel:
    """Stub exposing ``generate_stream_capturing`` directly (so
    ``_stream_capturing`` uses it instead of wrapping) whose stream
    raises a plain ``RuntimeError`` on the first chunk. Records every
    ``__aexit__`` on the stream context so tests can assert the context
    is closed even when the eager first-chunk pull blows up with a
    non-image error."""

    provider_id = "fake"
    supports_vision = True

    def __init__(self) -> None:
        self.aexit_calls: list[tuple[Any, Any]] = []

    def generate_stream_capturing(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> Any:
        stub = self

        class _Capture:
            metadata = None

            async def chunks(self) -> AsyncIterator[str]:
                raise RuntimeError("upstream boom")
                yield  # pragma: no cover — marks this as an async generator

        class _Ctx:
            async def __aenter__(self) -> _Capture:
                return _Capture()

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
                stub.aexit_calls.append((exc_type, exc))
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_stream_generic_first_chunk_error_closes_ctx_and_propagates() -> None:
    """A non-image error on the eager first-chunk pull must NOT leak the
    stream context: ``__aexit__`` runs, then the error propagates
    unchanged (no reclassification, no degrade retry)."""
    from kokoro_link.application.services.chat_service import _stream_capturing

    model = _ExplodingStreamModel()
    with pytest.raises(RuntimeError, match="upstream boom"):
        await _stream_capturing(
            model,
            "看看這張",
            image_urls=("data:image/png;base64,xxxx",),
        )

    # Context was exited exactly once, with the propagating error.
    assert len(model.aexit_calls) == 1
    exc_type, exc = model.aexit_calls[0]
    assert exc_type is RuntimeError
    assert isinstance(exc, RuntimeError)


# ---- content-driven image-recognition routing (real active provider) --
#
# Regression for the recognition-route hijack. When the main chat model is
# text-only on a NON-frontier provider, the turn's provider-derived
# ``content_tolerance`` is "community". Before the fix that community value
# was threaded into the image-recognition resolve, and the resolver's
# community-forcing branch rerouted recognition onto the admin NSFW
# community target instead of the ``multimodal_perception`` group route —
# even though the input is ordinary user-uploaded images in normal mode.
#
# These wire a REAL ``PreferenceBackedActiveLLMProvider`` (the fake routing
# stub above can't reproduce the hijack because it ignores tolerance) so the
# resolver's real precedence — layer-0 NSFW overlay → community-forcing →
# feature/group routing — actually runs.


class _RealRoutingHarness:
    def __init__(
        self,
        *,
        chat: ChatService,
        chars: CharacterService,
        text_main: _CapturingModel,
        group_model: _CapturingModel,
        nsfw_model: _CapturingModel,
        nsfw: NsfwModeService,
        prefs: InMemoryPreferencesRepository,
    ) -> None:
        self.chat = chat
        self.chars = chars
        self.text_main = text_main
        self.group_model = group_model
        self.nsfw_model = nsfw_model
        self.nsfw = nsfw
        self.prefs = prefs


async def _build_real_routing(
    *, main_supports_vision: bool = False,
) -> _RealRoutingHarness:
    """Wire ChatService with a real active provider whose global routing is:

    * ``active_model`` → text-only main chat model on a non-frontier
      provider id (so the turn tolerance derives to community).
    * ``feature_model_groups.multimodal_perception`` → a vision-capable
      recognition model (the CORRECT recognition route).
    * a configured global NSFW target → a DIFFERENT vision-capable model
      (the WRONG route that the hijack lands on).
    """
    chars_repo = InMemoryCharacterRepository()
    convos = InMemoryConversationRepository()
    mems = InMemoryMemoryRepository()

    text_main = _CapturingModel(
        provider_id="custom_openai_compatible",
        supports_vision=main_supports_vision,
        reply="主模型回覆",
    )
    group_model = _CapturingModel(
        provider_id="vision_group",
        supports_vision=True,
        reply="[圖 1] 這是多模態群組路由看到的畫面。",
    )
    nsfw_model = _CapturingModel(
        provider_id="nsfw_target",
        supports_vision=True,
        reply="[圖 1] 這是 NSFW 社群目標看到的畫面。",
    )
    registry = InMemoryChatModelRegistry(
        default_provider_id="custom_openai_compatible",
    )
    registry.register(text_main)
    registry.register(group_model)
    registry.register(nsfw_model)

    prefs = InMemoryPreferencesRepository()
    await prefs.set(
        "active_model",
        {"provider_id": "custom_openai_compatible", "model_id": "text-main"},
    )
    await prefs.set(
        "feature_model_groups",
        {
            FEATURE_GROUP_MULTIMODAL_PERCEPTION: {
                "provider_id": "vision_group",
                "model_id": "vision-caption",
            },
        },
    )

    nsfw = NsfwModeService(preferences=prefs, ttl_seconds=60)
    await nsfw.set_global_target(
        llm_provider_id="nsfw_target",
        llm_model_id="nsfw-model",
        image_profile_id="anime_nsfw",
    )

    provider = PreferenceBackedActiveLLMProvider(
        registry=registry,
        preferences=prefs,
        default_provider_id="custom_openai_compatible",
        nsfw_mode_service=nsfw,
    )

    storage = InMemoryObjectStorage(public_base_url="/uploads")
    await storage.put_bytes(
        object_key="chat-uploads/cat.png",
        content=_PNG_BYTES,
        content_type="image/png",
    )

    chat = ChatService(
        character_repository=chars_repo,
        conversation_repository=convos,
        memory_repository=mems,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        active_llm_provider=provider,
        nsfw_mode_service=nsfw,
        object_storage=storage,
    )
    return _RealRoutingHarness(
        chat=chat,
        chars=CharacterService(chars_repo),
        text_main=text_main,
        group_model=group_model,
        nsfw_model=nsfw_model,
        nsfw=nsfw,
        prefs=prefs,
    )


@pytest.mark.asyncio
async def test_non_frontier_main_recognition_uses_group_route_not_nsfw_target() -> None:
    """Non-stream path: a text-only, non-frontier main model must route
    user-image recognition through the multimodal_perception group, NOT
    the admin NSFW community target, in normal (non-NSFW) mode."""
    h = await _build_real_routing()
    created = await h.chars.create_character(CreateCharacterRequest(name="Yuki"))

    await h.chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="看看這張",
        attachment_urls=["/uploads/chat-uploads/cat.png"],
    ))

    # Recognition landed on the multimodal group model with its model id...
    assert len(h.group_model.calls) == 1
    assert h.group_model.last_model == "vision-caption"
    # ...and NOT on the NSFW community target (the hijack destination).
    assert h.nsfw_model.calls == []
    # Main text-only model saw the group caption, no raw images.
    assert h.text_main.last_image_urls == ()
    main_prompt = h.text_main.last_prompt or ""
    assert "多模態群組路由" in main_prompt
    assert "NSFW 社群目標" not in main_prompt


@pytest.mark.asyncio
async def test_streaming_non_frontier_main_recognition_uses_group_route() -> None:
    """Streaming no-tool path (the other call site): same content-driven
    routing so a non-frontier main model doesn't hijack recognition."""
    h = await _build_real_routing()
    created = await h.chars.create_character(CreateCharacterRequest(name="Yuki"))

    token_stream, finalizer = await h.chat.send_message_stream(
        SendChatMessageRequest(
            character_id=created.id,
            message="這張給你看",
            attachment_urls=["/uploads/chat-uploads/cat.png"],
        ),
    )
    chunks = [chunk async for chunk in token_stream]
    await finalizer.finish("".join(chunks))

    assert chunks == ["主模型回覆"]
    assert len(h.group_model.calls) == 1
    assert h.group_model.last_model == "vision-caption"
    assert h.nsfw_model.calls == []
    assert "多模態群組路由" in (h.text_main.last_prompt or "")


@pytest.mark.asyncio
async def test_nsfw_active_recognition_still_routes_to_nsfw_target() -> None:
    """Complementary layer-0 overlay: with NSFW mode ACTIVE the recognition
    resolve must land on the configured NSFW target regardless of the
    routing tolerance, and must NOT fall through to the multimodal group.

    Driven directly against ``_build_image_recognition_context`` with the
    exact tolerance ChatService threads in NSFW mode
    (``_content_tolerance_for_content_mode(NSFW)`` == community): under a
    single global NSFW target the main chat model would itself reroute to
    that (vision-capable) target and short-circuit recognition, so the
    method-level call isolates the recognition route."""
    h = await _build_real_routing()
    await h.nsfw.enable(user_id=DEFAULT_OPERATOR_ID)

    character = Character.create(
        name="Yuki", summary="",
        personality=[], interests=[], speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    text_only_main = _CapturingModel(
        provider_id="custom_openai_compatible",
        supports_vision=False,
    )

    context = await h.chat._build_image_recognition_context(  # noqa: SLF001
        character=character,
        main_model=text_only_main,
        attachment_urls=("/uploads/chat-uploads/cat.png",),
        content_tolerance=_content_tolerance_for_content_mode(
            MessageContentMode.NSFW,
        ),
    )

    # Recognition resolved to the NSFW target model...
    assert len(h.nsfw_model.calls) == 1
    assert h.nsfw_model.last_model == "nsfw-model"
    # ...and the multimodal group route was bypassed.
    assert h.group_model.calls == []
    assert "NSFW 社群目標" in context


# ---- route-entry supports_vision override drives the main chat model --
#
# Fix 4 end-to-end: a routing entry's ``supports_vision`` pin overrides
# the connection-level flag for calls resolved through it, so an
# aggregator connection can attach images on a vision route and drop them
# on a text-only route. The main chat model is resolved with NO content
# tolerance, so the override is NOT skipped by the community-forcing
# branch (that only guards the recognition sub-resolve).


@pytest.mark.asyncio
async def test_chat_feature_vision_true_forces_images_onto_text_main() -> None:
    """Connection-level supports_vision=False + a ``chat`` feature entry
    pinning supports_vision=true → the main model is bound vision-capable
    and receives the image directly (no recognition detour)."""
    h = await _build_real_routing(main_supports_vision=False)
    await h.prefs.set(
        "feature_models",
        {
            FEATURE_CHAT: {
                "provider_id": None,
                "model_id": None,
                "supports_vision": True,
            },
        },
    )
    created = await h.chars.create_character(CreateCharacterRequest(name="Yuki"))

    await h.chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="看看這張",
        attachment_urls=["/uploads/chat-uploads/cat.png"],
    ))

    # Exactly one main-model call carried the image (the generation), as a
    # base64 data URL — the connection flag was overridden to vision.
    image_calls = [c for c in h.text_main.calls if c["image_urls"]]
    assert len(image_calls) == 1
    assert image_calls[0]["image_urls"][0].startswith("data:image/png;base64,")
    # Main model saw the image itself → recognition group route unused.
    assert h.group_model.calls == []


@pytest.mark.asyncio
async def test_chat_feature_vision_false_drops_images_and_recognizes() -> None:
    """Inverse: connection-level supports_vision=True + a ``chat`` feature
    entry pinning supports_vision=false → images are dropped from the main
    model and the recognition group route produces a text summary."""
    h = await _build_real_routing(main_supports_vision=True)
    await h.prefs.set(
        "feature_models",
        {
            FEATURE_CHAT: {
                "provider_id": None,
                "model_id": None,
                "supports_vision": False,
            },
        },
    )
    created = await h.chars.create_character(CreateCharacterRequest(name="Yuki"))

    await h.chat.send_message(SendChatMessageRequest(
        character_id=created.id,
        message="看看這張",
        attachment_urls=["/uploads/chat-uploads/cat.png"],
    ))

    # Main model was flipped to text-only → no raw images on any of its
    # calls.
    assert all(c["image_urls"] == () for c in h.text_main.calls)
    # Recognition kicked in through the multimodal group model...
    assert len(h.group_model.calls) == 1
    # ...and its caption reached the main model's generation prompt.
    assert any(
        "圖片識別摘要" in c["prompt"] and "多模態群組路由" in c["prompt"]
        for c in h.text_main.calls
    )
