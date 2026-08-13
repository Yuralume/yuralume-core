"""角色資料邊界的**消費者政策軸**（CD0，ROADMAP #35）.

``registry.py`` answers *what a character's data is*. This module
answers *what each consumer does with it* — so the boundary stops being
"the backup exporter's private list" and becomes one fact four call
sites resolve against:

===========  ====================================================
consumer     what the policy decides
===========  ====================================================
``BACKUP``   travels in ``.lumebackup`` or not (identity mapping:
             the §3 classification already *is* the policy)
``DELETE``   ``delete_character`` purges / retains / leaves alone
``RESET``    which flag of ``reset_character_data`` clears what
``LUMECARD`` documentary only — column-level subset of A-layer
             ``characters``, no table axis of its own
===========  ====================================================

**Shape: default-per-classification + per-table override.** A flat
``N consumers × 60+ tables`` matrix would be unreadable and would rot
row by row; declaring the default at the classification level means a
newly registered table inherits a defensible stance the moment CB0's pin
test forces it to pick a classification, and only the genuinely
irregular tables spend an override (each of which must carry its own
``reason``).

**Resolution is total and fails loud.** :func:`policy_for` raises
:class:`UnresolvedPolicyError` when a table has neither an override nor
a classification default. That is deliberate for
``EXCLUDE_CROSS_OR_DEPLOYMENT``, which has **no** delete default: every
cross-character / deployment-bound table has to state its own stance,
because "what happens to the other side's row" is never a question one
default can answer. Deleting an override there turns the pin test red
rather than silently falling back.

Nothing here executes. It is a declaration the consumers will be wired
to; CD0 lands the declaration plus its pin test only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from kokoro_link.application.dto.character_backup.registry import (
    BACKUP_TABLE_RULES_BY_NAME,
    BackupClassification,
)


class DataConsumer(str, Enum):
    """Who consumes the character-data boundary."""

    BACKUP = "backup"
    DELETE = "delete"
    RESET = "reset"
    LUMECARD = "lumecard"


TABLE_POLICY_CONSUMERS: tuple[DataConsumer, ...] = (
    DataConsumer.BACKUP,
    DataConsumer.DELETE,
    DataConsumer.RESET,
)
"""Consumers with a per-table stance. ``LUMECARD`` is deliberately
absent — see :data:`LUMECARD_SCOPE`."""


class PolicySource(str, Enum):
    """Where a resolved policy came from (for diagnostics + pinning)."""

    CLASSIFICATION_DEFAULT = "classification_default"
    TABLE_OVERRIDE = "table_override"
    RESET_FLAG = "reset_flag"
    RESET_UNTOUCHED = "reset_untouched"


class UnresolvedPolicyError(LookupError):
    """A consumer has no stance on a table — never fall back silently."""


# ----------------------------------------------------------------------
# Policy verbs
# ----------------------------------------------------------------------


class DeletePolicy(str, Enum):
    """What ``delete_character`` does with a table's rows.

    ``RETAIN`` and ``SKIP`` both mean "rows survive" but for opposite
    reasons, and the distinction is the whole point of writing them
    down: ``RETAIN`` rows *are* this character's data and are kept
    anyway (evidence we are not allowed to destroy); ``SKIP`` rows were
    never exclusively this character's to delete.
    """

    PURGE = "purge"
    """``DELETE ... WHERE character_id = :id`` on this table."""

    PURGE_ANY_SIDE = "purge_any_side"
    """Cross-character row: delete when **either** side matches
    (``or_(from_character_id == id, to_character_id == id)`` and
    friends). Deleting only the "owning" side would leave the peer
    holding half a relationship."""

    PURGE_VIA_PARENT = "purge_via_parent"
    """No own character column — the rows are selected **through the
    parent chain** instead (``DELETE … WHERE <fk> IN (SELECT … FROM
    parent WHERE character_id = :id)``), and the parent's FK must carry
    ``ON DELETE CASCADE`` so the declared verb matches what the schema
    says happens to these rows.

    A consumer must still **emit that statement itself** — relying on the
    database cascade is not an implementation of this policy. Foreign
    keys are only enforced on PostgreSQL and on SQLite under
    ``PRAGMA foreign_keys=ON``, which
    :mod:`kokoro_link.infrastructure.persistence.engine` never sets, so a
    consumer that emits nothing here deletes nothing at all on every
    self-host deployment. ``VIA_PARENT`` names *how the rows are found*,
    not *who removes them*."""

    RETAIN = "retain"
    """Rows deliberately survive the character's deletion."""

    SKIP = "skip"
    """Not this character's rows to delete."""


