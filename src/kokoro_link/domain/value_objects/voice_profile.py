"""Per-character TTS configuration.

A ``VoiceProfile`` overrides the global ``TTSSettings`` for one
character. Empty string fields fall back to the global default at
synthesis time, so a partial profile (e.g. only ``prompt_text`` set)
still works — the rest comes from env. ``enabled=False`` greys out the
play button for that character without erasing the saved values.

``voice_id`` is the deployment-facing selector returned by the TTS
capability service. Legacy GPT-SoVITS path fields remain for stored-row
compatibility, but new UI and adapters should not expose or require them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    enabled: bool = True
    voice_id: str = ""
    """Stable id from the TTS capability service. Empty = inherit global."""
    ref_audio_path: str = ""
    """Server-side path of the reference WAV (3–10 sec). Empty =
    inherit global ``KOKORO_TTS_REF_AUDIO_PATH``."""
    prompt_text: str = ""
    """Verbatim transcript of ``ref_audio_path``. Empty = inherit
    global."""
    prompt_lang: str = ""
    """Language of the ref audio (``zh`` / ``ja`` / ``en``...). Empty
    = inherit global."""
    translate_target_lang: str = ""
    """Pre-TTS LLM dubbing target. Empty = inherit global. Set to a
    sentinel ``"-"`` to **disable** translation for this character
    even when the global default has one (rare, but covered)."""
    gpt_weights_path: str = ""
    """Path of the GPT-SoVITS GPT model (``GPT_weights_v4/...ckpt``).
    Relative to the GPT-SoVITS install. Empty = use whatever's
    currently loaded on the server."""
    sovits_weights_path: str = ""
    """Path of the SoVITS-G model (``SoVITS_weights_v4/...pth``)."""

    @property
    def is_empty(self) -> bool:
        """True when nothing is set — equivalent to "no per-character
        override", same effect as ``Character.voice_profile is None``.
        Used by the persistence mapping to avoid storing empty rows."""
        return (
            not self.voice_id
            and not self.ref_audio_path
            and not self.prompt_text
            and not self.prompt_lang
            and not self.translate_target_lang
            and not self.gpt_weights_path
            and not self.sovits_weights_path
            and self.enabled is True
        )

    def with_overrides(self, **kwargs) -> "VoiceProfile":
        return replace(self, **kwargs)

    @classmethod
    def from_payload(cls, data: dict | None) -> "VoiceProfile | None":
        """Parse from a user-supplied dict (API / DB JSON).

        ``None`` / empty / blank-only payloads return ``None`` so the
        downstream code can ``if profile is None`` cleanly."""
        if not data:
            return None
        profile = cls(
            enabled=bool(data.get("enabled", True)),
            voice_id=str(data.get("voice_id", "") or "").strip(),
            ref_audio_path=str(data.get("ref_audio_path", "") or "").strip(),
            prompt_text=str(data.get("prompt_text", "") or "").strip(),
            prompt_lang=str(data.get("prompt_lang", "") or "").strip(),
            translate_target_lang=str(
                data.get("translate_target_lang", "") or "",
            ).strip(),
            gpt_weights_path=str(
                data.get("gpt_weights_path", "") or "",
            ).strip(),
            sovits_weights_path=str(
                data.get("sovits_weights_path", "") or "",
            ).strip(),
        )
        return None if profile.is_empty else profile

    def to_payload(self) -> dict:
        return {
            "enabled": self.enabled,
            "voice_id": self.voice_id,
            "ref_audio_path": self.ref_audio_path,
            "prompt_text": self.prompt_text,
            "prompt_lang": self.prompt_lang,
            "translate_target_lang": self.translate_target_lang,
            "gpt_weights_path": self.gpt_weights_path,
            "sovits_weights_path": self.sovits_weights_path,
        }
