"""Scan a GPT-SoVITS install root for usable assets.

Powers the dropdown UX in ``VoiceProfilePanel`` — operators pick a
ref WAV / GPT model / SoVITS model from a generated list instead of
typing absolute paths. The scanner is read-only, deliberately
filesystem-shaped (no side effects, no DB), so it can run on every
panel open without coordinating cache invalidation.

Layout convention (loose, fits the upstream Windows package):

  <install>/
    GPT_weights_v?/*.ckpt        → gpt_weights
    SoVITS_weights_v?/*.pth      → sovits_weights
    refs/**/*.wav                → ref_audios (recursive)
    SoVITS_weights_v?/refs/**/*.wav   → also ref_audios (some users
                                       drop refs alongside the weight
                                       folder for a given character)

Sidecar TXT support: if ``X.wav`` has a sibling ``X.txt`` we read it
as the suggested ``prompt_text``. UI shows it as a one-click
auto-fill so a fresh ref doesn't require re-typing the transcript.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_MAX_FILES_PER_CATEGORY = 200
"""Cap so a misconfigured ``install_dir`` (pointing at C:\\) doesn't
walk the whole filesystem and DoS the panel."""

_REF_DIR_NAMES = ("refs", "reference", "ref_audio")


@dataclass(frozen=True, slots=True)
class TTSAsset:
    """One scan result.

    ``path`` is the canonical form sent to GPT-SoVITS — **relative to
    the install root**. This is portable across host / docker setups:
    when the TTS server runs in a container at ``/app`` and the host
    sees the same mount at ``./tts``, the path ``refs/foo.wav``
    resolves correctly on both sides without translation.
    ``absolute_path`` is kept around for diagnostics / logs.
    ``relative`` equals ``path`` (kept for dropdown labelling clarity).
    ``prompt_hint`` carries the sidecar TXT content for ref WAVs only;
    ``None`` for weight files or when no sidecar exists.
    """

    path: str
    relative: str
    absolute_path: str
    prompt_hint: str | None = None


@dataclass(frozen=True, slots=True)
class TTSAssetCatalog:
    ref_audios: tuple[TTSAsset, ...]
    gpt_weights: tuple[TTSAsset, ...]
    sovits_weights: tuple[TTSAsset, ...]


class TTSAssetScanner:
    def __init__(self, *, install_dir: str) -> None:
        self._root = Path(install_dir) if install_dir else None

    @property
    def enabled(self) -> bool:
        return self._root is not None and self._root.is_dir()

    def scan(self) -> TTSAssetCatalog:
        if self._root is None:
            return _empty_catalog()
        try:
            root = self._root.resolve(strict=True)
        except (OSError, RuntimeError):
            _LOGGER.warning(
                "tts asset scanner: install_dir %s does not exist",
                self._root,
            )
            return _empty_catalog()

        ref_audios = _collect_refs(root)
        gpt_weights = _collect_weights(root, prefix="GPT_weights", suffix=".ckpt")
        sovits_weights = _collect_weights(
            root, prefix="SoVITS_weights", suffix=".pth",
        )
        return TTSAssetCatalog(
            ref_audios=tuple(ref_audios),
            gpt_weights=tuple(gpt_weights),
            sovits_weights=tuple(sovits_weights),
        )


# ----------------------------------------------------------------------


def _empty_catalog() -> TTSAssetCatalog:
    return TTSAssetCatalog(ref_audios=(), gpt_weights=(), sovits_weights=())


def _collect_refs(root: Path) -> list[TTSAsset]:
    seen: set[Path] = set()
    out: list[TTSAsset] = []

    # Top-level refs/* and any SoVITS_weights_*/refs/*. Bounded glob
    # patterns rather than `rglob('*.wav')` so we don't walk
    # weights / training data trees on accident (multi-GB scans).
    candidates: list[Path] = []
    for ref_name in _REF_DIR_NAMES:
        top = root / ref_name
        if top.is_dir():
            candidates.extend(_walk_wavs(top))
    for sub in root.iterdir() if root.is_dir() else []:
        if sub.is_dir() and sub.name.lower().startswith("sovits_weights"):
            for ref_name in _REF_DIR_NAMES:
                inner = sub / ref_name
                if inner.is_dir():
                    candidates.extend(_walk_wavs(inner))
        if sub.is_dir() and sub.name.lower().startswith("gpt_weights"):
            for ref_name in _REF_DIR_NAMES:
                inner = sub / ref_name
                if inner.is_dir():
                    candidates.extend(_walk_wavs(inner))

    for wav in candidates:
        if wav in seen:
            continue
        seen.add(wav)
        try:
            relative = wav.relative_to(root).as_posix()
        except ValueError:
            relative = wav.name
        out.append(TTSAsset(
            path=relative,
            relative=relative,
            absolute_path=str(wav),
            prompt_hint=_read_sidecar_text(wav),
        ))
        if len(out) >= _MAX_FILES_PER_CATEGORY:
            break
    out.sort(key=lambda a: a.relative.lower())
    return out


def _walk_wavs(directory: Path) -> list[Path]:
    out: list[Path] = []
    try:
        for entry in directory.rglob("*.wav"):
            if entry.is_file():
                out.append(entry)
            if len(out) >= _MAX_FILES_PER_CATEGORY:
                break
    except OSError:
        _LOGGER.exception("tts asset scanner: walk failed at %s", directory)
    return out


def _collect_weights(
    root: Path, *, prefix: str, suffix: str,
) -> list[TTSAsset]:
    out: list[TTSAsset] = []
    if not root.is_dir():
        return out
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        if not sub.name.lower().startswith(prefix.lower()):
            continue
        try:
            for entry in sub.iterdir():
                if entry.is_file() and entry.suffix.lower() == suffix.lower():
                    try:
                        relative = entry.relative_to(root).as_posix()
                    except ValueError:
                        relative = entry.name
                    out.append(TTSAsset(
                        path=relative,
                        relative=relative,
                        absolute_path=str(entry),
                    ))
                if len(out) >= _MAX_FILES_PER_CATEGORY:
                    break
        except OSError:
            _LOGGER.exception(
                "tts asset scanner: list failed at %s", sub,
            )
        if len(out) >= _MAX_FILES_PER_CATEGORY:
            break
    out.sort(key=lambda a: a.relative.lower())
    return out


def _read_sidecar_text(wav: Path) -> str | None:
    """Return the contents of ``<wav>.txt`` (sibling) when present.

    The text file's stem matches the WAV's stem; we trim outer
    whitespace and bail when empty / oversized. Used by the UI to
    auto-fill ``prompt_text`` when the operator picks a ref."""
    sidecar = wav.with_suffix(".txt")
    if not sidecar.is_file():
        return None
    try:
        raw = sidecar.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    body = raw.strip()
    if not body or len(body) > 4000:
        return None
    return body
