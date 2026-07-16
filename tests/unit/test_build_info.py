from __future__ import annotations

from kokoro_link.infrastructure.build_info import get_build_info


def test_build_info_reads_project_version() -> None:
    info = get_build_info({})

    assert info.name == "Yuralume Core"
    assert info.version
    assert info.api_version == "v1"
    assert info.build.image_tag is None
    assert info.build.commit_sha is None
    assert info.build.built_at is None


def test_build_info_uses_runtime_build_metadata() -> None:
    info = get_build_info(
        {
            "YURALUME_BUILD_TAG": "v0.2.0",
            "YURALUME_BUILD_SHA": "abcdef123456",
            "YURALUME_BUILD_TIME": "2026-06-14T12:00:00Z",
        },
    )

    assert info.build.image_tag == "v0.2.0"
    assert info.build.commit_sha == "abcdef123456"
    assert info.build.built_at == "2026-06-14T12:00:00Z"


def test_build_info_falls_back_to_operator_image_tag() -> None:
    info = get_build_info({"YURALUME_IMAGE_TAG": "sha-deadbeef"})

    assert info.build.image_tag == "sha-deadbeef"
