"""Bundle ref WAVs with their matching GPT/SoVITS weights.

UX problem we solve: even after the dropdowns landed, the operator
still had to pick three things in lockstep — one ref + one GPT
weight + one SoVITS weight — and they're never independent. A voice
"is" the bundle, not three separate axes. This service walks the
asset catalog and clusters files by shared base names so the UI can
show a single "聲音" dropdown.

Heuristic:

1. Each ref WAV becomes one preset row.
2. The preset's identity = the ref's parent directory name (e.g.
   ``hakua`` from ``refs/hakua/hakua_ref2.wav``). For top-level refs
   (``refs/kokkoro_ref.wav``) we fall back to the filename stem.
3. We search GPT/SoVITS weights for one whose **basename contains**
   that identity token (case-insensitive). First match wins; the
   sort order ranks shorter candidates first so e.g. ``hakua_l`` is
   preferred over ``hakua_extra_v2`` for ``hakua``.
4. Sidecar ``.txt`` next to the ref auto-fills ``prompt_text``;
   detected language (kana → ja, hanzi → zh) auto-fills ``prompt_lang``.

Presets without a weight match still get returned (with empty
weight fields) so the operator can manually wire them in the
advanced section.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from kokoro_link.application.services.tts_asset_scanner import (
    TTSAsset,
    TTSAssetCatalog,
)


@dataclass(frozen=True, slots=True)
class TTSVoicePreset:
    """One ready-to-use voice config (UI dropdown row).

    All paths are relative to the install root — same convention as
    :class:`TTSAsset.path`. ``label`` is the human-readable string the
    UI shows in the dropdown; ``id`` is the stable token used for
    cache / bookkeeping (matches across reruns of the scanner).
    """

    id: str
    label: str
    ref_audio_path: str
    prompt_text: str
    prompt_lang: str
    gpt_weights_path: str
    sovits_weights_path: str

    @property
    def is_complete(self) -> bool:
        """A preset is "complete" when both weights matched. The UI
        can show a dimmed marker for incomplete ones so the operator
        knows they still need to pick weights manually."""
        return bool(self.gpt_weights_path and self.sovits_weights_path)


def build_voice_presets(catalog: TTSAssetCatalog) -> tuple[TTSVoicePreset, ...]:
    """Cluster ``catalog`` into voice presets keyed off ref filenames."""
    presets: list[TTSVoicePreset] = []
    for ref in catalog.ref_audios:
        token = _identity_token(ref)
        gpt = _best_match(token, catalog.gpt_weights)
        sovits = _best_match(token, catalog.sovits_weights)
        prompt_text = ref.prompt_hint or ""
        prompt_lang = _detect_lang(prompt_text)
        label = _build_label(ref, token, complete=bool(gpt and sovits))
        presets.append(TTSVoicePreset(
            id=ref.relative,
            label=label,
            ref_audio_path=ref.path,
            prompt_text=prompt_text,
            prompt_lang=prompt_lang,
            gpt_weights_path=gpt or "",
            sovits_weights_path=sovits or "",
        ))
    # Complete presets first (likely user intent), incomplete after.
    presets.sort(key=lambda p: (not p.is_complete, p.label.lower()))
    return tuple(presets)


# ---------------------------------------------------------------- helpers

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]+")


def _identity_token(ref: TTSAsset) -> str:
    """Return the token used to find weights for this ref.

    For ``refs/<dir>/<file>.wav`` we use ``<dir>`` because operators
    typically organise per-character folders; ``<file>`` often has
    suffixes like ``_ref2`` or ``_sample1`` that don't appear in the
    weights' filenames.

    For top-level refs (``refs/<file>.wav``) we use ``<file>``'s stem
    minus a trailing ``_ref`` etc. token so ``kokkoro_ref`` still
    finds ``kokkoro`` in the weights.
    """
    parts = ref.relative.split("/")
    if len(parts) >= 3:
        # refs/<dir>/<file>
        return parts[1]
    # refs/<file>
    stem = parts[-1].rsplit(".", 1)[0]
    stem = re.sub(r"_(ref\d*|sample\d*)$", "", stem, flags=re.IGNORECASE)
    return stem


def _best_match(token: str, weights: tuple[TTSAsset, ...]) -> str | None:
    """Find the weight asset whose filename contains ``token``."""
    if not token:
        return None
    needle = token.lower()
    candidates = []
    for asset in weights:
        name = asset.relative.rsplit("/", 1)[-1].lower()
        if needle in name:
            # Shorter names tend to be the cleanest fit (``hakua_l``
            # beats ``hakua_unstable_test``); use length as a stable tie-break.
            candidates.append((len(name), asset.path))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


_KANA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF]")
_HAN_RE = re.compile(r"[\u4E00-\u9FFF]")


def _detect_lang(text: str) -> str:
    """Best-effort prompt language inference from sidecar text.

    Empty text → empty (UI / global will pick a default). Kana wins
    over han so a Japanese sentence with kanji classifies as ``ja``."""
    if not text:
        return ""
    if _KANA_RE.search(text):
        return "ja"
    if _HAN_RE.search(text):
        return "zh"
    if re.fullmatch(r"[\x00-\x7F]+", text):
        return "en"
    return ""


def _build_label(ref: TTSAsset, token: str, *, complete: bool) -> str:
    """Compose the dropdown label.

    Rules: show the ref's relative-to-install path, prefix with the
    identity token when the ref filename doesn't already contain it,
    and append a (no weights) note for incomplete presets so the
    operator notices something's missing without expanding the
    advanced section."""
    label = ref.relative
    suffix = "" if complete else "  ⚠ 缺權重"
    return f"{label}{suffix}"
