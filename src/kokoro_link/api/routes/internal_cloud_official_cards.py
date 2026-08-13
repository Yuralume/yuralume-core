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
that module is arranged around. Here there is no card handle and no lookup:
the request carries its own text, nothing is read from the database and
nothing is written to it. The service credential and
`official-cards:translate` scope authorize that ops
action. A separate `routing_identity` comes from Cloud User's authenticated
Admin session only so the existing deployment-token Gateway path can verify
entitlement and quota; it is never used to authorize or load card data.

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
from kokoro_link.application.services.cloud_identity_context import cloud_actor_scope
from kokoro_link.application.services.official_card_translate import (
    OfficialCardTranslationUnavailable,
    build_official_card_translator,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.cloud_gateway import CloudGatewayIdentity

router = APIRouter(
    prefix="/cloud/official-cards",
    tags=["internal-cloud-official-cards"],
)

OFFICIAL_CARDS_TRANSLATE_SCOPE = "official-cards:translate"
"""Spend the deployment's routed models on translating catalog prose.
Deliberately separate from ``showcase:read``: that scope reads a
character's own feed, this one authorizes only a stateless text transform."""

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


class RoutingIdentity(BaseModel):
    """Cloud account claim used only for Gateway routing and entitlement.

    Gateway revalidates it before provider use.

    The service credential proves the caller is Cloud User; Gateway still
    introspects these ids on every cost-bearing capability request before it
    trusts them. They are never used to load or authorize card data here.
    """

    tenant_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    tenant_tier: str = Field(default="standard", min_length=1, max_length=32)

    @field_validator("tenant_id", "account_id", "tenant_tier")
    @classmethod
    def _clean_identity_value(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must be non-empty")
        return cleaned


class TranslateRequest(BaseModel):
    """A bounded card-prose object plus the language to render it in.

    Companion and personality-type prose keep their manifest nesting.
    """

    payload: dict[str, object] = Field(default_factory=dict)
    target_language: str
    routing_identity: RoutingIdentity

    @field_validator("target_language")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("must be non-empty")
        return cleaned

    @field_validator("payload")
    @classmethod
    def _bounded(cls, value: dict[str, object]) -> dict[str, object]:
        if len(value) > MAX_FIELDS:
            raise ValueError(f"at most {MAX_FIELDS} fields")
        total = 0
        for name, item in value.items():
            if not name.strip():
                raise ValueError("field names must be non-empty")
            total += _bounded_value(item)
        if total > MAX_TOTAL_CHARS:
            raise ValueError(f"at most {MAX_TOTAL_CHARS} characters in total")
        return value



def _bounded_value(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return _bounded_nested_object(value)
    if isinstance(value, list):
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError(f"at most {MAX_LIST_ITEMS} items per field")
        if all(isinstance(item, str) for item in value):
            return sum(len(item) for item in value)
        if all(isinstance(item, dict) for item in value):
            return sum(_bounded_nested_object(item) for item in value)
    raise ValueError("fields must be strings, string lists, objects, or object lists")


def _bounded_nested_object(value: dict[object, object]) -> int:
    if len(value) > MAX_FIELDS:
        raise ValueError(f"at most {MAX_FIELDS} nested fields")
    total = 0
    for raw_name, item in value.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("nested field names must be non-empty strings")
        if isinstance(item, str):
            total += len(item)
            continue
        if isinstance(item, list) and all(isinstance(entry, str) for entry in item):
            if len(item) > MAX_LIST_ITEMS:
                raise ValueError(f"at most {MAX_LIST_ITEMS} items per nested field")
            total += sum(len(entry) for entry in item)
            continue
        raise ValueError("nested fields must be strings or string lists")
    return total
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
        routing = request.routing_identity
        gateway_identity = CloudGatewayIdentity(
            operator_id=f"cloud:{routing.account_id}",
            account_id=routing.account_id,
            tenant_id=routing.tenant_id,
            character_ref="official-card-catalog",
            tenant_tier=routing.tenant_tier,
        )
        with cloud_actor_scope(gateway_identity=gateway_identity):
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
