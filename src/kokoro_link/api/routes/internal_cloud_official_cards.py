"""Cloud→Core internal channel for the official card catalog.

One endpoint, and it is a **pure text transformation**: prose in, prose
out. The Cloud control plane owns the official cards (artifact, per-locale
translation rows, approval state); Core owns the card translator's field
policy and merge discipline, which is the only thing being borrowed here
(plan §3.4 / D4 — a Java reimplementation would be a second source of
truth that drifts on the first new profile field).

**Why this router has no tenant scoping, unlike the showcase one.**
``internal_cloud_showcase`` answers 404 for another tenant's character
because ``character_id`` there is a handle on somebody's private feed, and
publishing the wrong character's posts on a marketing page is the failure
that module is arranged around. Here there is no handle and no lookup:
the request carries its own text, nothing is read from the database and
nothing is written to it. The only thing at stake is the deployment's LLM
budget, and that is exactly what the credential's scope governs — so the
scope check *is* the whole authorization story.

Auth is the same versioned service credential the freeze / tier / showcase
bridges use (``KOKORO_CLOUD_INTERNAL_CREDENTIALS``), with its own scope
``official-cards:translate``. Unconfigured means 503: a self-hosted
deployment simply never turns this channel on.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from kokoro_link.api.dependencies import get_container
from kokoro_link.api.routes.internal_cloud import _require_internal_cloud_credential
from kokoro_link.application.services.official_card_translate import (
    OfficialCardTranslationUnavailable,
    build_official_card_translator,
)
from kokoro_link.bootstrap.container import ServiceContainer

router = APIRouter(
    prefix="/cloud/official-cards",
    tags=["internal-cloud-official-cards"],
)

OFFICIAL_CARDS_TRANSLATE_SCOPE = "official-cards:translate"
"""Spend the deployment's routed models on translating catalog prose.
Deliberately separate from ``showcase:read``: that scope reads a
character's own feed, this one reads nothing at all."""

MAX_FIELDS = 64
MAX_LIST_ITEMS = 200
MAX_TOTAL_CHARS = 40_000
"""Bounds on one card's payload. A card's prose is a few thousand
characters; anything past these is a caller bug (a whole manifest dumped
in, a runaway list) and should fail loudly at the edge rather than become
a giant prompt."""


async def official_cards_translate_credential(
    authorization: str | None = Header(default=None),
    service_token: str | None = Header(default=None, alias="X-Yuralume-Service-Token"),
    key_id: str | None = Header(default=None, alias="X-Yuralume-Service-Key-Id"),
    caller: str | None = Header(default=None, alias="X-Yuralume-Service-Caller"),
    audience: str | None = Header(default=None, alias="X-Yuralume-Service-Audience"),
    scope: str | None = Header(default=None, alias="X-Yuralume-Service-Scope"),
) -> None:
    await _require_internal_cloud_credential(
        required_scope=OFFICIAL_CARDS_TRANSLATE_SCOPE,
        authorization=authorization,
        service_token=service_token,
        key_id=key_id,
        caller=caller,
        audience=audience,
        scope=scope,
    )


_GUARD = [Depends(official_cards_translate_credential)]


class TranslateRequest(BaseModel):
    """A flat field → text map plus the language to render it in.

    Flat because profile prose and card meta share one namespace without
    collisions (``name`` is the character's, ``title`` is the card's), and
    because the control plane stores exactly this shape per locale.
    """

    payload: dict[str, str | list[str]] = Field(default_factory=dict)
    target_language: str

    @field_validator("target_language")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must be non-empty")
        return cleaned

    @field_validator("payload")
    @classmethod
    def _bounded(
        cls, value: dict[str, str | list[str]],
    ) -> dict[str, str | list[str]]:
        if len(value) > MAX_FIELDS:
            raise ValueError(f"at most {MAX_FIELDS} fields")
        total = 0
        for name, item in value.items():
            if not name.strip():
                raise ValueError("field names must be non-empty")
            if isinstance(item, list):
                if len(item) > MAX_LIST_ITEMS:
                    raise ValueError(f"at most {MAX_LIST_ITEMS} items per field")
                total += sum(len(entry) for entry in item)
            else:
                total += len(item)
        if total > MAX_TOTAL_CHARS:
            raise ValueError(f"at most {MAX_TOTAL_CHARS} characters in total")
        return value


@router.post("/translate", dependencies=_GUARD)
async def translate(
    request: TranslateRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, object]:
    """Translate one card's catalog prose into one language.

    Stateless: nothing here is read from or written to the database, and
    the response is the caller's own payload rendered in another language.

    ``notes`` is the contract that makes partial failure usable. A field
    the model dropped, emptied or answered with the wrong list length
    comes back as **source text** with an entry naming it and why, so the
    console can show the owner what to retry instead of failing the whole
    card over one stubborn field. An empty ``notes`` means every field
    with content was translated.
    """
    try:
        translator = build_official_card_translator(container)
        result = await translator.translate(
            request.payload,
            target_language=request.target_language,
        )
    except OfficialCardTranslationUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return {
        "payload": result.payload,
        "notes": [
            {"field": note.field, "reason": note.reason} for note in result.notes
        ],
    }


__all__ = ["OFFICIAL_CARDS_TRANSLATE_SCOPE", "router"]
