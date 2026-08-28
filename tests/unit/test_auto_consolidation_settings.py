"""Auto-consolidation 的預設值不得由靜態 provider id 決定。

真實 LLM provider 是 DB-backed runtime settings，由 ``runtime_sync`` 在
container 建好*之後*註冊進 mutable registry；所以 DB-only self-host 在
settings 載入當下的 ``KOKORO_DEFAULT_PROVIDER_ID`` 仍是 "fake" 佔位值。
舊的 ``default_provider_id != "fake"`` 預設會在正是這種部署上無聲關掉
整條記憶 decay＋固化管線——與 2026-08-27 品質閘修復（見
``container.py`` novelty_gate 一帶的註解）同一個「bootstrap 靜態判斷
vs DB 動態路由」bug 家族。

「這一刀 merge 能不能真的跑」由 ``LLMMemoryConsolidator.merge`` 逐次呼叫
``ModelResolver.is_fake`` 回答：真 fake 路由短路回 ``None``、cluster 原封
不動，管線落到 decay-only——那是 ``_build_memory_consolidator`` docstring
明文的設計 fallback，不是這裡該預先替它做的決定。
"""

import pytest

from kokoro_link.bootstrap.settings import (
    AutoConsolidationSettings,
    _load_auto_consolidation_settings,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOKORO_AUTO_CONSOLIDATION_ENABLED", raising=False)
    monkeypatch.delenv("KOKORO_AUTO_CONSOLIDATION_THRESHOLD", raising=False)
    monkeypatch.delenv("KOKORO_AUTO_CONSOLIDATION_COOLDOWN_HOURS", raising=False)


def test_enabled_by_default_with_no_env() -> None:
    """零 env 的 DB-only self-host 必須拿到開啟的 trigger。

    零參數呼叫本身就是回歸釘：誰把 provider id 加回簽名，這裡先爆。
    """
    settings = _load_auto_consolidation_settings()
    assert settings.enabled is True
    assert settings.threshold == 200
    assert settings.cooldown_hours == 6.0


def test_env_off_override_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KOKORO_AUTO_CONSOLIDATION_ENABLED", "0")
    assert _load_auto_consolidation_settings().enabled is False


def test_env_on_override_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KOKORO_AUTO_CONSOLIDATION_ENABLED", "true")
    assert _load_auto_consolidation_settings().enabled is True


def test_dataclass_default_stays_enabled() -> None:
    """直接建構 ``AppSettings`` 的測試 container 一直都接著 trigger 在跑；
    這條釘住那個既成事實，別讓它跟著 loader 一起漂。"""
    assert AutoConsolidationSettings().enabled is True
