"""Parse :class:`VideoProfile` definitions from operator config.

Same shape as :mod:`kokoro_link.bootstrap.image_profiles`:

  * ``KOKORO_VIDEO_PROFILES`` env value is either a path to a JSON
    file or inline JSON. Empty can still produce one ``external_api``
    profile from ``KOKORO_VIDEO_API_*``.

  * String values support ``${ENV_VAR}`` interpolation so the same
    indirection works for any future hosted backend that needs an
    API key.

Legacy local workflow profiles can still be declared explicitly in JSON,
but the main settings path is gateway provider/endpoint/key/model only.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from kokoro_link.contracts.video_profile import (
    ExternalVideoApiProfileConfig,
    VideoProfile,
    WanVideoProfileConfig,
)

_LOGGER = logging.getLogger(__name__)
_ENV_INTERP = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _interpolate(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return os.getenv(match.group(1), "")

    return _ENV_INTERP.sub(replace, value)


def _coerce_str(value: Any, *, default: str = "") -> str:
    if isinstance(value, str):
        return _interpolate(value).strip()
    return default


def _coerce_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _coerce_float(value: Any, *, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _parse_profile(raw: dict[str, Any]) -> VideoProfile | None:
    pid = _coerce_str(raw.get("id"))
    if not pid:
        _LOGGER.warning("video profile dropped: missing id (%r)", raw)
        return None
    kind = _coerce_str(raw.get("kind")).lower()
    if kind in {"api", "external", "yuralume_video_api"}:
        kind = "external_api"
    if kind not in {"external_api", "comfyui_wan22"}:
        _LOGGER.warning(
            "video profile %r dropped: unsupported kind %r", pid, kind,
        )
        return None
    label = _coerce_str(raw.get("label")) or pid

    if kind == "external_api":
        cfg_raw = raw.get("api") or raw.get("external_api") or raw.get("video_api") or {}
        if not isinstance(cfg_raw, dict):
            _LOGGER.warning(
                "video profile %r dropped: api config not an object", pid,
            )
            return None
        base_url = _coerce_str(cfg_raw.get("base_url"))
        api_key = _coerce_str(cfg_raw.get("api_key"))
        model = _coerce_str(cfg_raw.get("model"), default=pid) or pid
        provider = (
            _coerce_str(cfg_raw.get("provider"), default="gateway").lower()
            or "gateway"
        )
        if not base_url or not api_key:
            _LOGGER.warning(
                "video profile %r dropped: api base_url/api_key required", pid,
            )
            return None
        return VideoProfile(
            id=pid,
            label=label,
            kind="external_api",
            api=ExternalVideoApiProfileConfig(
                base_url=base_url.rstrip("/"),
                api_key=api_key,
                model=model,
                provider=provider,
                timeout_seconds=_coerce_float(
                    cfg_raw.get("timeout_seconds"), default=1800.0,
                ),
            ),
        )

    cfg_raw = raw.get("comfyui_wan22") or {}
    if not isinstance(cfg_raw, dict):
        _LOGGER.warning(
            "video profile %r dropped: comfyui_wan22 config not an object", pid,
        )
        return None
    server = _coerce_str(cfg_raw.get("server"))
    if not server:
        _LOGGER.warning(
            "video profile %r dropped: comfyui_wan22.server is required", pid,
        )
        return None
    cfg = WanVideoProfileConfig(
        server=server,
        workflow_file=_coerce_str(cfg_raw.get("workflow_file")),
        fps=_coerce_int(cfg_raw.get("fps"), default=16),
        length_frames=_coerce_int(cfg_raw.get("length_frames"), default=81),
        width=_coerce_int(cfg_raw.get("width"), default=832),
        height=_coerce_int(cfg_raw.get("height"), default=480),
        generation_timeout_seconds=_coerce_float(
            cfg_raw.get("generation_timeout_seconds"), default=1800.0,
        ),
    )
    return VideoProfile(
        id=pid, label=label, kind="comfyui_wan22", comfyui_wan22=cfg,
    )


def load_video_profiles(
    *,
    raw_config: str,
    default_api: ExternalVideoApiProfileConfig | None = None,
) -> list[VideoProfile]:
    text = (raw_config or "").strip()
    if not text:
        if default_api is None:
            return []
        return [
            VideoProfile(
                id="default",
                label=default_api.model,
                kind="external_api",
                api=default_api,
            ),
        ]
    if not text.startswith("[") and not text.startswith("{"):
        path = Path(text).expanduser()
        if not path.exists():
            _LOGGER.warning(
                "KOKORO_VIDEO_PROFILES file %s not found; ignoring", path,
            )
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            _LOGGER.warning(
                "KOKORO_VIDEO_PROFILES file %s unreadable: %s", path, exc,
            )
            return []

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        _LOGGER.warning("KOKORO_VIDEO_PROFILES json invalid: %s", exc)
        return []

    if not isinstance(payload, list):
        _LOGGER.warning(
            "KOKORO_VIDEO_PROFILES root must be a JSON list, got %s",
            type(payload).__name__,
        )
        return []

    profiles: list[VideoProfile] = []
    seen: set[str] = set()
    for raw_profile in payload:
        if not isinstance(raw_profile, dict):
            continue
        profile = _parse_profile(raw_profile)
        if profile is None:
            continue
        if profile.id in seen:
            _LOGGER.warning(
                "duplicate video profile id %r — keeping the first", profile.id,
            )
            continue
        seen.add(profile.id)
        profiles.append(profile)
    return profiles
