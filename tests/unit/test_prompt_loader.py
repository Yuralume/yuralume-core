"""Unit tests for the external prompt template loader.

The loader is intentionally minimal — these tests pin the contract so
future migrations of hard-coded prompts can rely on stable behaviour
(template not found / variable missing / override directory / single
trailing newline collapse).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.bootstrap.settings import HumanizationSettings, PromptQualitySettings
from kokoro_link.infrastructure.prompt.default import (
    DefaultPromptContextBuilder,
    prompt_pack_hash_snapshot,
)
from kokoro_link.infrastructure.prompts import (
    PromptLoader,
    PromptTemplateNotFoundError,
    PromptVariableMissingError,
    reset_default_loader_for_tests,
    get_default_loader,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_render_substitutes_variables(tmp_path: Path) -> None:
    _write(tmp_path / "greet.txt", "你好 ${name}，今天 ${weather}。\n")
    loader = PromptLoader(package_dir=tmp_path)

    assert loader.render("greet", name="小櫻", weather="晴朗") == "你好 小櫻，今天 晴朗。"


def test_render_strips_single_trailing_newline(tmp_path: Path) -> None:
    # POSIX file convention: files end with newline. The render API
    # strips exactly one so callers can splice cleanly.
    _write(tmp_path / "a.txt", "line1\nline2\n")
    loader = PromptLoader(package_dir=tmp_path)
    assert loader.render("a") == "line1\nline2"


def test_render_preserves_internal_blank_lines(tmp_path: Path) -> None:
    _write(tmp_path / "b.txt", "head\n\nbody\n")
    loader = PromptLoader(package_dir=tmp_path)
    assert loader.render("b") == "head\n\nbody"


def test_render_lines_uses_splitlines(tmp_path: Path) -> None:
    _write(tmp_path / "c.txt", "alpha\nbeta\ngamma\n")
    loader = PromptLoader(package_dir=tmp_path)
    assert loader.render_lines("c") == ["alpha", "beta", "gamma"]


def test_missing_variable_raises(tmp_path: Path) -> None:
    _write(tmp_path / "v.txt", "hi ${missing}")
    loader = PromptLoader(package_dir=tmp_path)
    with pytest.raises(PromptVariableMissingError):
        loader.render("v")


def test_template_not_found_raises(tmp_path: Path) -> None:
    loader = PromptLoader(package_dir=tmp_path)
    with pytest.raises(PromptTemplateNotFoundError):
        loader.render("nope")


def test_traversal_attempts_rejected(tmp_path: Path) -> None:
    loader = PromptLoader(package_dir=tmp_path)
    for bad in ("", "../escape", "a/../../etc/passwd", "/absolute"):
        with pytest.raises(PromptTemplateNotFoundError):
            loader.render(bad)


def test_override_dir_takes_precedence(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    override = tmp_path / "override"
    _write(pkg / "msg.txt", "from package")
    _write(override / "msg.txt", "from override")
    loader = PromptLoader(package_dir=pkg, override_dir=override)
    assert loader.render("msg") == "from override"


def test_override_dir_falls_back_to_package(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    override = tmp_path / "override"
    _write(pkg / "only_pkg.txt", "package wins")
    override.mkdir()  # empty override dir
    loader = PromptLoader(package_dir=pkg, override_dir=override)
    assert loader.render("only_pkg") == "package wins"


def test_caching_reads_file_once(tmp_path: Path) -> None:
    target = tmp_path / "cached.txt"
    _write(target, "original")
    loader = PromptLoader(package_dir=tmp_path)
    assert loader.render("cached") == "original"
    # Mutate file after first read — cache should hold the original.
    target.write_text("changed", encoding="utf-8")
    assert loader.render("cached") == "original"
    loader.clear_cache()
    assert loader.render("cached") == "changed"


def test_nested_path_components(tmp_path: Path) -> None:
    _write(tmp_path / "shared" / "deep" / "p.txt", "deep prompt")
    loader = PromptLoader(package_dir=tmp_path)
    assert loader.render("shared/deep/p") == "deep prompt"


def test_raw_returns_unrendered(tmp_path: Path) -> None:
    _write(tmp_path / "r.txt", "hi ${name}\n")
    loader = PromptLoader(package_dir=tmp_path)
    assert loader.raw("r") == "hi ${name}\n"


def test_exists_distinguishes_present_and_absent(tmp_path: Path) -> None:
    _write(tmp_path / "yes.txt", "x")
    loader = PromptLoader(package_dir=tmp_path)
    assert loader.exists("yes") is True
    assert loader.exists("nope") is False


def test_prompt_pack_hash_changes_when_template_content_changes(tmp_path: Path) -> None:
    target = tmp_path / "chat" / "instructions_footer.txt"
    _write(target, "baseline footer")
    loader = PromptLoader(package_dir=tmp_path)

    original = loader.prompt_pack_hash()
    target.write_text("baseline footer.", encoding="utf-8")

    assert loader.prompt_pack_hash() != original


def test_prompt_pack_hash_normalizes_crlf(tmp_path: Path) -> None:
    lf_dir = tmp_path / "lf"
    crlf_dir = tmp_path / "crlf"
    _write(lf_dir / "chat" / "instructions_footer.txt", "line 1\nline 2\n")
    crlf_target = crlf_dir / "chat" / "instructions_footer.txt"
    crlf_target.parent.mkdir(parents=True, exist_ok=True)
    crlf_target.write_bytes(b"line 1\r\nline 2\r\n")

    assert (
        PromptLoader(package_dir=lf_dir).prompt_pack_hash()
        == PromptLoader(package_dir=crlf_dir).prompt_pack_hash()
    )


def test_prompt_pack_hash_includes_overlay_effective_content(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    overlay_dir = tmp_path / "overlay"
    _write(package_dir / "chat" / "instructions_footer.txt", "baseline footer")
    _write(overlay_dir / "chat" / "instructions_footer.txt", "tuned footer")

    baseline = PromptLoader(package_dir=package_dir).prompt_pack_hash()
    tuned = PromptLoader(
        package_dir=package_dir,
        override_dir=overlay_dir,
    ).prompt_pack_hash()

    assert tuned != baseline


def test_overlay_status_reports_disabled_overlay(tmp_path: Path) -> None:
    _write(tmp_path / "chat" / "instructions_footer.txt", "baseline footer")
    status = PromptLoader(package_dir=tmp_path).overlay_status()

    assert status.configured is False
    assert status.overlay_template_count == 0
    assert status.effective_template_count == 1


def test_overlay_status_reports_empty_configured_overlay(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    overlay_dir = tmp_path / "overlay"
    _write(package_dir / "chat" / "instructions_footer.txt", "baseline footer")
    overlay_dir.mkdir()

    status = PromptLoader(
        package_dir=package_dir,
        override_dir=overlay_dir,
    ).overlay_status()

    assert status.configured is True
    assert status.exists is True
    assert status.is_dir is True
    assert status.overlay_template_count == 0
    assert status.effective_template_count == 1


def test_overlay_status_reports_loaded_overlay_files(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    overlay_dir = tmp_path / "overlay"
    _write(package_dir / "chat" / "instructions_footer.txt", "baseline footer")
    _write(overlay_dir / "chat" / "instructions_footer.txt", "tuned footer")
    _write(overlay_dir / "busy" / "follow_up_composer.txt", "busy tuned")

    status = PromptLoader(
        package_dir=package_dir,
        override_dir=overlay_dir,
    ).overlay_status()

    assert status.configured is True
    assert status.exists is True
    assert status.is_dir is True
    assert status.overlay_template_count == 2
    assert status.effective_template_count == 2
    assert status.sample_templates == (
        "busy/follow_up_composer",
        "chat/instructions_footer",
    )


def test_prompt_pack_hash_includes_humanization_snapshot(tmp_path: Path) -> None:
    _write(tmp_path / "chat" / "instructions_footer.txt", "baseline footer")
    loader = PromptLoader(package_dir=tmp_path)

    enabled = loader.prompt_pack_hash(
        {"humanization": {"body_state_enabled": True}},
    )
    disabled = loader.prompt_pack_hash(
        {"humanization": {"body_state_enabled": False}},
    )

    assert enabled != disabled


def test_prompt_pack_hash_includes_prompt_quality_snapshot(tmp_path: Path) -> None:
    _write(tmp_path / "chat" / "instructions_footer.txt", "baseline footer")
    loader = PromptLoader(package_dir=tmp_path)

    digest_off = loader.prompt_pack_hash(
        prompt_pack_hash_snapshot(
            HumanizationSettings(),
            PromptQualitySettings(material_digest_enabled=False),
        ),
    )
    digest_on = loader.prompt_pack_hash(
        prompt_pack_hash_snapshot(
            HumanizationSettings(),
            PromptQualitySettings(material_digest_enabled=True),
        ),
    )
    gate_retry_two = loader.prompt_pack_hash(
        prompt_pack_hash_snapshot(
            HumanizationSettings(),
            PromptQualitySettings(
                novelty_gate_enabled=True,
                novelty_gate_max_retries=2,
            ),
        ),
    )

    assert digest_off != digest_on
    assert digest_on != gate_retry_two


def test_prompt_pack_hash_snapshot_matches_default_builder() -> None:
    settings = HumanizationSettings(body_state_enabled=False)
    prompt_quality = PromptQualitySettings(novelty_gate_enabled=True)
    builder = DefaultPromptContextBuilder(
        humanization_settings=settings,
        prompt_quality_settings=prompt_quality,
    )

    assert builder.last_prompt_pack_hash == get_default_loader().prompt_pack_hash(
        prompt_pack_hash_snapshot(settings, prompt_quality),
    )


def test_default_loader_honors_prompt_pack_overlay_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path / "chat" / "instructions_footer.txt", "overlay footer")
    monkeypatch.setenv("YURALUME_PROMPT_PACK_DIR", str(tmp_path))
    reset_default_loader_for_tests()

    try:
        assert get_default_loader().render("chat/instructions_footer") == "overlay footer"
    finally:
        reset_default_loader_for_tests()


def test_shipped_role_boundary_template_loads() -> None:
    """The package-shipped role_boundary template must exist + render —
    this is the migration baseline; if it breaks, downstream callers
    (chat, proactive, feed, schedule) all lose their boundary rail."""
    from kokoro_link.infrastructure.prompts import get_default_loader

    loader = get_default_loader()
    rendered = loader.render("shared/role_boundary")
    assert "認知範圍與誠實表達" in rendered
    assert rendered.count("\n- ") >= 4  # bullet list


def test_dollar_sign_must_be_escaped_with_double_dollar(tmp_path: Path) -> None:
    # ``string.Template`` treats $ as escape; literal $ in templates
    # must be written as $$ — pin the behaviour so the migration guide
    # can reference it.
    _write(tmp_path / "money.txt", "cost: $$10")
    loader = PromptLoader(package_dir=tmp_path)
    assert loader.render("money") == "cost: $10"
