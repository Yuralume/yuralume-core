"""Cloud→Core internal channel for hosted operations counters.

One endpoint today: ``GET /cloud/stats/characters`` — how many characters this
deployment stores and how many of them still cost background work. It is what
the Cloud admin console's dashboard card reads, and it is a pure aggregate:
no ids, no names, no tenant slice, nothing that could identify a player.

Auth is the same versioned service credential as the freeze / tier / showcase
bridges, with its own scope ``stats:read``. Separate on purpose: a credential
minted to move subscription state or to read one tenant's feed has no business
reading a platform-wide census, and — read the other way — this scope grants a
caller nothing about any individual character.

**部署前置**：``stats:read`` 必須加進兩側共用的 R1 credential descriptor
(``KOKORO_CLOUD_INTERNAL_CREDENTIALS`` / ``YURALUME_CORE_FREEZE_CREDENTIAL``)。
少了它時本端點一律 401，Cloud 側會把它降級成「讀不到」——儀表板顯示「—」而
不是 0，因為零和讀不到是兩件事。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status

from kokoro_link.api.dependencies import get_container
from kokoro_link.api.routes.internal_cloud import _require_internal_cloud_credential
from kokoro_link.bootstrap.container import ServiceContainer

router = APIRouter(prefix="/cloud/stats", tags=["internal-cloud-stats"])

STATS_SCOPE = "stats:read"
"""Read platform-wide aggregate counters. Deliberately separate from
``freeze:write`` / ``tier:write`` (those move state) and from
``showcase:read`` (that one reads one character's content)."""


async def stats_credential(
    authorization: str | None = Header(default=None),
    service_token: str | None = Header(default=None, alias="X-Yuralume-Service-Token"),
    key_id: str | None = Header(default=None, alias="X-Yuralume-Service-Key-Id"),
    caller: str | None = Header(default=None, alias="X-Yuralume-Service-Caller"),
    audience: str | None = Header(default=None, alias="X-Yuralume-Service-Audience"),
    scope: str | None = Header(default=None, alias="X-Yuralume-Service-Scope"),
) -> None:
    await _require_internal_cloud_credential(
        required_scope=STATS_SCOPE,
        authorization=authorization,
        service_token=service_token,
        key_id=key_id,
        caller=caller,
        audience=audience,
        scope=scope,
    )


@router.get("/characters", dependencies=[Depends(stats_credential)])
async def character_stats(
    container: ServiceContainer = Depends(get_container),
) -> dict[str, int]:
    """``{"total": N, "active": M}`` — the shelf, and the part still running.

    503 when the read model is not wired (a deployment with no database at
    all). Never a zero: the caller renders a missing number as "—", and a
    fabricated 0 would read as "every character went quiet"."""
    service = container.character_activity_stats_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="character stats subsystem not wired",
        )
    counts = await service.collect()
    return {"total": counts.total, "active": counts.active}