class ResetPolicy(str, Enum):
    """What one ``reset_character_data`` flag does to a table."""

    PURGE = "purge"
    """The reset issues an explicit ``DELETE ... WHERE character_id = :id``
    on this table."""

    PURGE_VIA_PARENT = "purge_via_parent"
    """No own character column — the rows are selected through the FK
    into a table this reset purges, whose ``ON DELETE`` verb must be
    ``CASCADE`` so the declared policy matches the schema.

    The reset **emits this delete explicitly**; the cascade is the
    schema's agreement with the policy, not the mechanism relied on (see
    :attr:`DeletePolicy.PURGE_VIA_PARENT` for why a cascade the consumer
    leans on is a no-op on self-host SQLite). It is written down here
    because none of it is visible at the ``reset_character_data`` call
    site."""

    CLEAR_REFERENCE = "clear_reference"
    """The row survives, one column is nulled — the reset emits
    ``UPDATE … SET <fk> = NULL`` itself. The FK's ``ON DELETE SET NULL``
    verb is what makes that the correct statement; it is not what
    performs it."""

    SKIP = "skip"
    """Reset never touches this table."""


@dataclass(frozen=True)
class ResolvedPolicy:
    """One consumer's stance on one table."""

    consumer: DataConsumer
    table: str
    policy: BackupClassification | DeletePolicy | ResetPolicy
    reason: str
    source: PolicySource
    reset_flag: "ResetFlag | None" = None
    """Which reset flag produced this, for ``DataConsumer.RESET`` only."""


@dataclass(frozen=True)
class PolicyOverride:
    """A per-table departure from the classification default."""

    policy: BackupClassification | DeletePolicy | ResetPolicy
    reason: str


@dataclass(frozen=True)
class ClassificationDefault:
    """A classification-wide stance for one consumer."""

    policy: BackupClassification | DeletePolicy | ResetPolicy
    reason: str


# ======================================================================
# BACKUP — identity mapping
# ======================================================================

#: The backup consumer's policy *is* the §3 classification: CB0 derived
#: the classification from "does this travel in a ``.lumebackup``" in
#: the first place, so any second vocabulary here would be a synonym
#: table that can only drift.
#:
#: The enum member names still read as backup verbs (``CARRY`` /
#: ``EXCLUDE_RUNTIME``) even though the axis is now consumer-neutral.
#: That is history, kept on purpose: renaming them would sweep through
#: the export reader, the import lander and the restore writer without
#: changing one fact about the data. Read the classification as *what
#: kind of data this is*, not as *what backup does with it* — the
#: mapping below is what makes backup's reading of it explicit.
BACKUP_CLASSIFICATION_DEFAULTS: Mapping[
    BackupClassification, ClassificationDefault,
] = MappingProxyType({
    classification: ClassificationDefault(
        classification,
        "備份消費者的政策即分類本身（恆等映射）。",
    )
    for classification in BackupClassification
})

BACKUP_TABLE_OVERRIDES: Mapping[str, PolicyOverride] = MappingProxyType({})
"""Empty by construction — an override here would mean the table's
classification lies about what backup does with it."""


# ======================================================================
# DELETE — owner 拍板 D1 / D2 (2026-08-05)
# ======================================================================

