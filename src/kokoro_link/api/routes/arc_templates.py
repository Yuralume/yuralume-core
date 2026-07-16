"""Arc template REST routes — pack rows + per-user authored rows.

After migration ``cy0d2e50075`` templates live in the ``arc_templates``
table:

- Pack rows (``user_id IS NULL``) are upserted from the bundled YAML
  on every startup and are visible to every user.
- User-authored rows are written via the intake wizard's save endpoint
  and are only visible to their owner.

The list / detail / patch / delete endpoints route through the
ownership-aware port surface so cross-user access collapses to the
same 404 every character-scoped route uses. Pack rows are read-only
from this surface — admins manage packs via the YAML files in source.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.operator_language import resolve_operator_primary_language
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.arc_template import (
    ARC_TEMPLATE_SCOPE_GENERIC,
    ArcTemplate,
    ArcTemplateBeat,
    ArcTemplateBinding,
)

router = APIRouter(tags=["arc-templates"])

_LOGGER = logging.getLogger(__name__)


class ArcTemplateBeatSummary(BaseModel):
    sequence: int
    day_offset: int
    title: str
    summary: str
    tension: str
    scene_type: str
    location: str | None
    scene_characters: list[str]
    dramatic_question: str | None
    required: bool


class ArcTemplateBindingSummary(BaseModel):
    world_frames: list[str]
    required_traits: list[str]


class ArcTemplateResponse(BaseModel):
    """Full template surface for the picker — id + display fields +
    beats so the UI can preview without a second request."""

    id: str
    title: str
    premise: str
    theme: str
    tone: str = "daily"
    language: str = "zh-TW"
    duration_days: int
    beat_count: int
    applicability_scope: str = ARC_TEMPLATE_SCOPE_GENERIC
    target_character_ids: list[str] = Field(default_factory=list)
    binding: ArcTemplateBindingSummary
    beats: list[ArcTemplateBeatSummary]

    @classmethod
    def from_domain(cls, template: ArcTemplate) -> "ArcTemplateResponse":
        return cls(
            id=template.id,
            title=template.title,
            premise=template.premise,
            theme=template.theme,
            tone=template.tone,
            language=template.language,
            duration_days=template.duration_days,
            beat_count=template.beat_count,
            applicability_scope=template.applicability_scope,
            target_character_ids=list(template.target_character_ids),
            binding=ArcTemplateBindingSummary(
                world_frames=list(template.binding.world_frames),
                required_traits=list(template.binding.required_traits),
            ),
            beats=[
                ArcTemplateBeatSummary(
                    sequence=b.sequence,
                    day_offset=b.day_offset,
                    title=b.title,
                    summary=b.summary,
                    tension=b.tension,
                    scene_type=b.scene_type,
                    location=b.location,
                    scene_characters=list(b.scene_characters),
                    dramatic_question=b.dramatic_question,
                    required=b.required,
                )
                for b in template.beats
            ],
        )


class ArcTemplateBeatPayload(BaseModel):
    """Patch-shaped beat — all fields optional except the rebuild-required
    ones. The PATCH route is whole-list replace, so missing optional
    fields fall back to the entity defaults rather than to the previous
    row's values (callers send the complete beat shape they want)."""

    sequence: int = Field(ge=0)
    day_offset: int = Field(ge=0)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    tension: str = "setup"
    scene_type: str = "encounter"
    location: str | None = None
    scene_characters: list[str] = Field(default_factory=list)
    dramatic_question: str | None = None
    required: bool = True


