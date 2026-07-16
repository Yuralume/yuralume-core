from __future__ import annotations

from fastapi import APIRouter

from kokoro_link.api.contracts.build_info import (
    BuildInfoResponse,
    build_info_response,
)
from kokoro_link.infrastructure.build_info import get_build_info

router = APIRouter(tags=["system"])


@router.get("/system/version", response_model=BuildInfoResponse)
async def get_system_version() -> BuildInfoResponse:
    return build_info_response(get_build_info())