_D1 = (
    "D1 拍板：計費／稽核證據不可隨角色刪除——帳務對帳要查得到已經發生"
    "過的用量。"
)
_D2 = (
    "D2 拍板：共著作品原樣保留，不刪列也不改寫 character_ids_json——"
    "刪掉一個角色不該讓別的角色的作品少一段。"
)

DELETE_CLASSIFICATION_DEFAULTS: Mapping[
    BackupClassification, ClassificationDefault,
] = MappingProxyType({
    BackupClassification.CARRY: ClassificationDefault(
        DeletePolicy.PURGE,
        "角色專屬活性資料：角色沒了就沒有任何消費者。",
    ),
    BackupClassification.CARRY_YAML: ClassificationDefault(
        DeletePolicy.SKIP,
        "模板／系列是安裝層共用資產，不是角色專屬——別的角色與未來"
        "新建的角色都還綁得到它。",
    ),
    BackupClassification.RECOMPUTE: ClassificationDefault(
        DeletePolicy.PURGE,
        "RECOMPUTE 目前只出現在欄位層（embeddings），欄位隨其表一起"
        "清；宣告在此僅為使分類軸上的解析完整。",
    ),
    BackupClassification.EXCLUDE_RUNTIME: ClassificationDefault(
        DeletePolicy.PURGE,
        "runtime／審計時態列按角色清理，否則角色刪除後留下永遠不會被"
        "讀的孤兒列。",
    ),
    # EXCLUDE_CROSS_OR_DEPLOYMENT 刻意沒有預設 —— 見模組 docstring。
})