class UpdateArcTemplateRequest(BaseModel):
    """Whole-template replacement payload for ``PATCH /arc-templates/{id}``.

    Mirrors ``TemplateDraftPayload`` from the intake router so the
    frontend can reuse its preview shape. Patch is replace-not-merge
    because beats are an ordered list whose semantics depend on the
    full sequence (cherry-picking a single beat to edit would require
    a separate beat-level endpoint we don't need yet).
    """

    title: str = Field(min_length=1)
    premise: str = Field(min_length=1)
    theme: str = "custom"
    language: str | None = None
    """Omitted (``None``) = keep the existing row's language untouched.
    Explicit non-empty value overwrites it. Never defaults to the
    domain default here — that would reset an en/ja-authored template
    back to zh-TW on every unrelated title/beat edit."""
    tone: str = "daily"
    duration_days: int = Field(default=14, ge=1, le=365)
    world_frames: list[str] = Field(default_factory=list)
    required_traits: list[str] = Field(default_factory=list)
    applicability_scope: str = ARC_TEMPLATE_SCOPE_GENERIC
    target_character_ids: list[str] = Field(default_factory=list)
    beats: list[ArcTemplateBeatPayload] = Field(default_factory=list)


def _require_repository(container: ServiceContainer):
    if container.arc_template_repository is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arc template repository not configured",
        )
    return container.arc_template_repository


def _request_to_template(
    *, template_id: str, payload: UpdateArcTemplateRequest, language: str,
) -> ArcTemplate:
    beats = [
        ArcTemplateBeat.create(
            sequence=b.sequence,
            day_offset=b.day_offset,
            title=b.title,
            summary=b.summary,
            tension=b.tension,
            scene_type=b.scene_type,
            location=b.location,
            scene_characters=b.scene_characters,
            dramatic_question=b.dramatic_question,
            required=b.required,
        )
        for b in payload.beats
    ]
    return ArcTemplate.create(
        id=template_id,
        title=payload.title,
        premise=payload.premise,
        theme=payload.theme,
        language=language,
        tone=payload.tone,
        duration_days=payload.duration_days,
        beats=beats,
        binding=ArcTemplateBinding(
            world_frames=tuple(payload.world_frames),
            required_traits=tuple(payload.required_traits),
        ),
        applicability_scope=payload.applicability_scope,
        target_character_ids=payload.target_character_ids,
    )


async def _assert_character_visible(
    container: ServiceContainer,
    *,
    character_id: str,
    current_user_id: str,
) -> None:
    character_service = getattr(container, "character_service", None)
    if character_service is None:
        return
    character = await character_service.get_character_entity(
        character_id, user_id=current_user_id,
    )
    if character is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Character {character_id!r} not found",
        )


