from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

APP_NAME = "Yuralume Core"
API_VERSION = "v1"
PACKAGE_NAME = "yuralume"
UNKNOWN_VERSION = "0.0.0"


@dataclass(frozen=True, slots=True)
class BuildMetadata:
    image_tag: str | None
    commit_sha: str | None
    built_at: str | None


@dataclass(frozen=True, slots=True)
class BuildInfo:
    name: str
    version: str
    api_version: str
    build: BuildMetadata


def get_build_info(environ: Mapping[str, str] | None = None) -> BuildInfo:
    env = environ if environ is not None else os.environ
    app_version = _env_value(env, "YURALUME_APP_VERSION") or _project_version()
    return BuildInfo(
        name=APP_NAME,
        version=app_version,
        api_version=API_VERSION,
        build=BuildMetadata(
            image_tag=(
                _env_value(env, "YURALUME_BUILD_TAG")
                or _env_value(env, "YURALUME_IMAGE_TAG")
            ),
            commit_sha=_env_value(env, "YURALUME_BUILD_SHA"),
            built_at=_env_value(env, "YURALUME_BUILD_TIME"),
        ),
    )


def _env_value(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _project_version() -> str:
    try:
        return package_version(PACKAGE_NAME)
    except PackageNotFoundError:
        return _pyproject_version() or UNKNOWN_VERSION


def _pyproject_version() -> str | None:
    pyproject_path = Path(__file__).resolve().parents[3] / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError:
        return None
    project = data.get("project")
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    if not isinstance(version, str):
        return None
    return version.strip() or None
