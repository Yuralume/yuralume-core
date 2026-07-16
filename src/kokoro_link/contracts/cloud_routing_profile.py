"""Core-side view of the control-plane routing profile (plan Phase 3 / §5.2).

In cloud mode Core asks the control-plane for a per-tenant/account routing profile
(``feature_key -> preset`` maps + flags) instead of parsing ``YURALUME_CLOUD_*``
preset env. The profile is cached and refreshed in the background so the chat /
generation hot path is an in-process O(1) lookup (§3.2.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class CloudRoutingProfileUnavailable(RuntimeError):
    """Raised when the control-plane routing profile cannot be fetched."""


@dataclass(frozen=True, slots=True)
class CloudRoutingProfile:
    llm_feature_presets: dict[str, str]
    image_feature_presets: dict[str, str]
    video_feature_presets: dict[str, str]
    tts_voice_defaults: dict[str, str]
    strict_no_fallback: bool
    disabled_features: frozenset[str]
    catalog_version: int
    routing_policy_version: int

    def preset_for(self, capability: str, feature_key: str) -> str | None:
        mapping = {
            "llm": self.llm_feature_presets,
            "image": self.image_feature_presets,
            "video": self.video_feature_presets,
            "tts": self.tts_voice_defaults,
        }.get(capability, {})
        return mapping.get(feature_key)

    def is_disabled(self, capability: str, feature_key: str) -> bool:
        return (
            capability in self.disabled_features
            or feature_key in self.disabled_features
            or f"{capability}:{feature_key}" in self.disabled_features
        )

    @property
    def source(self) -> str:
        return (
            "control-plane catalog="
            f"{self.catalog_version} routing={self.routing_policy_version}"
        )

    @classmethod
    def from_payload(cls, payload: Any) -> "CloudRoutingProfile":
        if not isinstance(payload, dict):
            raise CloudRoutingProfileUnavailable(
                "core-profile response is not a JSON object",
            )
        return cls(
            llm_feature_presets=_string_map(payload.get("llm_feature_presets")),
            image_feature_presets=_string_map(payload.get("image_feature_presets")),
            video_feature_presets=_string_map(payload.get("video_feature_presets")),
            tts_voice_defaults=_string_map(payload.get("tts_voice_defaults")),
            strict_no_fallback=bool(payload.get("strict_no_fallback", False)),
            disabled_features=frozenset(_string_list(payload.get("disabled_features"))),
            catalog_version=_int(payload.get("catalog_version")),
            routing_policy_version=_int(payload.get("routing_policy_version")),
        )


class CloudRoutingProfilePort(Protocol):
    async def get_profile(
        self, *, tenant_id: str, account_id: str, tier: str, user_id: str = ""
    ) -> CloudRoutingProfile:
        """Return the (cached) routing profile for a hosted account/user scope.

        ``user_id`` carries the per-player scope so a user-scope preference route
        actually resolves on the hot path (plan §6); blank means "no user override".
        """


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        if item is None:
            continue
        text = str(item).strip()
        if text:
            result[str(key)] = text
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