DELETE_TABLE_OVERRIDES: Mapping[str, PolicyOverride] = MappingProxyType({
    # --- §3.3 保留的計費／稽核證據（D1）-----------------------------
    "generation_usage_events": PolicyOverride(DeletePolicy.RETAIN, _D1),
    "account_runtime_events": PolicyOverride(
        DeletePolicy.RETAIN,
        _D1 + "（本表以 operator 為鍵，本來也沒有 character 欄可篩。）",
    ),
    # --- §3.3 無 character 欄，按角色刪列語意不成立 ------------------
    "realtime_events": PolicyOverride(
        DeletePolicy.SKIP,
        "realtime outbox 以 tenant/operator 為鍵，沒有 character 欄——"
        "沒有「這個角色的那幾列」可刪；短命列自己會被消費掉。",
    ),
    "cloud_subscription_states": PolicyOverride(
        DeletePolicy.SKIP,
        "每 tenant 一列的訂閱狀態，沒有 character 欄；刪角色不該動到"
        "帳號層的鎖定狀態。",
    ),
    "background_runtime_leases": PolicyOverride(
        DeletePolicy.SKIP,
        "以 lease 名稱為主鍵的 leader 選舉列（全實例共用幾列），沒有"
        "character 欄；按角色刪這裡等於刪掉別人的 leader lease。",
    ),
    # --- CARRY 子表：沒有自己的 character 欄，列選取走父鏈 ----------
    # （父表 FK 為 ON DELETE CASCADE，與 PURGE_VIA_PARENT 動詞相符；
    #   消費者仍須自己明發 DELETE，不得交給 DB cascade。）
    "messages": PolicyOverride(
        DeletePolicy.PURGE_VIA_PARENT,
        "只有 conversation_id（FK ON DELETE CASCADE），無 character 欄。",
    ),
    "schedule_activities": PolicyOverride(
        DeletePolicy.PURGE_VIA_PARENT,
        "只有 schedule_id（FK ON DELETE CASCADE），無 character 欄。",
    ),
    "story_arc_beats": PolicyOverride(
        DeletePolicy.PURGE_VIA_PARENT,
        "只有 arc_id（FK ON DELETE CASCADE），無 character 欄。",
    ),
    "feed_reactions": PolicyOverride(
        DeletePolicy.PURGE_VIA_PARENT,
        "只有 post_id（FK ON DELETE CASCADE），無 character 欄。",
    ),
    "feed_comments": PolicyOverride(
        DeletePolicy.PURGE_VIA_PARENT,
        "只有 post_id（FK ON DELETE CASCADE），無 character 欄。",
    ),
    # --- §3.4 跨角色四表：任一側匹配就刪 ----------------------------
    "character_relationships": PolicyOverride(
        DeletePolicy.PURGE_ANY_SIDE,
        "from/to 任一側是本角色就刪（port 層 delete_for_character 已用"
        " or_() 兩側比對）——只刪單側會留下對方手上半條關係。",
    ),
    "character_peer_profiles": PolicyOverride(
        DeletePolicy.PURGE_ANY_SIDE,
        "character_id/peer_character_id 任一側匹配就刪：對方對本角色的"
        "認識也失去指涉對象。",
    ),
    "character_encounters": PolicyOverride(
        DeletePolicy.PURGE_ANY_SIDE,
        "character_a_id/character_b_id 任一側匹配就刪。",
    ),
    "character_encounter_intents": PolicyOverride(
        DeletePolicy.PURGE_ANY_SIDE,
        "character_id/peer_character_id 任一側匹配就刪。",
    ),
    # --- §3.4 共著作品：原樣保留（D2）------------------------------
    "fusion_stories": PolicyOverride(DeletePolicy.SKIP, _D2),
    "fusion_story_beats": PolicyOverride(
        DeletePolicy.SKIP, _D2 + "（fusion_stories 子表，隨父表去留。）",
    ),
    "fusion_story_versions": PolicyOverride(
        DeletePolicy.SKIP, _D2 + "（fusion_stories 子表，隨父表去留。）",
    ),
    "branching_dramas": PolicyOverride(DeletePolicy.SKIP, _D2),
    "branching_drama_nodes": PolicyOverride(
        DeletePolicy.SKIP, _D2 + "（branching_dramas 子表，隨父表去留。）",
    ),
    "branching_drama_sessions": PolicyOverride(
        DeletePolicy.SKIP, _D2 + "（branching_dramas 子表，隨父表去留。）",
    ),
    # --- §3.4 其餘：部署綁定但確實屬於這個角色 ----------------------
    "messaging_accounts": PolicyOverride(
        DeletePolicy.PURGE,
        "以 character_id 為鍵的通訊帳號：角色沒了，credential 與 "
        "webhook 綁定就該一起沒。",
    ),
    "channel_bindings": PolicyOverride(
        DeletePolicy.PURGE_VIA_PARENT,
        "只有 account_id（FK ON DELETE CASCADE），無 character 欄——"
        "列選取走 messaging_accounts 這條父鏈；消費者仍須自己明發 "
        "DELETE，不得倚賴 DB cascade（self-host SQLite 不開 "
        "PRAGMA foreign_keys，倚賴 cascade＝一列都沒刪）。",
    ),
    "character_event_inbox": PolicyOverride(
        DeletePolicy.PURGE,
        "以 character_id 為鍵的世界事件收件匣：角色沒了就沒人讀。",
    ),
    "character_event_mentions": PolicyOverride(
        DeletePolicy.PURGE,
        "以 character_id 為鍵的「這個角色已經跟玩家提過哪些事件」索引："
        "角色沒了就沒人讀，同 character_event_inbox。",
    ),
})


# ======================================================================
# RESET — 現行 reset_character_data 的實際行為（語意不變）
# ======================================================================


class ResetFlag(str, Enum):
    """The four flags of
    ``CharacterService.reset_character_data``."""

    MEMORIES = "memories"
    CONVERSATIONS = "conversations"
    STATE_HISTORY = "state_history"
    OPERATOR_PERSONA = "operator_persona"


@dataclass(frozen=True)
class ResetTableEffect:
    """What one flag does to one table."""

    table: str
    policy: ResetPolicy
    reason: str


