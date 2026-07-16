from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kokoro_link.api.app import _log_prompt_pack_overlay_status
from kokoro_link.infrastructure.prompts import reset_default_loader_for_tests


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_startup_log_reports_loaded_prompt_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write(tmp_path / "chat" / "instructions_footer.txt", "tuned footer")
    monkeypatch.setenv("YURALUME_PROMPT_PACK_DIR", str(tmp_path))
    reset_default_loader_for_tests()
    caplog.set_level(logging.INFO, logger="kokoro_link.api.app")

    try:
        _log_prompt_pack_overlay_status()
    finally:
        reset_default_loader_for_tests()

    assert "Prompt pack overlay loaded" in caplog.text
    assert f"path={tmp_path}" in caplog.text
    assert "overlay_templates=1" in caplog.text
    assert "sample=chat/instructions_footer" in caplog.text


def test_startup_log_warns_when_prompt_overlay_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("YURALUME_PROMPT_PACK_DIR", str(tmp_path))
    reset_default_loader_for_tests()
    caplog.set_level(logging.WARNING, logger="kokoro_link.api.app")

    try:
        _log_prompt_pack_overlay_status()
    finally:
        reset_default_loader_for_tests()

    assert "Prompt pack overlay configured but empty" in caplog.text
    assert f"path={tmp_path}" in caplog.text
    assert "overlay_templates=0" in caplog.text