async def _assert_target_characters_visible(
    container: ServiceContainer,
    *,
    character_ids: list[str],
    current_user_id: str,
) -> None:
    seen: set[str] = set()
    for character_id in character_ids:
        cleaned = (character_id or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        await _assert_character_visible(
            container, character_id=cleaned, current_user_id=current_user_id,
        )


@router.get(
    "/arc-templates",
    response_model=list[ArcTemplateResponse],
    summary="列出對當前 user 可見的 arc template（pack + 自建）",
)
async def list_arc_templates(
    character_id: str | None = Query(default=None),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> list[ArcTemplateResponse]:
    """Pack rows + the caller's own rows, sorted by id.

    Used by the settings UI picker. The frontend joins ``id`` against
    each character's ``arc_template_id`` to mark the active row.
    """
    repo = _require_repository(container)
    templates = await repo.list_for_user(current_user_id)
    if character_id:
        await _assert_character_visible(
            container,
            character_id=character_id,
            current_user_id=current_user_id,
        )
        templates = [t for t in templates if t.is_applicable_to(character_id)]
    return [ArcTemplateResponse.from_domain(t) for t in templates]


@router.get(
    "/arc-templates/{template_id}",
    response_model=ArcTemplateResponse,
    summary="取得單一 arc template（pack 或自建）",
)
async def get_arc_template(
    template_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ArcTemplateResponse:
    repo = _require_repository(container)
    template = await repo.get_for_user(
        template_id, user_id=current_user_id,
    )
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Arc template {template_id!r} not found",
        )
    return ArcTemplateResponse.from_domain(template)


@router.get(
    "/arc-templates/{template_id}/preview-translation",
    response_model=ArcTemplateResponse,
    summary="預覽 arc template 翻成操作者主要語言（不寫 DB）",
)
async def preview_arc_template_translation(
    template_id: str,
    target_language: str | None = Query(default=None),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ArcTemplateResponse:
    """Return the template rendered in the operator's language.

    Read-only preview for the picker's "翻成我的語言" toggle — the
    translation is never persisted. Falls back to the authored prose
    (fail-soft) when no translator is wired, the languages already match,
    or the LLM call fails, so the picker always gets a usable body.
    """
    repo = _require_repository(container)
    template = await repo.get_for_user(
        template_id, user_id=current_user_id,
    )
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Arc template {template_id!r} not found",
        )
    target = (target_language or "").strip()
    if not target:
        target = await resolve_operator_primary_language(
            container, current_user_id,
        )
    translator = getattr(container, "arc_template_translator", None)
    if (
        translator is not None
        and target
        and (template.language or "").strip().casefold() != target.casefold()
    ):
        try:
            template = await translator.translate_template(
                template, target_language=target,
            )
        except Exception:  # pragma: no cover — adapters are fail-soft
            _LOGGER.exception(
                "arc template preview translation failed template=%s",
                template_id,
            )
    return ArcTemplateResponse.from_domain(template)


@router.patch(
    "/arc-templates/{template_id}",
    response_model=ArcTemplateResponse,
    summary="覆寫自建 arc template（pack 不可寫）",
)
async def update_arc_template(
    template_id: str,
    payload: UpdateArcTemplateRequest,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> ArcTemplateResponse:
    """Owner-only replace. Pack rows raise 409 because their content
    comes from source-controlled YAML; user-owned rows are replaced
    whole (beats are an ordered list — partial merge would be
    ambiguous). Cross-user access collapses to 404 to match the rest
    of the per-user surface."""
    repo = _require_repository(container)
    await _assert_target_characters_visible(
        container,
        character_ids=payload.target_character_ids,
        current_user_id=current_user_id,
    )
    existing = await repo.get_for_user(template_id, user_id=current_user_id)
    if payload.language is not None and payload.language.strip():
        resolved_language = payload.language.strip()
    elif existing is not None:
        resolved_language = existing.language
    else:
        resolved_language = await resolve_operator_primary_language(
            container, current_user_id,
        )
    template = _request_to_template(
        template_id=template_id, payload=payload, language=resolved_language,
    )
    try:
        await repo.save_for_user(
            template, user_id=current_user_id, overwrite=True,
        )
    except ValueError as exc:
        # ``save_for_user`` raises for: pack collision, foreign-owner
        # collision, validation. Pack collisions are intentional — the
        # user is asking to mutate a pack row. 409 mirrors the intake
        # wizard's collision shape so the frontend can reuse handling.
        if "reserved by a bundled pack" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    saved = await repo.get_for_user(template_id, user_id=current_user_id)
    if saved is None:
        # Sanity guard — we just wrote it.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Template saved but could not be re-loaded",
        )
    return ArcTemplateResponse.from_domain(saved)


@router.delete(
    "/arc-templates/{template_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="刪除自建 arc template",
)
async def delete_arc_template(
    template_id: str,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> None:
    """Owner-only delete. Pack rows (no owner) and rows belonging to
    other users both return 404 — pack management lives in the YAML
    source, and cross-user enumeration is shut down at the boundary."""
    repo = _require_repository(container)
    removed = await repo.delete_for_user(template_id, user_id=current_user_id)
    if not removed:
        # Check whether the slug exists but belongs to a pack so we can
        # surface a 409 (informative) instead of a generic 404 in that
        # specific case. The cross-user case still collapses to 404.
        visible = await repo.get_for_user(
            template_id, user_id=current_user_id,
        )
        if visible is not None:
            # Visible row that wasn't deleted = pack row. (User-owned
            # row would have been removed by the delete above.)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Template {template_id!r} is a bundled pack — "
                    "edit the YAML in source to remove it."
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Arc template {template_id!r} not found",
        )