#: Reset is **description, not design**: every entry below is what the
#: four repository calls already do today, read off the SA
#: implementations, not what they arguably should do. Widening or
#: narrowing this set would change ``reset_character_data``'s meaning,
#: which CD0 is explicitly not allowed to do.
#:
#: The ``PURGE_VIA_PARENT`` / ``CLEAR_REFERENCE`` rows are the ones worth
#: writing down: ``reset(conversations=True)`` reads as "clear the chat
#: log", but everything hanging off ``conversations`` goes with it — every
#: turn journal, **every unfulfilled promise** (``pending_follow_ups``)
#: and every scene session — and the channel binding's conversation
#: pointer is nulled. None of that is visible at the call site. The FK
#: verbs quoted below say what the schema agrees should happen to those
#: rows; the eraser still emits every one of those statements itself.
RESET_FLAG_EFFECTS: Mapping[ResetFlag, tuple[ResetTableEffect, ...]] = (
    MappingProxyType({
        ResetFlag.MEMORIES: (
            ResetTableEffect(
                "memory_items",
                ResetPolicy.PURGE,
                "SaMemoryRepository.delete_for_character：DELETE WHERE "
                "character_id。memory_items 沒有任何 FK 子表，所以就是"
                "這一張表。（memoir_pins.entry_id 是無 FK 的軟參照，"
                "指向被刪記憶的釘選列會留下——CB0 已記載的孤兒債，"
                "不在本票修。）",
            ),
        ),
        ResetFlag.CONVERSATIONS: (
            ResetTableEffect(
                "conversations",
                ResetPolicy.PURGE,
                "SaConversationRepository.delete_for_character：DELETE "
                "WHERE character_id。",
            ),
            ResetTableEffect(
                "messages",
                ResetPolicy.PURGE,
                "同一個 repo 方法明確先刪 MessageRow（FK 亦為 ON "
                "DELETE CASCADE，兩條路都會清）。",
            ),
            ResetTableEffect(
                "turn_journals",
                ResetPolicy.PURGE_VIA_PARENT,
                "conversation_id FK ON DELETE CASCADE：無 character 欄，"
                "列選取走 conversations 這條父鏈（eraser 明發 DELETE，"
                "不倚賴 cascade）。回溯快照跟著對話一起消失。",
            ),
            ResetTableEffect(
                "pending_follow_ups",
                ResetPolicy.PURGE_VIA_PARENT,
                "conversation_id（NOT NULL）FK ON DELETE CASCADE：列選取"
                "走 conversations 父鏈（eraser 明發 DELETE）。清對話會連"
                "同尚未兌現的約定一起清掉，呼叫端看不到這件事。",
            ),
            ResetTableEffect(
                "story_scene_sessions",
                ResetPolicy.PURGE_VIA_PARENT,
                "conversation_id（NOT NULL）FK ON DELETE CASCADE：列選取"
                "走 conversations 父鏈（eraser 明發 DELETE）。起幕場次史"
                "隨對話消失。",
            ),
            ResetTableEffect(
                "channel_bindings",
                ResetPolicy.CLEAR_REFERENCE,
                "conversation_id FK ON DELETE SET NULL：eraser 明發 "
                "UPDATE … SET conversation_id = NULL——綁定列本身留著，"
                "只是不再指向任何對話。",
            ),
        ),
        ResetFlag.STATE_HISTORY: (
            ResetTableEffect(
                "state_snapshots",
                ResetPolicy.PURGE,
                "SaStateHistoryRepository.delete_for_character：DELETE "
                "WHERE character_id；無 FK 子表。",
            ),
        ),
        ResetFlag.OPERATOR_PERSONA: (
            ResetTableEffect(
                "operator_profile_fields",
                ResetPolicy.PURGE,
                "SaOperatorPersonaRepository.delete_for_character：只刪"
                "這一張表。同屬 operator persona 家族的 "
                "operator_address_preferences / "
                "operator_address_change_log / "
                "persona_curiosity_attempts 現行**不**被清——如實記載，"
                "不在本票擴大。",
            ),
        ),
    })
)

