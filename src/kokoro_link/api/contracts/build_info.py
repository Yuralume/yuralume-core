from __future__ import annotations

from pydantic import BaseModel

from kokoro_link.infrastructure.build_info import BuildInfo


class BuildMetadataResponse(BaseModel):
    image_tag: str | None = None
    commit_sha: str | None = None
    built_at: str | None = None


class BuildInfoResponse(BaseModel):
    name: str
    version: str
    api_version: str
    build: BuildMetadataResponse


def build_info_response(info: BuildInfo) -> BuildInfoResponse:
    return BuildInfoResponse(
        name=info.name,
        version=info.version,
        api_version=info.api_version,
        build=BuildMetadataResponse(
            image_tag=info.build.image_tag,
            commit_sha=info.build.commit_sha,
            built_at=info.build.built_at,
        ),
    )
