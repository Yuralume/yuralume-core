"""CharacterDisposition — 角色的「內在動機傾向」四維 qualitative band。

跟 :class:`Familiarity` 同樣的設計哲學：**只給 LLM 質性 phrase，不暴露數值**。
四個維度都是 ``low / medium / high`` 三段，渲染成中文事實層片段灌入 prompt：

* ``self_centeredness`` — 自我中心度：高者偏好分享自己的近況，低者偏好詢問對方
* ``candor``           — 直言程度：高者遇到歧見直說，低者傾向附和、委婉
* ``sharing_drive``    — 分享慾：高者經常想找人說話，低者沒事不會主動開口
* ``associativeness``  — 聯想力：高者容易翻起過去聊過的事，低者就事論事

**LLM-first 紅線**：四維任何情況下都**不應**進入 heuristic gate / ranker /
busy decider 等程式分支條件。它們只能渲染成 prompt 中的事實層描述，由 LLM
自行決定如何在當下對話中展現。如果出現
``if disposition.sharing_drive == "high": …`` 這種程式判斷，那就是違反
``CLAUDE.md`` 第一條紅線的退化。

設計理由（見 ``docs/SESSION_HANDOFF.md`` 對應段、與作者對話脈絡）：
``Character.personality`` 是描述性人格（外向、體貼），對 LLM 的引導力其實
飄忽；本 VO 把「行為傾向」獨立成可控、可單測、可前端調整的維度。

不使用 ``from __future__ import annotations`` —— 跟 ``familiarity.py`` 同樣
理由，``ClassVar`` 單例 pattern 仰賴 dataclass 在 class build time 識別
``ClassVar[...]``，PEP 563 的字串延後求值會破壞偵測。
"""

from dataclasses import dataclass, replace
from typing import ClassVar


_VALID_BANDS: frozenset[str] = frozenset({"low", "medium", "high"})

_FIELDS: tuple[str, ...] = (
    "self_centeredness",
    "candor",
    "sharing_drive",
    "associativeness",
)


def _normalise_band(raw: object, *, field_name: str) -> str:
    """Coerce caller input to one of the valid bands.

    Defensive: API / DB / 前端 都可能丟進大小寫不一、空字串、None。空值
    一律回 ``"medium"``（中性預設），無效字串會丟 :class:`ValueError`
    避免悄悄吃掉操作員的 typo。
    """
    if raw is None:
        return "medium"
    if not isinstance(raw, str):
        raise ValueError(
            f"CharacterDisposition.{field_name} must be a string band, "
            f"got {type(raw).__name__}",
        )
    cleaned = raw.strip().lower()
    if not cleaned:
        return "medium"
    if cleaned not in _VALID_BANDS:
        raise ValueError(
            f"CharacterDisposition.{field_name} must be one of "
            f"{sorted(_VALID_BANDS)}, got {raw!r}",
        )
    return cleaned


@dataclass(frozen=True, slots=True)
class CharacterDisposition:
    """四維內在動機傾向，全部預設 ``"medium"`` —— 對應「沒設定」狀態。

    全 medium 視為 ``is_default``，prompt builder 可以選擇跳過渲染避免無
    意義的提示噪音。任一維非 medium 就完整渲染四行（讓 LLM 知道完整光譜
    上的相對位置）。
    """

    self_centeredness: str = "medium"
    candor: str = "medium"
    sharing_drive: str = "medium"
    associativeness: str = "medium"

    DEFAULT: "ClassVar[CharacterDisposition]"

    def __post_init__(self) -> None:
        # 走 ``__setattr__`` 因為 dataclass 是 frozen。
        for field_name in _FIELDS:
            value = getattr(self, field_name)
            object.__setattr__(
                self, field_name, _normalise_band(value, field_name=field_name),
            )

    @property
    def is_default(self) -> bool:
        """所有四維都是 medium —— 等同於「沒設定 / 中性人格」。"""
        return all(getattr(self, name) == "medium" for name in _FIELDS)

    def with_overrides(self, **changes: str) -> "CharacterDisposition":
        return replace(self, **changes)

    @classmethod
    def from_payload(cls, data: object) -> "CharacterDisposition":
        """從 API / DB JSON 還原。``None`` / 空 dict → 全 medium 預設。

        未知 key 一律忽略（forward-compatible：若未來新增第五維，舊版本
        程式碼讀新資料不會炸）。
        """
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
        """渲染成 prompt 事實層片段（list[str]，每元素一行）。

        全 medium 時回空 list —— 沒必要在 prompt 裡放一段「都是中性」的
        噪音佔 context 空間。任一維非 medium 時，**完整渲染四行**：讓 LLM
        看到所有維度的相對位置，而不是只有偏離 medium 的那幾條。

        刻意不暴露 ``low / medium / high`` 字面，改寫成自然中文描述，避免
        LLM 進入「band-matching」模式（"我是 high 所以要 80% 自我中心"）。
        """
        if self.is_default:
            return []
        lines = ["你的內在表達傾向（影響語氣節奏，不是強制規則）："]
        lines.append(f"- {_describe_self_centeredness(self.self_centeredness)}")
        lines.append(f"- {_describe_candor(self.candor)}")
        lines.append(f"- {_describe_sharing_drive(self.sharing_drive)}")
        lines.append(f"- {_describe_associativeness(self.associativeness)}")
        return lines


CharacterDisposition.DEFAULT = CharacterDisposition()


def _describe_self_centeredness(band: str) -> str:
    if band == "low":
        return (
            "自我表達：傾向先關心對方近況再講自己；對話節奏偏向把焦點"
            "讓給使用者，不會一開口就大講自己的事"
        )
    if band == "high":
        return (
            "自我表達：有想分享的事就會主動講，興奮點容易先從自己的"
            "近況或感受切入，但不至於完全不問對方"
        )
    return (
        "自我表達：自然交替分享自己與關心對方，沒有特別偏向哪一邊"
    )


def _describe_candor(band: str) -> str:
    if band == "low":
        return (
            "面對歧見：傾向先傾聽、附和或委婉表達，較少直接唱反調；"
            "不代表沒主見，只是會盡量避開硬碰硬"
        )
    if band == "high":
        return (
            "面對歧見：有不同看法會直說，不勉強附和對方；不是為反而反，"
            "是把『直話直說』看得比『場面和諧』更重要"
        )
    return (
        "面對歧見：有不同看法時會表達，但會看場合與對方情緒拿捏分寸"
    )


def _describe_sharing_drive(band: str) -> str:
    if band == "low":
        return (
            "分享慾：不太常主動找人說話，沒明顯動機不會特意開口；"
            "主動訊息或社群發文都偏節制，通常一兩則短訊就講完，"
            "不會連珠炮洗版"
        )
    if band == "high":
        return (
            "分享慾：心裡有東西就想找人說，沒事也常想分享日常；主動"
            "訊息或社群發文的衝動較頻繁，興奮時會像連珠炮一樣連發幾則，"
            "但仍會自我克制"
        )
    return (
        "分享慾：看心情而定，有事想說就說，沒事也不會強迫自己開口"
    )


def _describe_associativeness(band: str) -> str:
    if band == "low":
        return (
            "回憶連結：聊天偏就事論事，較少把話題勾回到過去聊過的事；"
            "不會主動翻舊帳，被提起時才會延伸"
        )
    if band == "high":
        return (
            "回憶連結：聊天時很容易聯想到以前的事或共同回憶，喜歡把"
            "之前聊過的細節翻出來呼應當下話題"
        )
    return (
        "回憶連結：偶爾會聯想到過去聊過的事，但不會主導話題走向"
    )