RESET_UNTOUCHED_REASON = (
    "reset 不觸及：reset_character_data 只呼叫 memory / conversation / "
    "state_history / operator_persona 四個 repository。"
)


def reset_effects_for_flag(flag: ResetFlag) -> tuple[ResetTableEffect, ...]:
    """Tables one reset flag actually affects."""
    return RESET_FLAG_EFFECTS[flag]


def reset_tables_for_flag(flag: ResetFlag) -> frozenset[str]:
    """Table names one reset flag actually affects."""
    return frozenset(effect.table for effect in RESET_FLAG_EFFECTS[flag])


def reset_primary_table_for_flag(flag: ResetFlag) -> str:
    """The table whose row count ``reset_character_data`` reports for
    one flag (CD3).

    By construction the **first** entry declared for a flag in
    :data:`RESET_FLAG_EFFECTS` is the table that names the flag's own
    action, and every declaration above is deliberately ordered that
    way: ``conversations`` leads the ``CONVERSATIONS`` tuple even though
    ``messages`` is *also* an explicit ``PURGE`` right behind it — the
    pre-CD3 repository call only ever reported the former. Reading the
    first entry, rather than writing a second ``flag -> table`` map here,
    is what keeps this a derivation instead of a duplicate declaration.
    """
    return RESET_FLAG_EFFECTS[flag][0].table


_RESET_EFFECT_BY_TABLE: Mapping[str, tuple[ResetFlag, ResetTableEffect]] = (
    MappingProxyType({
        effect.table: (flag, effect)
        for flag, effects in RESET_FLAG_EFFECTS.items()
        for effect in effects
    })
)


# ======================================================================
# LUMECARD — documentary entry (no table axis)
# ======================================================================


@dataclass(frozen=True)
class LumecardScope:
    """``.lumecard`` stated in registry terms, deliberately without a
    per-table policy map.

    ``.lumecard`` is a *character card*, not a save file: it carries a
    column-level subset of the A-layer ``characters`` row plus the arc
    assets the card binds to. Giving it a table-level policy for all
    60-odd registered tables would fabricate 60 stances where the format
    only ever had one question — "which columns of ``characters`` travel"
    — and every one of those fabricated ``SKIP`` rows would be noise a
    future reader has to re-derive. So it cross-references the existing
    column-level mechanism instead of restating it.
    """

    summary: str
    carried_tables: tuple[str, ...]
    column_scope_table: str
    excluded_column_classifications: tuple[BackupClassification, ...]
    reason: str


LUMECARD_SCOPE = LumecardScope(
    summary=(
        "角色卡：characters 的 A 層欄位子集＋卡片綁定的 arc 資產"
        "（走既有 YAML 打包與 _save_with_remap 落地）。"
    ),
    carried_tables=(
        "characters",
        "arc_templates",
        "arc_series",
        "arc_series_members",
    ),
    column_scope_table="characters",
    excluded_column_classifications=(
        BackupClassification.EXCLUDE_CROSS_OR_DEPLOYMENT,
    ),
    reason=(
        "不帶的欄位就是 registry 上 characters.excluded_columns 裡標為 "
        "B 層（EXCLUDE_CROSS_OR_DEPLOYMENT）的那些——voice_profile_json"
        "／loras_json／feature_* 路由／subscription_locked。這裡只交叉"
        "引用該機制，不另立一份會漂移的清單。"
    ),
)


