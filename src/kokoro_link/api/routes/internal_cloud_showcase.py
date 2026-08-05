"""Cloud→Core internal channel for the public showcase pipeline.

The Cloud control plane owns the owner's approvals, the translation cache
and the published document; Core owns the posts, the character card, the
schedule and the models. These four endpoints are that seam and nothing
more — every one of them is a read or a pure computation, and none of
them stores anything.

Auth is the same versioned service credential the freeze / tier bridges
use (``KOKORO_CLOUD_INTERNAL_CREDENTIALS``), with its own scope
``showcase:read`` so a credential minted for tier pushes cannot read a
character's feed. Unconfigured means 503, as everywhere on this router:
a self-hosted deployment simply never turns this channel on.

**Tenant scoping is the red line.** ``character_id`` alone is not
authority — the service resolves the tenant's operators first and answers
404 when the character is not one of theirs. Publishing the wrong
character's feed on a public marketing page is the failure this whole
module is arranged around.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from kokoro_link.api.dependencies import get_container
from kokoro_link.api.routes.internal_cloud import _require_internal_cloud_credential
from kokoro_link.application.services.showcase.service import (
    DEFAULT_POST_LIMIT,
    ShowcaseError,
    ShowcaseNotFound,
    SnapshotPost,
    TranslationRequest,
    build_showcase_service,
    post_source_hash,
)
from kokoro_link.bootstrap.container import ServiceContainer

router = APIRouter(prefix="/cloud/showcase", tags=["internal-cloud-showcase"])

SHOWCASE_SCOPE = "showcase:read"
"""Read the character's own feed, and spend the deployment's routed models
on reviewing / translating it. Deliberately separate from ``freeze:write``
and ``tier:write``: those move subscription state and never read content."""

MAX_POST_LIMIT = 200
MAX_BATCH = 200


async def showcase_credential(
    authorization: str | None = Header(default=None),
    service_token: str | None = Header(default=None, alias="X-Yuralume-Service-Token"),
    key_id: str | None = Header(default=None, alias="X-Yuralume-Service-Key-Id"),
    caller: str | None = Header(default=None, alias="X-Yuralume-Service-Caller"),
    audience: str | None = Header(default=None, alias="X-Yuralume-Service-Audience"),
    scope: str | None = Header(default=None, alias="X-Yuralume-Service-Scope"),
) -> None:
    await _require_internal_cloud_credential(
        required_scope=SHOWCASE_SCOPE,
        authorization=authorization,
        service_token=service_token,
        key_id=key_id,
        caller=caller,
        audience=audience,
        scope=scope,
    )


_GUARD = [Depends(showcase_credential)]


# --------------------------------------------------------------------------- #
# payloads
# --------------------------------------------------------------------------- #


class _TenantScoped(BaseModel):
    tenant_id: str
    character_id: str

    @field_validator("tenant_id", "character_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must be non-empty")
        return cleaned


class ReviewRequest(_TenantScoped):
    post_ids: list[str] = Field(default_factory=list, max_length=MAX_BATCH)
    limit: int = Field(default=DEFAULT_POST_LIMIT, ge=1, le=MAX_POST_LIMIT)


class TranslateItem(BaseModel):
    key: str
    text: str


class TranslateRequest(_TenantScoped):
    items: list[TranslateItem] = Field(default_factory=list, max_length=MAX_BATCH)
    locales: list[str] = Field(default_factory=list)


class SnapshotPostPayload(BaseModel):
    id: str
    kind: str = ""
    created_at: str = ""
    text: dict[str, str] = Field(default_factory=dict)
    image_url: str | None = None


class SnapshotRequest(_TenantScoped):
    posts: list[SnapshotPostPayload] = Field(default_factory=list, max_length=1000)
    base_url: str
    locales: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #


@router.get("/characters", dependencies=_GUARD)
async def list_characters(
    tenant_id: str = Query(..., min_length=1),
    container: ServiceContainer = Depends(get_container),
) -> dict[str, object]:
    """The tenant's roster, for the console's character picker."""
    service = build_showcase_service(container)
    with _translated_errors():
        summaries = await service.list_characters(tenant_id=tenant_id)
    return {
        "characters": [
            {
                "id": summary.id,
                "name": summary.name,
                "summary": summary.summary,
                "created_at": _iso(summary.created_at),
            }
            for summary in summaries
        ],
    }


@router.get("/candidates", dependencies=_GUARD)
async def list_candidates(
    tenant_id: str = Query(..., min_length=1),
    character_id: str = Query(..., min_length=1),
    limit: int = Query(DEFAULT_POST_LIMIT, ge=1, le=MAX_POST_LIMIT),
    container: ServiceContainer = Depends(get_container),
) -> dict[str, object]:
    """Publishable candidates, already past the mechanical filter.

    ``source_hash`` travels with every candidate: it is what the control
    plane stores instead of the post body, and what it compares again at
    publish time so an approval cannot silently move to edited text.
    """
    service = build_showcase_service(container)
    with _translated_errors():
        bundle = await service.list_candidates(
            tenant_id=tenant_id, character_id=character_id, limit=limit,
        )
    return {
        "character": {
            "id": bundle.character_id,
            "name": bundle.character_name,
            "persona": bundle.persona,
        },
        "posts_scanned": bundle.posts_scanned,
        "excluded": dict(bundle.outcome.excluded),
        "candidates": [
            {
                "id": candidate.id,
                "kind": candidate.kind,
                "created_at": _iso(candidate.created_at),
                "text": candidate.text,
                "image_url": candidate.image_url,
                "source_hash": post_source_hash(candidate.text),
            }
            for candidate in bundle.outcome.candidates
        ],
    }


@router.post("/review", dependencies=_GUARD)
async def review(
    payload: ReviewRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, object]:
    """Advisory pre-review. Never approves anything; a post that could not
    be reviewed comes back ``needs_manual_review`` with the reason."""
    service = build_showcase_service(container)
    with _translated_errors():
        summary = await service.review_posts(
            tenant_id=payload.tenant_id,
            character_id=payload.character_id,
            post_ids=payload.post_ids,
            limit=payload.limit,
        )
    return {
        "passed": summary.passed,
        "flagged": summary.flagged,
        "failed": summary.failed,
        "reviews": [
            {
                "post_id": item.post_id,
                "verdict": item.verdict,
                "reasons": list(item.reasons),
                "rewrite": item.rewrite,
            }
            for item in summary.reviews
        ],
    }


@router.post("/translate", dependencies=_GUARD)
async def translate(
    payload: TranslateRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, object]:
    """Translate approved text, carrying the character's voice.

    ``missing`` is the contract that lets the caller keep its promise not
    to publish a half-translated post — a locale that failed is named, not
    quietly replaced with the source text.
    """
    service = build_showcase_service(container)
    with _translated_errors():
        results = await service.translate_texts(
            tenant_id=payload.tenant_id,
            character_id=payload.character_id,
            items=[
                TranslationRequest(key=item.key, text=item.text)
                for item in payload.items
            ],
            locales=payload.locales,
        )
    return {
        "results": [
            {
                "key": result.key,
                "translations": dict(result.translations),
                "missing": list(result.missing),
            }
            for result in results
        ],
    }


@router.post("/snapshot", dependencies=_GUARD)
async def snapshot(
    payload: SnapshotRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, object]:
    """Render the public document from already-approved posts.

    The card, the "now" block and the schedule strip are read and
    translated here on every call: they are true only at the moment of
    publishing, so they are never cached against an approval.
    """
    service = build_showcase_service(container)
    with _translated_errors():
        result = await service.render_snapshot(
            tenant_id=payload.tenant_id,
            character_id=payload.character_id,
            posts=[
                SnapshotPost(
                    id=post.id,
                    kind=post.kind,
                    created_at=post.created_at,
                    text=dict(post.text),
                    image_url=post.image_url,
                )
                for post in payload.posts
            ],
            base_url=payload.base_url,
            locales=payload.locales,
        )
    return {
        "document": result.document,
        "post_count": result.post_count,
        "skipped_posts": list(result.skipped_posts),
        "warnings": list(result.warnings),
    }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class _translated_errors:  # noqa: N801 — used as a context manager
    """Map the service's refusals onto HTTP without leaking which tenant
    owns what: an unknown character and another tenant's character are the
    same 404."""

    def __enter__(self) -> "_translated_errors":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:  # noqa: ANN001
        if exc is None:
            return False
        if isinstance(exc, ShowcaseNotFound):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="character not available to this tenant",
            ) from exc
        if isinstance(exc, ShowcaseError):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        return False


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.isoformat()


__all__ = ["SHOWCASE_SCOPE", "router"]
