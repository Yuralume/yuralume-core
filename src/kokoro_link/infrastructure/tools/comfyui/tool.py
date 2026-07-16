"""ComfyUI image-generation tool.

Thin adapter over ``ImageProviderPort`` that (a) enforces the
``ToolPort`` contract for the chat-loop tool cycle, (b) writes the
returned bytes to Object Storage under
``characters/{id}/tools/``, (c) wraps the URL in a ``ToolAttachment``
so the message bubble / messaging adapters can deliver it without
knowing anything about ComfyUI, and (d) records the generated file in
the character's album so the operator can
browse / re-promote / delete later.

The actual generation logic lives in ``generator.py`` and is shared
with ``CharacterImageService.generate_portrait`` — tweaking the prompt
recipe or aspect mapping in one place updates both surfaces.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from kokoro_link.application.services.album_service import AlbumService
from kokoro_link.application.services.feature_keys import (
    FEATURE_IMAGE_CHAT_TOOL,
)
from kokoro_link.application.services.image_usage import image_usage_parts_from_provider
from kokoro_link.application.services.visual_generation_style import (
    VisualGenerationStyleService,
)
from kokoro_link.contracts.active_image import ActiveImageProviderPort
from kokoro_link.contracts.generation_usage import (
    UsageEventDraft,
    UsageEventRecorderPort,
)
from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
    ImageTimeoutError,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.tool import ToolContext, ToolPort
from kokoro_link.domain.entities.generation_usage import (
    CAPABILITY_IMAGE,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
)
from kokoro_link.domain.value_objects.tool_call import (
    ToolAttachment,
    ToolResult,
)

_LOGGER = logging.getLogger(__name__)
_CHAT_TOOL_BATCH_SIZE = 1
_USAGE_FEATURE_CHAT_IMAGE_TOOL = "chat_image_tool"


class ComfyImageTool(ToolPort):
    name: str = "generate_image"
    description: str = (
        "把『你此刻的樣子／正在做的動作／所在的場景』直接畫出來傳給使用者，"
        "取代純文字描述。只要當下有明確場景＋動作／姿態／表情就該主動呼叫，"
        "不用等使用者開口要圖。positive 用 danbooru 風格 tag，"
        "同時描寫角色主體外觀與場景氛圍；如果角色是非人類動物，"
        "必須用動物本體 tag，不要寫 1girl/1boy/人臉人身；"
        "caption 寫一句自然的角色台詞配圖。"
    )
    parameters_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "positive": {
                "type": "string",
                "description": "danbooru 風格 tag，描述想拍出的樣子／情境",
            },
            "aspect": {
                "type": "string",
                "enum": ["portrait", "landscape", "square"],
                "description": "畫面比例；一般角色預設 portrait",
            },
            "caption": {
                "type": "string",
                "description": "一句給使用者看的中文說明，會跟著圖片送出",
            },
        },
        "required": ["positive"],
    }

    def __init__(
        self,
        *,
        image_provider: ActiveImageProviderPort,
        uploads_dir: Path,
        url_prefix: str = "/uploads",
        album_service: AlbumService | None = None,
        object_storage: ObjectStoragePort | None = None,
        visual_style_service: VisualGenerationStyleService | None = None,
        usage_recorder: UsageEventRecorderPort | None = None,
    ) -> None:
        self._image_provider = image_provider
        _ = uploads_dir, url_prefix
        self._object_storage = object_storage
        # Optional: container wires this in so every tool generation
        # ends up browsable in the album UI. Left optional so unit tests
        # of the tool can ignore album behaviour and focus on generation.
        self._album_service = album_service
        self._visual_style_service = visual_style_service
        self._usage_recorder = usage_recorder

    def set_usage_recorder(self, recorder: UsageEventRecorderPort | None) -> None:
        self._usage_recorder = recorder

    async def invoke(self, ctx: ToolContext) -> ToolResult:
        args = dict(ctx.arguments)
        positive = str(args.get("positive") or "")
        aspect = str(args.get("aspect") or "portrait")

        provider = await self._image_provider.resolve(
            FEATURE_IMAGE_CHAT_TOOL, character=ctx.character,
        )
        profile_id = await self._image_provider.resolve_profile_id(
            FEATURE_IMAGE_CHAT_TOOL, character=ctx.character,
        )
        if self._object_storage is None:
            return ToolResult.failure("Object storage is not configured")
        if provider is None:
            return ToolResult.failure(
                "目前沒有可用的生圖通道（image profile 未配置）",
            )
        started_at = datetime.now(timezone.utc)
        try:
            styled_positive = await self._styled_prompt(
                positive, character=ctx.character,
            )
            images = await provider.generate(
                character=ctx.character,
                positive=styled_positive,
                aspect=aspect,
                batch=_CHAT_TOOL_BATCH_SIZE,
                recent_dialogue=ctx.recent_dialogue,
                user_attachment_urls=ctx.user_attachment_urls,
            )
        except ImageTimeoutError as exc:
            await self._record_usage_safely(
                ctx=ctx,
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                returned=0,
                artifact_count=0,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            return ToolResult.failure(str(exc))
        except ImageNoOutputError as exc:
            await self._record_usage_safely(
                ctx=ctx,
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                returned=0,
                artifact_count=0,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            return ToolResult.failure(str(exc))
        except ImageGenerationError as exc:
            await self._record_usage_safely(
                ctx=ctx,
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                returned=0,
                artifact_count=0,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            return ToolResult.failure(str(exc))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("ComfyImageTool generation crashed")
            await self._record_usage_safely(
                ctx=ctx,
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                returned=0,
                artifact_count=0,
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
            )
            return ToolResult.failure(f"產圖失敗：{exc}")

        returned_images = list(images)
        images_to_deliver = returned_images[:_CHAT_TOOL_BATCH_SIZE]
        attachments: list[ToolAttachment] = []
        caption = str(args.get("caption") or "").strip() or None
        try:
            for data in images_to_deliver:
                filename = f"{uuid4().hex}.png"
                stored = await self._object_storage.put_bytes(
                    object_key=(
                        f"characters/{ctx.character.id}/tools/{filename}"
                    ),
                    content=data,
                    content_type="image/png",
                    metadata={
                        "character_id": ctx.character.id,
                        "kind": "chat-tool",
                    },
                )
                url = stored.url
                attachments.append(
                    ToolAttachment(
                        kind="image", url=url, mime_type="image/png",
                        caption=caption,
                    ),
                )
                # Best-effort album capture: if the album service is wired,
                # record this generation so the operator can find it later
                # from the album tab. We intentionally don't await inside a
                # try/except-free block — a DB blip should not fail the
                # chat-facing tool call after bytes are already on disk.
                if self._album_service is not None:
                    try:
                        await self._album_service.add_auto(
                            character_id=ctx.character.id,
                            url=url,
                            caption=caption,
                            byte_size=len(data),
                        )
                    except Exception:  # noqa: BLE001 — album failure must not poison tool result
                        _LOGGER.exception(
                            "album.add_auto failed for character=%s url=%s",
                            ctx.character.id, url,
                        )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("ComfyImageTool object storage write failed")
            await self._record_usage_safely(
                ctx=ctx,
                provider=provider,
                profile_id=profile_id or "",
                aspect=aspect,
                returned=len(returned_images),
                artifact_count=len(attachments),
                status=STATUS_FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
                started_at=started_at,
                billable_quantity=len(returned_images),
            )
            return ToolResult.failure(f"產圖存檔失敗：{exc}")

        await self._record_usage_safely(
            ctx=ctx,
            provider=provider,
            profile_id=profile_id or "",
            aspect=aspect,
            returned=len(returned_images),
            artifact_count=len(attachments),
            status=STATUS_SUCCEEDED,
            started_at=started_at,
        )
        output_text = (
            caption
            or f"已產生 {len(attachments)} 張圖片"
        )
        return ToolResult.success(
            output_text=output_text, attachments=attachments,
        )

    async def _styled_prompt(self, positive: str, *, character) -> str:
        if self._visual_style_service is None:
            return positive
        return await self._visual_style_service.styled_prompt(
            positive, character=character,
        )

    async def _record_usage_safely(
        self,
        *,
        ctx: ToolContext,
        provider: object,
        profile_id: str,
        aspect: str,
        returned: int,
        artifact_count: int,
        status: str,
        started_at: datetime,
        error_code: str | None = None,
        error_message: str | None = None,
        billable_quantity: int | None = None,
    ) -> None:
        if self._usage_recorder is None:
            return
        completed_at = datetime.now(timezone.utc)
        usage_parts = image_usage_parts_from_provider(
            provider=provider,
            requested=_CHAT_TOOL_BATCH_SIZE,
            returned=returned,
            status=status,
            billable_quantity=billable_quantity,
            base_metadata={
                "aspect": aspect,
                "batch": _CHAT_TOOL_BATCH_SIZE,
                "recent_dialogue": bool(ctx.recent_dialogue),
                "user_attachment_count": len(ctx.user_attachment_urls),
            },
        )
        try:
            await self._usage_recorder.record(UsageEventDraft(
                capability=CAPABILITY_IMAGE,
                character_id=ctx.character.id,
                operator_id=getattr(ctx.character, "user_id", ""),
                feature_key=_USAGE_FEATURE_CHAT_IMAGE_TOOL,
                source_surface="chat_image_tool",
                upstream_request_id=str(
                    getattr(provider, "last_request_id", "") or "",
                ),
                provider_id=usage_parts.provider_id,
                model_id=usage_parts.model_id,
                profile_id=profile_id,
                quantity=usage_parts.quantity,
                cost=usage_parts.cost,
                latency_ms=int((completed_at - started_at).total_seconds() * 1000),
                status=status,
                error_code=error_code,
                error_message=error_message,
                artifact_count=artifact_count,
                metadata=usage_parts.metadata,
                completed_at=completed_at,
            ))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("ComfyImageTool usage recorder dispatch failed")
