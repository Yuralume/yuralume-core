"""角色私人 NPC 同伴。

每個 character 可以掛一組 ``CharacterCompanion`` —— 同事、室友、家人、
朋友這類「在角色生活圈裡有名字的配角」。他們不是獨立的 character
entity、不會跑自己的 surface（聊天 / 動態 / 劇場），也不會寫入自己的
``MemoryItem``。存在的目的只有一個：讓角色的行程、貼文、對話可以
自然地提到「今天跟誰誰出去」，避免一切都是獨角戲。

當 character 的 schedule activity 或 memory 中提到 companion 時，
參考 ``actor_kind="npc"`` 的 :class:`ParticipantRef` 機制；
``ParticipantRef.actor_id`` 可以填 companion 的 ``id`` 來反向追蹤
是哪一個。如果未來需要把 companion 升格為真正的 character，這個
``id`` 也能拿來保留歷史關係的連續性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

_MAX_NAME_CHARS = 40
_MAX_ROLE_CHARS = 40
_MAX_PROFILE_CHARS = 240
_MAX_PERSONALITY_ITEMS = 6
_MAX_PERSONALITY_ITEM_CHARS = 30
_MAX_RELATIONSHIP_CHARS = 160


@dataclass(frozen=True, slots=True)
class CharacterCompanion:
    """一位 NPC 同伴的設定。

    ``id`` 是 UUID4，建立後不變，後續從 memory / participant 反查就靠它。

    ``name`` 是角色腦中如何稱呼這位同伴（必填，例：「室友小美」、
    「上司王哥」）。

    ``role`` 是這位同伴跟角色的關係類型（自由文字，例：「室友」、
    「同事」、「弟弟」、「青梅竹馬」、「合作對象」）。空字串表示
    未指定。

    ``brief_profile`` 是一段二三十字的速寫（職業、外貌、個性的混合），
    給 LLM 在 schedule / chat prompt 用作上下文。空字串代表只有名字
    跟關係即可。

    ``personality_sketch`` 是 1~6 個短詞，補強 brief_profile 抓不到
    的個性傾向。空 list 代表不額外提示。

    ``relationship_snippet`` 是 character 與此同伴關係的關鍵片段
    （例：「兩年室友，感情很好」、「上週吵過架還沒和好」），
    讓 LLM 在生成互動時能反映關係的當下狀態。空字串代表中性關係。
    """

    id: str
    name: str
    role: str = ""
    brief_profile: str = ""
    personality_sketch: tuple[str, ...] = field(default_factory=tuple)
    relationship_snippet: str = ""

    def __post_init__(self) -> None:
        name = (self.name or "").strip()[:_MAX_NAME_CHARS]
        if not name:
            raise ValueError("CharacterCompanion.name must be non-empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self, "role", (self.role or "").strip()[:_MAX_ROLE_CHARS],
        )
        object.__setattr__(
            self,
            "brief_profile",
            (self.brief_profile or "").strip()[:_MAX_PROFILE_CHARS],
        )
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in self.personality_sketch or ():
            if not isinstance(item, str):
                continue
            trimmed = item.strip()[:_MAX_PERSONALITY_ITEM_CHARS]
            if not trimmed or trimmed in seen:
                continue
            seen.add(trimmed)
            cleaned.append(trimmed)
            if len(cleaned) >= _MAX_PERSONALITY_ITEMS:
                break
        object.__setattr__(self, "personality_sketch", tuple(cleaned))
        object.__setattr__(
            self,
            "relationship_snippet",
            (self.relationship_snippet or "").strip()[:_MAX_RELATIONSHIP_CHARS],
        )

    @classmethod
    def create(
        cls,
        *,
        name: str,
        role: str = "",
        brief_profile: str = "",
        personality_sketch: tuple[str, ...] | list[str] | None = None,
        relationship_snippet: str = "",
        id_: str | None = None,
    ) -> "CharacterCompanion":
        return cls(
            id=id_ or str(uuid4()),
            name=name,
            role=role,
            brief_profile=brief_profile,
            personality_sketch=tuple(personality_sketch or ()),
            relationship_snippet=relationship_snippet,
        )
