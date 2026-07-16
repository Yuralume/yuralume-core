"""BodyState — 角色的「具身訊號」四維 qualitative band (HUMANIZATION_ROADMAP §4.1).

跟 :class:`CharacterDisposition` 同一個設計哲學：**只給 LLM 質性 phrase、
不暴露數值、不做行為分支**。四個維度都是 ``low / medium / high`` 三段
（``low`` = 沒這個身體訊號，``high`` = 強），渲染成自然中文事實層片段
灌入 prompt。

四個維度（2026-05-21 owner 決議：不做月經週期相位）：

* ``hunger``           — 飢餓感
* ``thirst``           — 口渴感
* ``sleep_debt``       — 睡眠債
* ``seasonal_allergy`` — 季節過敏

**LLM-first 紅線**：任何情況下都**不應**根據 BodyState 走程式分支。
``if body.hunger == "high": skip_proactive`` 這種寫法視為違規，必須改回
「把事實塞進 prompt，由 LLM 決定如何在當下對話中體現」。

跟 disposition.py 一樣不使用 ``from __future__ import annotations`` —
``ClassVar`` 單例 pattern 仰賴 dataclass 在 class build time 識別
``ClassVar[...]``。
"""

from dataclasses import dataclass, replace
from typing import Callable, ClassVar


_VALID_BANDS: frozenset[str] = frozenset({"low", "medium", "high"})

_FIELDS: tuple[str, ...] = (
    "hunger",
    "thirst",
    "sleep_debt",
    "seasonal_allergy",
)


def _normalise_band(raw: object, *, field_name: str) -> str:
    if raw is None:
        return "low"
    if not isinstance(raw, str):
        raise ValueError(
            f"BodyState.{field_name} must be a string band, got "
            f"{type(raw).__name__}",
        )
    cleaned = raw.strip().lower()
    if not cleaned:
        return "low"
    if cleaned not in _VALID_BANDS:
        raise ValueError(
            f"BodyState.{field_name} must be one of {sorted(_VALID_BANDS)}, "
            f"got {raw!r}",
        )
    return cleaned


@dataclass(frozen=True, slots=True)
class BodyState:
    """四維具身訊號。全部預設 ``"low"`` —— 對應「沒任何身體不適 / 平常」狀態。

    為何預設 low 而非 disposition 的 medium：disposition 是「人格中性軸」，
    medium = 平均；BodyState 是「不適訊號」，預設應該是「沒不適」也就是
    low。整體 ``is_default`` 時 prompt builder 跳過渲染避免噪音。
    """

    hunger: str = "low"
    thirst: str = "low"
    sleep_debt: str = "low"
    seasonal_allergy: str = "low"

    DEFAULT: "ClassVar[BodyState]"

    def __post_init__(self) -> None:
        for field_name in _FIELDS:
            value = getattr(self, field_name)
            object.__setattr__(
                self, field_name, _normalise_band(value, field_name=field_name),
            )

    @property
    def is_default(self) -> bool:
        return all(getattr(self, name) == "low" for name in _FIELDS)

    def with_overrides(self, **changes: str) -> "BodyState":
        return replace(self, **changes)

    @classmethod
    def from_payload(cls, data: object) -> "BodyState":
        if not data:
            return cls()
        if not isinstance(data, dict):
            return cls()
        kwargs: dict[str, str] = {}
        for field_name in _FIELDS:
            if field_name in data:
                kwargs[field_name] = data[field_name]
        return cls(**kwargs)

    def to_payload(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in _FIELDS}

    def to_prompt_lines(self) -> list[str]:
        """渲染成 prompt 事實層片段。

        全 low 時回空 list（沒身體不適就不必塞段）。任一維非 low 時，
        **只渲染非 low 的維度** —— BodyState 的 prompt 噪音敏感度比
        disposition 高（每多塞一段都壓縮 chat history），所以只說「現在
        有什麼不舒服」而不是完整光譜對照。
        """
        if self.is_default:
            return []
        lines = ["身體訊號（事實層；自然體現於對話、不需直白報告）："]
        for field_name, describer in _DESCRIBERS:
            band = getattr(self, field_name)
            if band == "low":
                continue
            lines.append(f"- {describer(band)}")
        return lines


BodyState.DEFAULT = BodyState()


def _describe_hunger(band: str) -> str:
    if band == "high":
        return "肚子很餓，吃飯念頭明顯，會影響專注力與語氣耐心"
    return "有點餓但還可以撐"


def _describe_thirst(band: str) -> str:
    if band == "high":
        return "口很渴，喉嚨乾，講話間隔比平常想喝水"
    return "嘴有點乾，不至於影響說話"


def _describe_sleep_debt(band: str) -> str:
    if band == "high":
        return "明顯沒睡飽，注意力比平常差，反應慢半拍，容易煩躁"
    return "睡眠略不足，精神比平常稍鈍但還能正常對話"


def _describe_seasonal_allergy(band: str) -> str:
    if band == "high":
        return "季節性過敏發作明顯（鼻塞 / 眼睛癢 / 打噴嚏），體感不舒服"
    return "有點過敏徵兆但還能忍"


_DESCRIBERS: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("hunger", _describe_hunger),
    ("thirst", _describe_thirst),
    ("sleep_debt", _describe_sleep_debt),
    ("seasonal_allergy", _describe_seasonal_allergy),
)
