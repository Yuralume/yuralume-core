"""Service-to-service Cloud→Core internal channel.

Exposes three sibling bridges, each its own scope on the same versioned
credential:

- ``/subscription-freeze`` — a tenant's whole paid tier is downgraded
  (freeze) or restored (unfreeze); fans out to every operator's characters
  under that tenant.
- ``/tenant-tier`` — a tenant's exact tier is pushed so Core's per-tier
  runtime profile takes effect without waiting for re-login.
- ``/exclusive-card-freeze`` — one official card's IP-partner contract
  starts/ends (D7 / EC10-A is the Cloud admin switch; EC10-B here is the
  consumer), silencing every character installed from that card across
  every tenant. Independent of the tenant channel above: keyed by card, not
  tenant, and writes the orthogonal ``frozen`` / ``frozen_reason`` chain
  rather than the ``subscription_locked`` projection.

Auth uses a versioned service credential (caller, audience, scope and key id), not the operator JWT.
The router is deliberately mounted without the get_current_user / require_admin
dependencies. KOKORO_CLOUD_INTERNAL_CREDENTIALS is the production rotation set;
KOKORO_CLOUD_INTERNAL_TOKENS remains an explicit R1a bearer fallback. Fail-closed: if
both are unset the endpoint returns 503; a credential matching none of the configured
entries returns 401.
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, field_validator

from kokoro_link.api.dependencies import get_container
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.infrastructure.cloud.internal_service_auth import InternalServiceCredential

router = APIRouter(prefix="/cloud", tags=["internal-cloud"])

_INTERNAL_TOKENS_ENV = "KOKORO_CLOUD_INTERNAL_TOKENS"
_INTERNAL_CREDENTIALS_ENV = "KOKORO_CLOUD_INTERNAL_CREDENTIALS"
_INTERNAL_AUDIENCE = "yuralume-core"
_INTERNAL_CALLER = "cloud-user"
_LOG = logging.getLogger(__name__)

_ACTION_FREEZE = "freeze"
_ACTION_UNFREEZE = "unfreeze"

_EXCLUSIVE_CARD_FREEZE_SCOPE = "card-freeze:write"
"""Separate scope from ``freeze:write`` / ``tier:write`` (EC10-A/EC10-B):
this switch silences every character built from one card across every
tenant, which is a strictly wider blast radius than either tenant-scoped
bridge, so a credential minted for those cannot also fire this one. The
common single-Core deployment adds this scope to the same rotated
descriptor used for the other two (see ``services/user`` README's
``YURALUME_CORE_EXCLUSIVE_FREEZE_*`` env vars, which default to the
subscription channel's own base-url/credential).

**部署前置**：此 scope 必須加進兩側共用的 R1 credential descriptor，且
Cloud 與 Core 同批更新——descriptor 少了 ``card-freeze:write`` 時，
Cloud 的 per-card 凍結呼叫一律 401，凍結開關會靜靜地什麼都沒做（Cloud
側 FU 票同批對齊）。"""


def _configured_tokens() -> frozenset[str]:
    "Parse the R1a legacy bearer allow-list at request time."
    raw = os.getenv(_INTERNAL_TOKENS_ENV, "")
    return frozenset(token.strip() for token in raw.split(",") if token.strip())


def _configured_credentials() -> tuple[InternalServiceCredential, ...]:
    "Parse the R1 credential rotation set at request time."
    raw = os.getenv(_INTERNAL_CREDENTIALS_ENV, "")
    credentials: list[InternalServiceCredential] = []
    for descriptor in raw.split(";"):
        if not descriptor.strip():
            continue
        try:
            credentials.append(InternalServiceCredential.parse(descriptor))
        except ValueError:
            _LOG.error("invalid cloud internal credential descriptor")
            return ()
    return tuple(credentials)

def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    presented = parts[1].strip()
    return presented or None


async def _require_internal_cloud_credential(
    *,
    required_scope: str,
    authorization: str | None,
    service_token: str | None,
    key_id: str | None,
    caller: str | None,
    audience: str | None,
    scope: str | None,
) -> None:
    credentials = _configured_credentials()
    if credentials and _credential_matches(
        credentials,
        required_scope=required_scope,
        token=service_token,
        key_id=key_id,
        caller=caller,
        audience=audience,
        scope=scope,
    ):
        return

    # R1a compatibility: only the old explicit bearer allow-list may pass
    # while clients are being rolled to the new credential headers.
    tokens = _configured_tokens()
    if tokens:
        presented = _extract_bearer(authorization)
        if presented is not None and _token_matches(presented, tokens):
            _LOG.info("internal_auth_legacy_hit route_scope=%s", required_scope)
            return

    if not credentials and not tokens:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="cloud internal channel not configured",
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid internal service credential",
    )


async def require_internal_cloud_token(
    authorization: str | None = Header(default=None),
    service_token: str | None = Header(default=None, alias="X-Yuralume-Service-Token"),
    key_id: str | None = Header(default=None, alias="X-Yuralume-Service-Key-Id"),
    caller: str | None = Header(default=None, alias="X-Yuralume-Service-Caller"),
    audience: str | None = Header(default=None, alias="X-Yuralume-Service-Audience"),
    scope: str | None = Header(default=None, alias="X-Yuralume-Service-Scope"),
) -> None:
    await _require_internal_cloud_credential(
        required_scope="freeze:write",
        authorization=authorization,
        service_token=service_token,
        key_id=key_id,
        caller=caller,
        audience=audience,
        scope=scope,
    )


async def require_internal_tier_credential(
    authorization: str | None = Header(default=None),
    service_token: str | None = Header(default=None, alias="X-Yuralume-Service-Token"),
    key_id: str | None = Header(default=None, alias="X-Yuralume-Service-Key-Id"),
    caller: str | None = Header(default=None, alias="X-Yuralume-Service-Caller"),
    audience: str | None = Header(default=None, alias="X-Yuralume-Service-Audience"),
    scope: str | None = Header(default=None, alias="X-Yuralume-Service-Scope"),
) -> None:
    await _require_internal_cloud_credential(
        required_scope="tier:write",
        authorization=authorization,
        service_token=service_token,
        key_id=key_id,
        caller=caller,
        audience=audience,
        scope=scope,
    )


async def require_internal_exclusive_card_credential(
    authorization: str | None = Header(default=None),
    service_token: str | None = Header(default=None, alias="X-Yuralume-Service-Token"),
    key_id: str | None = Header(default=None, alias="X-Yuralume-Service-Key-Id"),
    caller: str | None = Header(default=None, alias="X-Yuralume-Service-Caller"),
    audience: str | None = Header(default=None, alias="X-Yuralume-Service-Audience"),
    scope: str | None = Header(default=None, alias="X-Yuralume-Service-Scope"),
) -> None:
    await _require_internal_cloud_credential(
        required_scope=_EXCLUSIVE_CARD_FREEZE_SCOPE,
        authorization=authorization,
        service_token=service_token,
        key_id=key_id,
        caller=caller,
        audience=audience,
        scope=scope,
    )

def _credential_matches(
    credentials: tuple[InternalServiceCredential, ...],
    *,
    required_scope: str,
    token: str | None,
    key_id: str | None,
    caller: str | None,
    audience: str | None,
    scope: str | None,
) -> bool:
    if not token or not key_id or not caller or not audience:
        return False
    requested_scopes = {
        item.strip() for item in (scope or "").split(",") if item.strip()
    }
    if required_scope not in requested_scopes:
        return False
    if caller != _INTERNAL_CALLER or audience != _INTERNAL_AUDIENCE:
        return False
    for credential in credentials:
        if credential.key_id != key_id or credential.caller != caller:
            continue
        if credential.audience != audience or not requested_scopes.issubset(credential.scopes):
            continue
        if secrets.compare_digest(
            credential.secret.encode("utf-8"),
            token.encode("utf-8"),
        ):
            return True
    return False

def _token_matches(presented: str, tokens: frozenset[str]) -> bool:
    """Constant-time membership test, tolerant of a non-ASCII presented token.

    ``secrets.compare_digest`` raises ``TypeError`` when handed a ``str`` with
    non-ASCII characters, so a ``Bearer café…`` header would surface as a 500
    instead of a clean 401. Comparing the UTF-8 byte encodings keeps the
    comparison constant-time while accepting any Unicode input (which simply
    fails to match the ASCII allow-list)."""
    presented_bytes = presented.encode("utf-8")
    return any(
        secrets.compare_digest(presented_bytes, candidate.encode("utf-8"))
        for candidate in tokens
    )


class SubscriptionFreezeRequest(BaseModel):
    tenant_id: str
    action: str

    @field_validator("tenant_id")
    @classmethod
    def _tenant_id_non_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("tenant_id must be non-empty")
        return cleaned

    @field_validator("action")
    @classmethod
    def _action_known(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if cleaned not in {_ACTION_FREEZE, _ACTION_UNFREEZE}:
            raise ValueError("action must be 'freeze' or 'unfreeze'")
        return cleaned


class ExclusiveCardFreezeRequest(BaseModel):
    card_id: str
    action: str

    @field_validator("card_id")
    @classmethod
    def _card_id_non_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("card_id must be non-empty")
        return cleaned

    @field_validator("action")
    @classmethod
    def _action_known(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if cleaned not in {_ACTION_FREEZE, _ACTION_UNFREEZE}:
            raise ValueError("action must be 'freeze' or 'unfreeze'")
        return cleaned


class TenantTierSyncRequest(BaseModel):
    tenant_id: str
    tier: str

    @field_validator("tenant_id")
    @classmethod
    def _tenant_id_non_empty(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("tenant_id must be non-empty")
        return cleaned

    @field_validator("tier")
    @classmethod
    def _tier_non_empty(cls, value: str) -> str:
        # Mirror ``_normalise_cloud_tier`` (strip + lower) so the stored tier
        # matches the resolver's comparisons, but reject blank as a 422 rather
        # than silently defaulting to "standard".
        cleaned = (value or "").strip().lower()
        if not cleaned:
            raise ValueError("tier must be non-empty")
        return cleaned


@router.post(
    "/subscription-freeze",
    dependencies=[Depends(require_internal_cloud_token)],
)
async def subscription_freeze(
    payload: SubscriptionFreezeRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, int]:
    """Freeze / thaw every character under a Cloud tenant on tier change.

    Tenant state is committed before character projections, so access remains
    fail-closed even if a projection write fails. Such failures are counted
    and return 500 so the Cloud caller retries background-scan convergence.
    Re-running is idempotent."""
    service = container.subscription_freeze_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="subscription freeze subsystem not wired",
        )
    if payload.action == _ACTION_FREEZE:
        result = await service.freeze_all_for_cloud_tenant(payload.tenant_id)
        body = {
            "operators": result.operators,
            "frozen": result.frozen,
            "failures": result.failures,
        }
    else:
        result = await service.unfreeze_subscription_lapse_for_cloud_tenant(
            payload.tenant_id,
        )
        body = {
            "operators": result.operators,
            "unfrozen": result.unfrozen,
            "failures": result.failures,
        }
    if result.failures:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "subscription freeze partially failed", **body},
        )
    return body


@router.post(
    "/tenant-tier",
    dependencies=[Depends(require_internal_tier_credential)],
)
async def tenant_tier(
    payload: TenantTierSyncRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, int]:
    """Push a subscription tier change onto a Cloud tenant's operators.

    Mirrors ``subscription-freeze``: same scoped service-credential guard, same 503 when
    the subsystem is unwired. The write is idempotent; any repository error
    bubbles to a 500 so the Cloud caller retries through its outbox."""
    service = container.cloud_tenant_tier_sync_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="cloud tenant tier sync subsystem not wired",
        )
    result = await service.apply_tier(payload.tenant_id, payload.tier)
    return {"operators": result.operators, "updated": result.updated}


@router.post(
    "/exclusive-card-freeze",
    dependencies=[Depends(require_internal_exclusive_card_credential)],
)
async def exclusive_card_freeze(
    payload: ExclusiveCardFreezeRequest,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, int]:
    """Freeze / thaw every character installed from one official card.

    D7 / EC10-B — the Core-side consumer of the Cloud admin per-card
    contract-termination switch (EC10-A). Keyed by ``card_id``, not tenant;
    independent of ``/subscription-freeze`` above (writes ``frozen`` /
    ``frozen_reason``, not ``subscription_locked``). Any per-character
    repository failure is counted and returns 500 so the Cloud caller's
    durable outbox retries. Re-running is idempotent."""
    service = container.exclusive_card_freeze_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="exclusive card freeze subsystem not wired",
        )
    if payload.action == _ACTION_FREEZE:
        result = await service.freeze_card(payload.card_id)
        body = {
            "characters": result.characters,
            "frozen": result.frozen,
            "failures": result.failures,
        }
    else:
        result = await service.unfreeze_card(payload.card_id)
        body = {
            "characters": result.characters,
            "unfrozen": result.unfrozen,
            "failures": result.failures,
        }
    if result.failures:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "exclusive card freeze partially failed", **body},
        )
    return body
