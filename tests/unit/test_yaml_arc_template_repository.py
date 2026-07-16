"""YAML pack loader — schema, robustness, caching.

Migrated from the old read/write repo tests: YAML is no longer the
runtime store, only a startup source for pack rows. Tests that used
to exercise the save path now live in
``test_sa_arc_template_repository.py`` (DB) and
``test_arc_template_intake_service.py`` (in-memory).
"""

from __future__ import annotations

from pathlib import Path

from kokoro_link.infrastructure.story.yaml_arc_template_repository import (
    YAMLArcTemplatePackLoader,
)


_VALID_TEMPLATE_YAML = """
id: cafe_idol_audition
title: 三週的試鏡
premise: 她報名了一場從沒想過會報的試鏡。
theme: ambition
duration_days: 14
binding:
  world_frames: [modern, school]
  required_traits: []
beats:
  - sequence: 0
    day_offset: 0
    title: 公告張貼
    summary: 週一早上的公告欄。
    tension: setup
    scene_type: encounter
    location: 學校公告欄
    scene_characters: []
    dramatic_question: 她敢報名嗎？
    required: true
  - sequence: 1
    day_offset: 5
    title: 第一次撞牆
    summary: 鏡子裡的自己呼吸不穩。
    tension: rising
    scene_type: conflict
    location: 音樂教室
    scene_characters: [指導老師]
    dramatic_question: 她要承認嗎？
    required: true
"""


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _by_id(entries, template_id):
    for entry in entries:
        if entry.template.id == template_id:
            return entry
    return None


def test_loads_valid_template(tmp_path: Path) -> None:
    _write(tmp_path / "cafe_idol_audition.yaml", _VALID_TEMPLATE_YAML)
    loader = YAMLArcTemplatePackLoader(directories=[tmp_path])

    entries = loader.load_all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.pack_id == "cafe_idol_audition"
    # external_id is None when the YAML's declared id matches the file
    # stem — the loader treats that as "no override".
    assert entry.external_id is None
    tpl = entry.template
    assert tpl.title == "三週的試鏡"
    assert tpl.theme == "ambition"
    assert tpl.duration_days == 14
    assert tpl.beat_count == 2
    assert tpl.binding.world_frames == ("modern", "school")
    # Beats sort by (day_offset, sequence) on the entity side.
    assert [b.title for b in tpl.beats] == ["公告張貼", "第一次撞牆"]


def test_id_falls_back_to_filename_stem(tmp_path: Path) -> None:
    yaml_no_id = _VALID_TEMPLATE_YAML.replace("id: cafe_idol_audition\n", "")
    _write(tmp_path / "summer_festival.yaml", yaml_no_id)
    loader = YAMLArcTemplatePackLoader(directories=[tmp_path])

    entries = loader.load_all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.template.id == "summer_festival"
    assert entry.pack_id == "summer_festival"
    assert entry.external_id is None


def test_external_id_captured_when_yaml_overrides_filename(
    tmp_path: Path,
) -> None:
    """Authors who give the YAML a different ``id:`` than the filename
    get their declared id captured in ``external_id`` for provenance."""
    yaml_override = _VALID_TEMPLATE_YAML.replace(
        "id: cafe_idol_audition", "id: cafe_idol_audition_v2",
    )
    _write(tmp_path / "cafe_idol_audition.yaml", yaml_override)
    loader = YAMLArcTemplatePackLoader(directories=[tmp_path])

    entries = loader.load_all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.template.id == "cafe_idol_audition_v2"
    assert entry.pack_id == "cafe_idol_audition"
    assert entry.external_id == "cafe_idol_audition_v2"


def test_list_returns_sorted_by_id(tmp_path: Path) -> None:
    _write(tmp_path / "z_template.yaml", _VALID_TEMPLATE_YAML.replace(
        "id: cafe_idol_audition", "id: z_template",
    ))
    _write(tmp_path / "a_template.yaml", _VALID_TEMPLATE_YAML.replace(
        "id: cafe_idol_audition", "id: a_template",
    ))
    loader = YAMLArcTemplatePackLoader(directories=[tmp_path])

    entries = loader.load_all()
    assert [e.template.id for e in entries] == ["a_template", "z_template"]


def test_bad_yaml_skipped_gracefully(tmp_path: Path) -> None:
    _write(tmp_path / "good.yaml", _VALID_TEMPLATE_YAML.replace(
        "id: cafe_idol_audition", "id: good",
    ))
    _write(tmp_path / "bad.yaml", "this is: : not valid yaml: [unclosed")
    loader = YAMLArcTemplatePackLoader(directories=[tmp_path])

    entries = loader.load_all()
    # Good template loads; bad one is silently skipped (log, no crash).
    assert [e.template.id for e in entries] == ["good"]


def test_missing_beats_key_skipped(tmp_path: Path) -> None:
    _write(tmp_path / "broken.yaml", """
id: broken
title: 標題
premise: 沒有 beats 的範本應被跳過。
duration_days: 7
""")
    _write(tmp_path / "ok.yaml", _VALID_TEMPLATE_YAML.replace(
        "id: cafe_idol_audition", "id: ok",
    ))
    loader = YAMLArcTemplatePackLoader(directories=[tmp_path])

    entries = loader.load_all()
    assert [e.template.id for e in entries] == ["ok"]


def test_id_collision_keeps_first(tmp_path: Path) -> None:
    _write(tmp_path / "a.yaml", _VALID_TEMPLATE_YAML.replace(
        "id: cafe_idol_audition", "id: dup_template",
    ))
    _write(tmp_path / "b.yaml", _VALID_TEMPLATE_YAML.replace(
        "id: cafe_idol_audition", "id: dup_template",
    ).replace("title: 三週的試鏡", "title: 應被忽略"))
    loader = YAMLArcTemplatePackLoader(directories=[tmp_path])

    entries = loader.load_all()
    assert len(entries) == 1
    # Alphabetical filename order means "a.yaml" wins.
    assert entries[0].template.title == "三週的試鏡"


def test_required_string_coerced(tmp_path: Path) -> None:
    yaml_with_string_bool = _VALID_TEMPLATE_YAML.replace(
        "    required: true", "    required: 'no'",
    )
    _write(tmp_path / "x.yaml", yaml_with_string_bool)
    loader = YAMLArcTemplatePackLoader(directories=[tmp_path])

    entry = _by_id(loader.load_all(), "cafe_idol_audition")
    assert entry is not None
    # First beat has the literal "no" string in our replacement; the
    # second was already true.
    assert entry.template.beats[0].required is False


def test_scene_characters_accepts_comma_string(tmp_path: Path) -> None:
    yaml_with_string_list = _VALID_TEMPLATE_YAML.replace(
        "    scene_characters: [指導老師]",
        "    scene_characters: '指導老師, 同學A'",
    )
    _write(tmp_path / "x.yaml", yaml_with_string_list)
    loader = YAMLArcTemplatePackLoader(directories=[tmp_path])

    entry = _by_id(loader.load_all(), "cafe_idol_audition")
    assert entry is not None
    assert entry.template.beats[1].scene_characters == ("指導老師", "同學A")


def test_cache_is_lazy_and_reloadable(tmp_path: Path) -> None:
    loader = YAMLArcTemplatePackLoader(directories=[tmp_path])
    # Empty directory → empty cache, no error.
    assert loader.load_all() == []

    # Add a file, then call reload to invalidate.
    _write(tmp_path / "late.yaml", _VALID_TEMPLATE_YAML.replace(
        "id: cafe_idol_audition", "id: late",
    ))
    loader.reload()
    entries = loader.load_all()
    assert [e.template.id for e in entries] == ["late"]
