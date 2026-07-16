"""Tests for ``build_voice_presets``.

Covers identity-token derivation (per-character subdir vs top-level
file), weight matching by substring, sidecar TXT pickup, language
detection, and the "incomplete" flag when weights are missing.
"""

from __future__ import annotations

from kokoro_link.application.services.tts_asset_scanner import (
    TTSAsset,
    TTSAssetCatalog,
)
from kokoro_link.application.services.tts_voice_preset import (
    build_voice_presets,
)


def _ref(relative: str, prompt_hint: str | None = None) -> TTSAsset:
    return TTSAsset(
        path=relative,
        relative=relative,
        absolute_path=f"/abs/{relative}",
        prompt_hint=prompt_hint,
    )


def _weight(relative: str) -> TTSAsset:
    return TTSAsset(
        path=relative,
        relative=relative,
        absolute_path=f"/abs/{relative}",
    )


def test_subdir_ref_matches_weight_by_dir_name() -> None:
    catalog = TTSAssetCatalog(
        ref_audios=(_ref("refs/hakua/hakua_ref2.wav"),),
        gpt_weights=(_weight("GPT_weights_v2/hakua_l-e15.ckpt"),),
        sovits_weights=(_weight("SoVITS_weights_v2/hakua_l_e8_s672.pth"),),
    )
    presets = build_voice_presets(catalog)
    assert len(presets) == 1
    p = presets[0]
    assert p.is_complete
    assert p.ref_audio_path == "refs/hakua/hakua_ref2.wav"
    assert p.gpt_weights_path == "GPT_weights_v2/hakua_l-e15.ckpt"
    assert p.sovits_weights_path == "SoVITS_weights_v2/hakua_l_e8_s672.pth"


def test_top_level_ref_strips_ref_suffix() -> None:
    """``refs/kokkoro_ref.wav`` should still match a weight named
    ``...kokkoro...ckpt`` (the ``_ref`` suffix is noise)."""
    catalog = TTSAssetCatalog(
        ref_audios=(_ref("refs/kokkoro_ref.wav"),),
        gpt_weights=(_weight("GPT_weights_v4/kokkoro-e15.ckpt"),),
        sovits_weights=(_weight("SoVITS_weights_v4/kokkoro-e8.pth"),),
    )
    presets = build_voice_presets(catalog)
    assert len(presets) == 1
    assert presets[0].is_complete


def test_each_ref_wav_becomes_its_own_preset() -> None:
    """A character with multiple ref takes (different emotions / clips)
    surfaces as multiple dropdown rows — operator picks which one to
    use; they all share the same weights."""
    catalog = TTSAssetCatalog(
        ref_audios=(
            _ref("refs/hakua/hakua_ref1.wav"),
            _ref("refs/hakua/hakua_ref2.wav"),
            _ref("refs/hakua/hakua_ref3.wav"),
        ),
        gpt_weights=(_weight("GPT_weights_v2/hakua_l-e15.ckpt"),),
        sovits_weights=(_weight("SoVITS_weights_v2/hakua_l_e8_s672.pth"),),
    )
    presets = build_voice_presets(catalog)
    assert len(presets) == 3
    refs = {p.ref_audio_path for p in presets}
    assert refs == {
        "refs/hakua/hakua_ref1.wav",
        "refs/hakua/hakua_ref2.wav",
        "refs/hakua/hakua_ref3.wav",
    }
    assert all(p.is_complete for p in presets)


def test_sidecar_text_and_lang_inferred() -> None:
    catalog = TTSAssetCatalog(
        ref_audios=(_ref(
            "refs/kokkoro/ref.wav",
            prompt_hint="私、故郷から遠く離れて過ごしております。",
        ),),
        gpt_weights=(_weight("GPT_weights_v4/kokkoro-e15.ckpt"),),
        sovits_weights=(_weight("SoVITS_weights_v4/kokkoro-e8.pth"),),
    )
    p = build_voice_presets(catalog)[0]
    assert p.prompt_text == "私、故郷から遠く離れて過ごしております。"
    assert p.prompt_lang == "ja"


def test_chinese_prompt_detected_as_zh() -> None:
    catalog = TTSAssetCatalog(
        ref_audios=(_ref(
            "refs/aiko/ref.wav",
            prompt_hint="你今天看起來不錯",
        ),),
        gpt_weights=(),
        sovits_weights=(),
    )
    p = build_voice_presets(catalog)[0]
    assert p.prompt_lang == "zh"
    assert not p.is_complete  # no weights matched


def test_incomplete_preset_still_returned_with_empty_weights() -> None:
    catalog = TTSAssetCatalog(
        ref_audios=(_ref("refs/orphan/foo.wav"),),
        gpt_weights=(_weight("GPT_weights_v2/different-name.ckpt"),),
        sovits_weights=(),
    )
    presets = build_voice_presets(catalog)
    assert len(presets) == 1
    p = presets[0]
    assert not p.is_complete
    assert p.gpt_weights_path == ""
    assert p.sovits_weights_path == ""


def test_complete_presets_ranked_first() -> None:
    catalog = TTSAssetCatalog(
        ref_audios=(
            _ref("refs/orphan/foo.wav"),
            _ref("refs/hakua/hakua_ref2.wav"),
        ),
        gpt_weights=(_weight("GPT_weights_v2/hakua_l-e15.ckpt"),),
        sovits_weights=(_weight("SoVITS_weights_v2/hakua_l_e8_s672.pth"),),
    )
    presets = build_voice_presets(catalog)
    assert presets[0].is_complete is True   # hakua first
    assert presets[1].is_complete is False  # orphan after


def test_shorter_weight_name_wins_when_substring_ambiguous() -> None:
    """If ``hakua`` matches both ``hakua_l.ckpt`` and ``hakua_extra.ckpt``,
    pick the shorter one — usually the canonical primary fit."""
    catalog = TTSAssetCatalog(
        ref_audios=(_ref("refs/hakua/hakua_ref.wav"),),
        gpt_weights=(
            _weight("GPT_weights_v2/hakua_extra_v2.ckpt"),
            _weight("GPT_weights_v2/hakua_l.ckpt"),
        ),
        sovits_weights=(_weight("SoVITS_weights_v2/hakua_l.pth"),),
    )
    p = build_voice_presets(catalog)[0]
    assert p.gpt_weights_path == "GPT_weights_v2/hakua_l.ckpt"
