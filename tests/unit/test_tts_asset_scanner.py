"""Tests for ``TTSAssetScanner``.

Covers the layout discovery (top-level refs/, weight-folder
sub-refs/), recursion bounds, sidecar TXT pickup, and graceful
handling of a missing/misconfigured install dir.
"""

from __future__ import annotations

from pathlib import Path

from kokoro_link.application.services.tts_asset_scanner import (
    TTSAssetScanner,
)


def _seed_install(tmp_path: Path) -> Path:
    root = tmp_path / "GPT-SoVITS"
    (root / "GPT_weights_v4").mkdir(parents=True)
    (root / "SoVITS_weights_v4").mkdir(parents=True)
    (root / "SoVITS_weights_v2" / "refs" / "hakua").mkdir(parents=True)
    (root / "refs").mkdir(parents=True)
    return root


def test_disabled_when_dir_missing(tmp_path: Path) -> None:
    scanner = TTSAssetScanner(install_dir=str(tmp_path / "nope"))
    assert scanner.enabled is False
    cat = scanner.scan()
    assert cat.ref_audios == ()
    assert cat.gpt_weights == ()


def test_disabled_when_empty_string() -> None:
    scanner = TTSAssetScanner(install_dir="")
    assert scanner.enabled is False
    assert scanner.scan().ref_audios == ()


def test_collects_refs_from_top_and_weight_subdirs(tmp_path: Path) -> None:
    root = _seed_install(tmp_path)
    (root / "refs" / "kokkoro_ref.wav").write_bytes(b"WAV")
    (root / "SoVITS_weights_v2" / "refs" / "hakua" / "hakua_ref2.wav").write_bytes(b"WAV")

    catalog = TTSAssetScanner(install_dir=str(root)).scan()
    relatives = sorted(a.relative for a in catalog.ref_audios)
    assert "refs/kokkoro_ref.wav" in relatives
    assert "SoVITS_weights_v2/refs/hakua/hakua_ref2.wav" in relatives


def test_collects_weights(tmp_path: Path) -> None:
    root = _seed_install(tmp_path)
    (root / "GPT_weights_v4" / "kokkoro-e15.ckpt").write_bytes(b"M")
    (root / "SoVITS_weights_v4" / "kokkoro-e8.pth").write_bytes(b"M")

    catalog = TTSAssetScanner(install_dir=str(root)).scan()
    assert any(a.relative.endswith("kokkoro-e15.ckpt") for a in catalog.gpt_weights)
    assert any(a.relative.endswith("kokkoro-e8.pth") for a in catalog.sovits_weights)


def test_sidecar_txt_loaded_as_prompt_hint(tmp_path: Path) -> None:
    root = _seed_install(tmp_path)
    wav = root / "refs" / "kokkoro_ref.wav"
    wav.write_bytes(b"WAV")
    wav.with_suffix(".txt").write_text(
        "私、故郷から遠く離れて過ごしております。", encoding="utf-8",
    )

    catalog = TTSAssetScanner(install_dir=str(root)).scan()
    target = next(
        a for a in catalog.ref_audios if a.relative == "refs/kokkoro_ref.wav"
    )
    assert target.prompt_hint == "私、故郷から遠く離れて過ごしております。"


def test_no_sidecar_means_no_hint(tmp_path: Path) -> None:
    root = _seed_install(tmp_path)
    (root / "refs" / "wahaha.wav").write_bytes(b"WAV")
    catalog = TTSAssetScanner(install_dir=str(root)).scan()
    assert catalog.ref_audios[0].prompt_hint is None


def test_non_wav_files_ignored_in_refs(tmp_path: Path) -> None:
    root = _seed_install(tmp_path)
    (root / "refs" / "kokkoro_ref.wav").write_bytes(b"WAV")
    (root / "refs" / "notes.txt").write_text("…", encoding="utf-8")
    (root / "refs" / "diagram.png").write_bytes(b"PNG")

    catalog = TTSAssetScanner(install_dir=str(root)).scan()
    assert len(catalog.ref_audios) == 1
    assert catalog.ref_audios[0].relative.endswith(".wav")
