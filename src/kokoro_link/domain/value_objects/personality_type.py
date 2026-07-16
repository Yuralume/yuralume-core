"""CharacterPersonalityType — 16 型性格創作參考。

這是角色 A-layer 靜態設定：可以跟角色卡一起流通，也可以渲染進 prompt
作為創作輔助；但它不是心理診斷，也不能用來寫 Python 行為分支。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


VALID_PERSONALITY_TYPE_CODES: frozenset[str] = frozenset({
    "INTJ", "INTP", "ENTJ", "ENTP",
    "INFJ", "INFP", "ENFJ", "ENFP",
    "ISTJ", "ISFJ", "ESTJ", "ESFJ",
    "ISTP", "ISFP", "ESTP", "ESFP",
})

VALID_PERSONALITY_TYPE_SOURCES: frozenset[str] = frozenset({
    "unset", "user_explicit", "llm_inferred",
})

_MAX_RATIONALE_CHARS = 240
_MAX_NOTE_CHARS = 160
_MAX_NOTES = 5


@dataclass(frozen=True, slots=True)
class CharacterPersonalityType:
    """16 型性格設定。

    ``code=""`` 代表未設定。非空 code 必須是 16 種合法值；這裡刻意
    fail loud，避免 typo 靜默寫進角色資料與 prompt。
    """

    system: str = "mbti_16"
    code: str = ""
    source: str = "unset"
    confidence: float = 0.0
    rationale: str = ""
    consistency_notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        system = (self.system or "mbti_16").strip() or "mbti_16"
        if system != "mbti_16":
            raise ValueError("CharacterPersonalityType.system must be 'mbti_16'")
        object.__setattr__(self, "system", system)

        code = (self.code or "").strip().upper()
        if code and code not in VALID_PERSONALITY_TYPE_CODES:
            raise ValueError(
                "CharacterPersonalityType.code must be one of "
                f"{sorted(VALID_PERSONALITY_TYPE_CODES)} or empty, got {self.code!r}",
            )
        object.__setattr__(self, "code", code)

        source = (self.source or "unset").strip().lower()
        if not code:
            source = "unset"
        elif source == "unset":
            source = "user_explicit"
        if source not in VALID_PERSONALITY_TYPE_SOURCES:
            raise ValueError(
                "CharacterPersonalityType.source must be one of "
                f"{sorted(VALID_PERSONALITY_TYPE_SOURCES)}, got {self.source!r}",
            )
        object.__setattr__(self, "source", source)

        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        object.__setattr__(self, "confidence", max(0.0, min(1.0, confidence)))
        object.__setattr__(
            self, "rationale", _normalise_text(self.rationale, _MAX_RATIONALE_CHARS),
        )
        object.__setattr__(
            self,
            "consistency_notes",
            _normalise_notes(self.consistency_notes),
        )

    @property
    def is_unset(self) -> bool:
        return self.code == ""

    def with_overrides(self, **changes: object) -> "CharacterPersonalityType":
        return replace(self, **changes)

    @classmethod
    def from_payload(cls, data: object) -> "CharacterPersonalityType":
        if not data:
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(
            system=data.get("system", "mbti_16"),
            code=data.get("code", ""),
            source=data.get("source", "unset"),
            confidence=data.get("confidence", 0.0),
            rationale=data.get("rationale", ""),
            consistency_notes=tuple(data.get("consistency_notes", ()) or ()),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "system": self.system,
            "code": self.code,
            "source": self.source,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "consistency_notes": list(self.consistency_notes),
        }

    def to_prompt_lines(self) -> list[str]:
        if self.is_unset:
            return []
        lines = [
            "16 型性格參考（創作輔助，不是硬規則）：",
            f"- 類型：{self.code}",
        ]
        if self.rationale:
            lines.append(f"- 理解方式：{self.rationale}")
        for note in self.consistency_notes:
            lines.append(f"- 注意：{note}")
        lines.append(
            "- 若此參考與更具體的人設、說話風格或當下情境衝突，以具體設定優先。"
        )
        return lines


def _normalise_text(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_chars]


def _normalise_notes(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _normalise_text(item, _MAX_NOTE_CHARS)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= _MAX_NOTES:
            break
    return tuple(out)


CharacterPersonalityType.DEFAULT = CharacterPersonalityType()  # type: ignore[attr-defined]
