"""Parse :class:`ImageProfile` definitions from operator config.

Two input shapes are supported on ``KOKORO_IMAGE_PROFILES``:

  * **A filesystem path** to a JSON file holding a list of profile
    objects. Preferred for non-trivial setups (multiple profiles, long
    API keys) because it keeps the env terse and lets the JSON live
    next to the rest of the config.

  * **Inline JSON** — the env value parses as a JSON list directly.
    Handy for one-line docker-compose overrides.

When the variable is empty, :func:`load_image_profiles` may synthesise
a single ``default`` external API profile from ``KOKORO_IMAGE_API_*``.
Local ComfyUI / direct provider shapes are still accepted when they are
declared explicitly in JSON, but they are no longer inferred from global
env vars.

String values support ``${ENV_VAR}`` interpolation so operators can
keep API keys out of the JSON file and reference an env var instead.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from kokoro_link.contracts.image_profile import (
    ComfyProfileConfig,
    ExternalImageApiProfileConfig,
    ImageProfile,
    OpenAIProfileConfig,
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


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
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


def _parse_profile(raw: dict[str, Any]) -> ImageProfile | None:
    pid = _coerce_str(raw.get("id"))
    if not pid:
        _LOGGER.warning("image profile dropped: missing id (%r)", raw)
        return None
    kind = _coerce_str(raw.get("kind")).lower()
    if kind in {"api", "external", "yuralume_image_api"}:
        kind = "external_api"
    if kind not in {"external_api", "comfyui", "openai"}:
        _LOGGER.warning(
            "image profile %r dropped: unsupported kind %r", pid, kind,
        )
        return None
    label = _coerce_str(raw.get("label")) or pid

    if kind == "external_api":
        cfg_raw = raw.get("api") or raw.get("external_api") or raw.get("image_api") or {}
        if not isinstance(cfg_raw, dict):
            _LOGGER.warning(
                "image profile %r dropped: api config not an object", pid,
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
                "image profile %r dropped: api base_url/api_key required", pid,
            )
            return None
        return ImageProfile(
            id=pid,
            label=label,
            kind="external_api",
            api=ExternalImageApiProfileConfig(
                base_url=base_url.rstrip("/"),
                api_key=api_key,
                model=model,
                provider=provider,
                timeout_seconds=_coerce_float(
                    cfg_raw.get("timeout_seconds"), default=180.0,
                ),
            ),
        )

    if kind == "comfyui":
        cfg_raw = raw.get("comfyui") or {}
        if not isinstance(cfg_raw, dict):
            _LOGGER.warning(
                "image profile %r dropped: comfyui config not an object", pid,
            )
            return None
        server = _coerce_str(cfg_raw.get("server"))
        if not server:
            _LOGGER.warning(
                "image profile %r dropped: comfyui.server is required", pid,
            )
            return None
        comfy = ComfyProfileConfig(
            server=server,
            checkpoint=_coerce_str(
                cfg_raw.get("checkpoint"),
                default="waiNSFWIllustrious_v140.safetensors",
            ) or "waiNSFWIllustrious_v140.safetensors",
            workflow_file=_coerce_str(cfg_raw.get("workflow_file")),
            generation_timeout_seconds=_coerce_float(
                cfg_raw.get("generation_timeout_seconds"), default=180.0,
            ),
            use_prompt_rewriter=_coerce_bool(
                cfg_raw.get("use_prompt_rewriter"), default=True,
            ),
        )
        return ImageProfile(
            id=pid, label=label, kind="comfyui", comfyui=comfy,
        )

    # openai
    cfg_raw = raw.get("openai") or {}
    if not isinstance(cfg_raw, dict):
        _LOGGER.warning(
            "image profile %r dropped: openai config not an object", pid,
        )
        return None
    api_key = _coerce_str(cfg_raw.get("api_key"))
    if not api_key:
        _LOGGER.warning(
            "image profile %r dropped: openai.api_key resolves empty", pid,
        )
        return None
    base_url = _coerce_str(
        cfg_raw.get("base_url"),
        default="https://api.openai.com/v1",
    ) or "https://api.openai.com/v1"
    openai_cfg = OpenAIProfileConfig(
        api_key=api_key,
        model=_coerce_str(cfg_raw.get("model"), default="gpt-image-2")
        or "gpt-image-2",
        quality=_coerce_str(cfg_raw.get("quality"), default="medium")
        or "medium",
        timeout_seconds=_coerce_float(
            cfg_raw.get("timeout_seconds"), default=180.0,
        ),
        base_url=base_url.rstrip("/"),
    )
    return ImageProfile(
        id=pid, label=label, kind="openai", openai=openai_cfg,
    )


def load_image_profiles(
    *,
    raw_config: str,
    default_api: ExternalImageApiProfileConfig | None = None,
    legacy_comfy: ComfyProfileConfig | None = None,
    legacy_openai: OpenAIProfileConfig | None = None,
) -> list[ImageProfile]:
    """Resolve the operator's image-profile list.

    ``raw_config`` is the value of ``KOKORO_IMAGE_PROFILES`` — a path
    to a JSON file, an inline JSON list, or empty. When empty, we
    synthesise a single ``default`` external API profile from
    ``KOKORO_IMAGE_API_*`` if those gateway provider/endpoint/key/model
    settings exist.

    ``legacy_*`` parameters are accepted for backward-compatible callers,
    but they no longer create default profiles from local ComfyUI/OpenAI
    image settings.
    """
    profiles = _load_from_raw(raw_config)
    if profiles:
        return profiles

    if default_api is not None:
        return [
            ImageProfile(
                id="default",
                label=default_api.model,
                kind="external_api",
                api=default_api,
            ),
        ]
    return []


def _load_from_raw(raw: str) -> list[ImageProfile]:
    text = (raw or "").strip()
    if not text:
        return []
    # File path → read the file. Heuristic: if the value doesn't start
    # with ``[`` or ``{``, treat it as a path. Lets operators write
    # either ``KOKORO_IMAGE_PROFILES=/etc/kokoro/profiles.json`` or the
    # inline ``KOKORO_IMAGE_PROFILES=[{...}]``.
    if not text.startswith("[") and not text.startswith("{"):
        path = Path(text).expanduser()
        if not path.exists():
            _LOGGER.warning(
                "KOKORO_IMAGE_PROFILES file %s not found; ignoring", path,
            )
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            _LOGGER.warning(
                "KOKORO_IMAGE_PROFILES file %s unreadable: %s", path, exc,
            )
            return []

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        _LOGGER.warning("KOKORO_IMAGE_PROFILES json invalid: %s", exc)
        return []

    if not isinstance(payload, list):
        _LOGGER.warning(
            "KOKORO_IMAGE_PROFILES root must be a JSON list, got %s",
            type(payload).__name__,
        )
        return []

    profiles: list[ImageProfile] = []
    seen_ids: set[str] = set()
    for raw_profile in payload:
        if not isinstance(raw_profile, dict):
            continue
        profile = _parse_profile(raw_profile)
        if profile is None:
            continue
        if profile.id in seen_ids:
            _LOGGER.warning(
                "duplicate image profile id %r — keeping the first", profile.id,
            )
            continue
        seen_ids.add(profile.id)
        profiles.append(profile)
    return profiles