def lumecard_excluded_columns() -> frozenset[str]:
    """Columns ``.lumecard`` leaves behind, **derived** from the registry.

    Reads ``characters.excluded_columns`` rather than copying it, so the
    card's B-layer carve-out cannot drift from the backup one.
    """
    rule = BACKUP_TABLE_RULES_BY_NAME[LUMECARD_SCOPE.column_scope_table]
    wanted = set(LUMECARD_SCOPE.excluded_column_classifications)
    return frozenset(
        column
        for column, exclusion in rule.excluded_columns.items()
        if exclusion.classification in wanted
    )


# ======================================================================
# Resolution
# ======================================================================


_CLASSIFICATION_DEFAULTS: Mapping[
    DataConsumer, Mapping[BackupClassification, ClassificationDefault],
] = MappingProxyType({
    DataConsumer.BACKUP: BACKUP_CLASSIFICATION_DEFAULTS,
    DataConsumer.DELETE: DELETE_CLASSIFICATION_DEFAULTS,
})

_TABLE_OVERRIDES: Mapping[DataConsumer, Mapping[str, PolicyOverride]] = (
    MappingProxyType({
        DataConsumer.BACKUP: BACKUP_TABLE_OVERRIDES,
        DataConsumer.DELETE: DELETE_TABLE_OVERRIDES,
    })
)


def policy_for(consumer: DataConsumer, table: str) -> ResolvedPolicy:
    """Resolve ``consumer``'s stance on ``table``.

    Raises :class:`UnresolvedPolicyError` for an unregistered table, for
    a consumer with no table axis (``LUMECARD``), or when a registered
    table has neither an override nor a classification default.
    """
    rule = BACKUP_TABLE_RULES_BY_NAME.get(table)
    if rule is None:
        raise UnresolvedPolicyError(
            f"{table!r} is not in CHARACTER_BACKUP_TABLE_RULES — register "
            "it in registry.py before asking for a consumer policy.",
        )
    if consumer is DataConsumer.RESET:
        return _reset_policy_for(table)
    if consumer not in _TABLE_OVERRIDES:
        raise UnresolvedPolicyError(
            f"{consumer.value!r} has no per-table policy axis "
            "(see LUMECARD_SCOPE).",
        )
    override = _TABLE_OVERRIDES[consumer].get(table)
    if override is not None:
        return ResolvedPolicy(
            consumer=consumer,
            table=table,
            policy=override.policy,
            reason=override.reason,
            source=PolicySource.TABLE_OVERRIDE,
        )
    default = _CLASSIFICATION_DEFAULTS[consumer].get(rule.classification)
    if default is None:
        raise UnresolvedPolicyError(
            f"{consumer.value} has no policy for {table!r}: classification "
            f"{rule.classification.value!r} has no default, so this table "
            "must declare its own override with a reason.",
        )
    return ResolvedPolicy(
        consumer=consumer,
        table=table,
        policy=default.policy,
        reason=default.reason,
        source=PolicySource.CLASSIFICATION_DEFAULT,
    )


def _reset_policy_for(table: str) -> ResolvedPolicy:
    found = _RESET_EFFECT_BY_TABLE.get(table)
    if found is None:
        return ResolvedPolicy(
            consumer=DataConsumer.RESET,
            table=table,
            policy=ResetPolicy.SKIP,
            reason=RESET_UNTOUCHED_REASON,
            source=PolicySource.RESET_UNTOUCHED,
        )
    flag, effect = found
    return ResolvedPolicy(
        consumer=DataConsumer.RESET,
        table=table,
        policy=effect.policy,
        reason=effect.reason,
        source=PolicySource.RESET_FLAG,
        reset_flag=flag,
    )


#: Policy verbs that mean "rows survive" — every one of them must carry a
#: non-empty reason, because "we chose not to touch it" is the stance a
#: future reader is most likely to mistake for an oversight.
SURVIVING_POLICIES: frozenset[
    BackupClassification | DeletePolicy | ResetPolicy
] = frozenset({
    DeletePolicy.RETAIN,
    DeletePolicy.SKIP,
    ResetPolicy.SKIP,
    ResetPolicy.CLEAR_REFERENCE,
})
