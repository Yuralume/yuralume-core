"""角色資料邊界權威 registry（CB0）.

The system had no authoritative definition of "which tables are a
character's data" — ``delete_character`` clears 12 repositories and 16
character tables have no FK. **This module is that definition now**:
every table that references characters is listed with its §3.1–§3.4
classification and the reason, and the pin test
(``tests/unit/test_character_backup_registry.py``) turns red the moment
a new character-referencing table lands without registering here.

Classification map (plan §3):

- ``CARRY``          §3.1 帶（活性資料）— exported as ``data/<table>.jsonl``
                     through the listed DTO.
- ``CARRY_YAML``     §3.1 帶，但走 `.lumecard` 既有 YAML 打包與
                     ``_save_with_remap`` 落地機制，不另做 DTO。
- ``RECOMPUTE``      §3.2 不帶、還原後重算 — used at *column* level
                     (embeddings); WebP variants / TTS cache are storage
                     artifacts, not tables, and regenerate on their own.
- ``EXCLUDE_RUNTIME``  §3.3 審計／計費／runtime 時態 — restoring these
                     would fabricate history or ghost work.
- ``EXCLUDE_CROSS_OR_DEPLOYMENT``  §3.4 跨角色＋部署綁定（含還原即懸空
                     的跨實例引用）。

The CARRY entries are ordered **parent-before-child** — the same order a
restore must land rows in (and reverse order for failure cleanup).

Beyond the table axis this module also carries the two *non-table*
boundaries every consumer needs (CD0):

- :data:`CHARACTER_STORAGE_PREFIXES` — the object-key prefixes a
  character owns wholesale.
- :data:`CHARACTER_MEDIA_URL_FIELDS` — the DB columns that hold media
  URLs, i.e. the reverse-lookup harvest list. Prefix sweeps alone are
  **not** sufficient (see the ``users/{user_id}/chat-uploads/`` note on
  :data:`OPERATOR_SCOPED_MEDIA_PREFIXES`), which is why this list has to
  stay complete.

This module is the **data axis** (what a character's data *is*). What
each consumer *does* with a classification lives next door in
``consumer_policies.py`` (CD0) — backup, delete, reset and ``.lumecard``
all resolve against these rules instead of restating them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from kokoro_link.application.dto.character_backup.base import BackupRecord
from kokoro_link.application.dto.character_backup.character import (
    CharacterBackupRecord,
)
from kokoro_link.application.dto.character_backup.conversation import (
    ConversationBackupRecord,
    DeferredIntentBackupRecord,
    DialogueCheckpointBackupRecord,
    MessageBackupRecord,
    PendingFollowUpBackupRecord,
    TurnJournalBackupRecord,
)
from kokoro_link.application.dto.character_backup.feed import (
    CharacterAlbumItemBackupRecord,
    FeedCommentBackupRecord,
    FeedPostBackupRecord,
    FeedReactionBackupRecord,
)
from kokoro_link.application.dto.character_backup.memory import (
    MemoirPinBackupRecord,
    MemoryItemBackupRecord,
)
from kokoro_link.application.dto.character_backup.persona import (
    CharacterOperatorRelationshipSeedBackupRecord,
    OperatorAddressChangeLogBackupRecord,
    OperatorAddressPreferenceBackupRecord,
    OperatorProfileFieldBackupRecord,
    PersonaCuriosityAttemptBackupRecord,
    PlayerPersonaNoteBackupRecord,
)
from kokoro_link.application.dto.character_backup.schedule import (
    DailyScheduleBackupRecord,
    ScheduleActivityBackupRecord,
)
from kokoro_link.application.dto.character_backup.state_profile import (
    BehavioralPatternBackupRecord,
    CharacterGoalBackupRecord,
    DispositionDriftHistoryBackupRecord,
    EmotionEventBackupRecord,
    SelfReflectionBackupRecord,
    StateSnapshotBackupRecord,
)
from kokoro_link.application.dto.character_backup.story import (
    CharacterSeriesProgressBackupRecord,
    StoryArcBackupRecord,
    StoryArcBeatBackupRecord,
    StoryEventBackupRecord,
    StorySceneSessionBackupRecord,
    StorySeedBackupRecord,
)


class BackupClassification(str, Enum):
    CARRY = "carry"
    CARRY_YAML = "carry_yaml"
    RECOMPUTE = "recompute"
    EXCLUDE_RUNTIME = "exclude_runtime"
    EXCLUDE_CROSS_OR_DEPLOYMENT = "exclude_cross_or_deployment"


@dataclass(frozen=True)
class ExcludedColumn:
    """Column-level carve-out inside a CARRY table."""

    classification: BackupClassification
    reason: str


@dataclass(frozen=True)
class BackupTableRule:
    """One table's place in the character-data boundary."""

    table: str
    classification: BackupClassification
    reason: str
    dto: type[BackupRecord] | None = None
    """Row DTO for CARRY tables; ``None`` otherwise."""
    excluded_columns: Mapping[str, ExcludedColumn] = field(
        default_factory=dict,
    )
    """Columns of a CARRY table that do NOT travel. The pin test
    enforces ``dto fields == table columns − excluded_columns``."""
    row_filter: str | None = None
    """Documented predicate when only part of the table is character
    data (the export query must apply it)."""
    parent_table: str | None = None
    """For CARRY children reached through a parent (no own
    ``character_id``)."""


_B_LAYER = BackupClassification.EXCLUDE_CROSS_OR_DEPLOYMENT
_RUNTIME = BackupClassification.EXCLUDE_RUNTIME


CHARACTER_BACKUP_TABLE_RULES: tuple[BackupTableRule, ...] = (
    # ------------------------------------------------------------------
    # §3.1 帶（活性資料）— restore lands rows in THIS order.
    # ------------------------------------------------------------------
    BackupTableRule(
        table="characters",
        classification=BackupClassification.CARRY,
        reason=(
            "角色主檔（A 層全部欄位＋image_urls＋累積狀態欄）。"
            "id 僅作重映射錨點；user_id／image_urls 於還原時改寫。"
        ),
        dto=CharacterBackupRecord,
        excluded_columns={
            "voice_profile_json": ExcludedColumn(
                _B_LAYER,
                "B 層部署綁定：指向目標實例不存在的 TTS 設定（同 "
                ".lumecard 慣例）。",
            ),
            "loras_json": ExcludedColumn(
                _B_LAYER,
                "B 層部署綁定：LoRA 檔案是部署資產。",
            ),
            "feature_models_json": ExcludedColumn(
                _B_LAYER,
                "B 層路由：provider/model id 是部署資產。",
            ),
            "feature_image_profiles_json": ExcludedColumn(
                _B_LAYER,
                "B 層路由：image profile id 是部署資產。",
            ),
            "feature_video_profiles_json": ExcludedColumn(
                _B_LAYER,
                "B 層路由：video profile id 是部署資產。",
            ),
            "subscription_locked": ExcludedColumn(
                _B_LAYER,
                "匯出端 Cloud tenant lock 的投影——目標實例的訂閱狀"
                "態與它無關。",
            ),
            "origin_official_card_id": ExcludedColumn(
                _B_LAYER,
                "B 層跨實例引用：指向 Cloud 側的雲端專屬官方卡，還原"
                "到別的實例即懸空。帶著它更糟——還原出的角色會宣稱自"
                "己是託管角色，卻沒有任何授權關係在後面。",
            ),
            "last_consolidated_at": ExcludedColumn(
                _RUNTIME,
                "跨 replica 的 consolidation claim 戳，純 runtime 機"
                "制欄位。",
            ),
        },
    ),
    BackupTableRule(
        table="conversations",
        classification=BackupClassification.CARRY,
        reason="對話主檔。",
        dto=ConversationBackupRecord,
    ),
    BackupTableRule(
        table="messages",
        classification=BackupClassification.CARRY,
        reason=(
            "對話內容（含 attachments_json／content_mode／"
            "safe_summary——hosted D5 折疊發生在匯入時，不在格式層）。"
        ),
        dto=MessageBackupRecord,
        parent_table="conversations",
    ),
    BackupTableRule(
        table="dialogue_checkpoints",
        classification=BackupClassification.CARRY,
        reason=(
            "對話累積摘要（DH3）：角色對「載入視窗以外的整段關係」的"
            "唯一記憶。訊息本身有帶走，但那段的**摘要**無法從一個已"
            "經回溯不到那麼遠的視窗重算——不帶等於還原出一個忘記了"
            "這段關係的角色，而且救不回來。"
            "covers_until_message_key 是訊息的內容指紋不是 row id，"
            "匯入重發 id 之後仍指向同一則訊息。"
        ),
        dto=DialogueCheckpointBackupRecord,
    ),
    BackupTableRule(
        table="turn_journals",
        classification=BackupClassification.CARRY,
        reason="每回合回滾快照——玩家可見的「回溯」功能狀態。",
        dto=TurnJournalBackupRecord,
    ),
    BackupTableRule(
        table="pending_follow_ups",
        classification=BackupClassification.CARRY,
        reason="尚未兌現的延後回覆／約定（角色對玩家的承諾）。",
        dto=PendingFollowUpBackupRecord,
    ),
    BackupTableRule(
        table="deferred_intents",
        classification=BackupClassification.CARRY,
        reason="被時機閘擋下、待再評估的主動動機。",
        dto=DeferredIntentBackupRecord,
    ),
    BackupTableRule(
        table="memory_items",
        classification=BackupClassification.CARRY,
        reason="長期記憶的文字與 metadata。",
        dto=MemoryItemBackupRecord,
        excluded_columns={
            "embedding": ExcludedColumn(
                BackupClassification.RECOMPUTE,
                "§3.2 可重算：向量無模型標記，跨實例混用會靜默劣化；"
                "還原後走既有 attach_embeddings 補算。",
            ),
            "tags_embedding": ExcludedColumn(
                BackupClassification.RECOMPUTE,
                "同 embedding——還原後補算。",
            ),
        },
    ),
    BackupTableRule(
        table="memoir_pins",
        classification=BackupClassification.CARRY,
        reason="玩家釘選的回憶錄項目。",
        dto=MemoirPinBackupRecord,
    ),
    BackupTableRule(
        table="state_snapshots",
        classification=BackupClassification.CARRY,
        reason="狀態變化歷史。",
        dto=StateSnapshotBackupRecord,
    ),
    BackupTableRule(
        table="character_goals",
        classification=BackupClassification.CARRY,
        reason="中期目標。",
        dto=CharacterGoalBackupRecord,
    ),
    BackupTableRule(
        table="emotion_events",
        classification=BackupClassification.CARRY,
        reason="情緒事件 append-only log（回憶錄時間軸的資料源）。",
        dto=EmotionEventBackupRecord,
    ),
    BackupTableRule(
        table="disposition_drift_history",
        classification=BackupClassification.CARRY,
        reason="人格演化軌跡＋30 天 cooldown 的依據。",
        dto=DispositionDriftHistoryBackupRecord,
    ),
    BackupTableRule(
        table="self_reflections",
        classification=BackupClassification.CARRY,
        reason="dream-time 自我反思快照。",
        dto=SelfReflectionBackupRecord,
    ),
    BackupTableRule(
        table="behavioral_patterns",
        classification=BackupClassification.CARRY,
        reason="行為模式觀察。",
        dto=BehavioralPatternBackupRecord,
    ),
    BackupTableRule(
        table="operator_profile_fields",
        classification=BackupClassification.CARRY,
        reason=(
            "五層人際畫像（per-character 對玩家的認識——"
            "「stranger → acquaintance」歷程本體）。operator_id 還原"
            "時重映射到匯入者。"
        ),
        dto=OperatorProfileFieldBackupRecord,
    ),
    BackupTableRule(
        table="operator_address_preferences",
        classification=BackupClassification.CARRY,
        reason="觀察到的稱呼／語距偏好。",
        dto=OperatorAddressPreferenceBackupRecord,
    ),
    BackupTableRule(
        table="player_persona_notes",
        classification=BackupClassification.CARRY,
        reason=(
            "玩家為這段關係自述的身分與世界觀設定——是玩家給角色的演出"
            "授權，關係還原後角色必須繼續接得住。"
        ),
        dto=PlayerPersonaNoteBackupRecord,
    ),
    BackupTableRule(
        table="operator_address_change_log",
        classification=BackupClassification.CARRY,
        reason="稱呼變更歷史（改名軌跡是關係歷程的一部分）。",
        dto=OperatorAddressChangeLogBackupRecord,
    ),
    BackupTableRule(
        table="character_operator_relationship_seeds",
        classification=BackupClassification.CARRY,
        reason="初始關係設定（玩家與角色的關係底稿）。",
        dto=CharacterOperatorRelationshipSeedBackupRecord,
    ),
    BackupTableRule(
        table="persona_curiosity_attempts",
        classification=BackupClassification.CARRY,
        reason="好奇心提問 ledger——避免還原後重複追問已問過的題。",
        dto=PersonaCuriosityAttemptBackupRecord,
    ),
    BackupTableRule(
        table="daily_schedules",
        classification=BackupClassification.CARRY,
        reason="行程主檔。",
        dto=DailyScheduleBackupRecord,
    ),
    BackupTableRule(
        table="schedule_activities",
        classification=BackupClassification.CARRY,
        reason="行程活動區塊。",
        dto=ScheduleActivityBackupRecord,
        parent_table="daily_schedules",
    ),
    BackupTableRule(
        table="story_seeds",
        classification=BackupClassification.CARRY,
        reason=(
            "僅角色私有種子；先於 story_events 落地（seed_id FK "
            "RESTRICT）。"
        ),
        dto=StorySeedBackupRecord,
        row_filter="character_id IS NOT NULL",
    ),
    BackupTableRule(
        table="story_arcs",
        classification=BackupClassification.CARRY,
        reason="故事弧主幹。",
        dto=StoryArcBackupRecord,
    ),
    BackupTableRule(
        table="story_arc_beats",
        classification=BackupClassification.CARRY,
        reason="故事弧 beat。",
        dto=StoryArcBeatBackupRecord,
        parent_table="story_arcs",
    ),
    BackupTableRule(
        table="story_events",
        classification=BackupClassification.CARRY,
        reason=(
            "已展開的故事事件；在 story_seeds／story_arc_beats 之後"
            "落地（seed_id／arc_beat_id 參照）。"
        ),
        dto=StoryEventBackupRecord,
    ),
    BackupTableRule(
        table="story_scene_sessions",
        classification=BackupClassification.CARRY,
        reason="起幕場次（已玩過的場景史＋進行中的 frame）。",
        dto=StorySceneSessionBackupRecord,
    ),
    BackupTableRule(
        table="character_series_progress",
        classification=BackupClassification.CARRY,
        reason=(
            "系列進度。series_id FK → arc_series（CARRY_YAML）：還原"
            "須先落 series 再寫本表並重映射。"
        ),
        dto=CharacterSeriesProgressBackupRecord,
    ),
    BackupTableRule(
        table="feed_posts",
        classification=BackupClassification.CARRY,
        reason="LumeGram 貼文（media URL 由匯出反查收原檔）。",
        dto=FeedPostBackupRecord,
    ),
    BackupTableRule(
        table="feed_reactions",
        classification=BackupClassification.CARRY,
        reason="貼文按讚。",
        dto=FeedReactionBackupRecord,
        parent_table="feed_posts",
    ),
    BackupTableRule(
        table="feed_comments",
        classification=BackupClassification.CARRY,
        reason="貼文留言。",
        dto=FeedCommentBackupRecord,
        parent_table="feed_posts",
    ),
    BackupTableRule(
        table="character_album_items",
        classification=BackupClassification.CARRY,
        reason="相簿索引（原檔隨 assets 打包）。",
        dto=CharacterAlbumItemBackupRecord,
    ),
    # ------------------------------------------------------------------
    # §3.1 帶——沿 .lumecard 既有 YAML 機制（不另做 DTO）
    # ------------------------------------------------------------------
    BackupTableRule(
        table="arc_templates",
        classification=BackupClassification.CARRY_YAML,
        reason=(
            "綁定的 template 沿 .lumecard 既有 YAML 打包與 "
            "_save_with_remap 落地（衝突自動改名）。"
        ),
    ),
    BackupTableRule(
        table="arc_series",
        classification=BackupClassification.CARRY_YAML,
        reason="同 arc_templates——沿 .lumecard 既有機制。",
    ),
    BackupTableRule(
        table="arc_series_members",
        classification=BackupClassification.CARRY_YAML,
        reason="arc_series 的成員列，隨 series 打包。",
    ),
    # ------------------------------------------------------------------
    # §3.3 不帶（審計／計費／runtime 時態）
    # ------------------------------------------------------------------
    BackupTableRule(
        table="turn_records",
        classification=_RUNTIME,
        reason=(
            "每回合 prompt/response 審計，體積為對話本體數倍、僅考古"
            "價值。"
        ),
    ),
    BackupTableRule(
        table="proactive_attempts",
        classification=_RUNTIME,
        reason="主動訊息評估審計 log。",
    ),
    BackupTableRule(
        table="tool_invocations",
        classification=_RUNTIME,
        reason="工具呼叫審計 log。",
    ),
    BackupTableRule(
        table="generation_usage_events",
        classification=_RUNTIME,
        reason="計費／用量 ledger——帳不跟人走實例。",
    ),
    BackupTableRule(
        table="account_runtime_events",
        classification=_RUNTIME,
        reason="operator 級額度帳本（滾動窗配額不可靠還原重置）。",
    ),
    BackupTableRule(
        table="background_jobs",
        classification=_RUNTIME,
        reason="還原會製造幽靈工作。",
    ),
    BackupTableRule(
        table="pending_feed_videos",
        classification=_RUNTIME,
        reason=(
            "在途影片任務的暫存草稿（CV4）：一則還沒發出的貼文＋它的"
            "起始圖與 job ticket。還原它等於在新實例上等一個那邊根本"
            "沒送出的任務——不是幽靈工作就是幽靈貼文；貼文真正落地後"
            "本來就進 `feed_posts`（CARRY），那才是要帶走的東西。"
        ),
    ),
    BackupTableRule(
        table="prompt_material_digests",
        classification=_RUNTIME,
        reason=(
            "上一回合 post-turn 蒸給下一回合讀的素材摘要"
            "（DIGEST_OFFPATH）：每個欄位都是從 emotion_events／"
            "self_reflections／story_events／story_arcs／feed_posts 重"
            "算得出來的衍生值，而那些來源表本身就在 CARRY 清單裡——"
            "還原完第一次 post-turn 就會重新蒸一份。**帶著它反而更"
            "糟**：匯出當下的摘要會以「現況」的語氣掛回還原後的 "
            "prompt，而它描述的是另一個時間點的素材；讀取端的 24h "
            "上限也只是把這個錯誤限縮成一天，不是消掉。與 "
            "dialogue_checkpoints（CARRY）的差別正在這裡：那份摘要"
            "涵蓋的是視窗外、已經重算不回來的對話，這份沒有任何一"
            "個位元是重算不回來的。"
        ),
    ),
    BackupTableRule(
        table="background_visible_slots",
        classification=_RUNTIME,
        reason="tick 去重 claim ledger，純 runtime。",
    ),
    BackupTableRule(
        table="background_runtime_leases",
        classification=_RUNTIME,
        reason="TTL leader lease，純 runtime。",
    ),
    BackupTableRule(
        table="scheduler_tick_journal",
        classification=_RUNTIME,
        reason="tick journal，純 runtime。",
    ),
    BackupTableRule(
        table="realtime_events",
        classification=_RUNTIME,
        reason="realtime outbox，純 runtime。",
    ),
    BackupTableRule(
        table="experiment_assignments",
        classification=_RUNTIME,
        reason="A/B 實驗黏著分派——實驗是部署的，不是角色的。",
    ),
    BackupTableRule(
        table="external_chat_turn_receipts",
        classification=_RUNTIME,
        reason="外部通道 turn 狀態機收據，runtime 時態。",
    ),
    BackupTableRule(
        table="external_proactive_events",
        classification=_RUNTIME,
        reason="外部主動送信 pre-send ledger，runtime 時態。",
    ),
    BackupTableRule(
        table="line_reactivation_campaign_items",
        classification=_RUNTIME,
        reason=(
            "LINE 休眠回訪 campaign 的逐角色 outcome（LR 系列）：這是"
            "**營運者做過什麼**的稽核列，不是角色的資料。它掛在一個備"
            "份不帶走的 campaign 上，把它還原到另一個部署等於宣稱那邊"
            "發生過一次從未發生的後台操作。"
            "（父表 `line_reactivation_campaigns` 沒有任何 character 欄，"
            "本來就不在本 registry 的掃描範圍內。）"
        ),
    ),
    BackupTableRule(
        table="cloud_subscription_states",
        classification=_RUNTIME,
        reason="Cloud tenant 鎖定狀態——目標實例自己的訂閱說了算。",
    ),
    BackupTableRule(
        table="character_image_candidate_batches",
        classification=_RUNTIME,
        reason="未提交的相簿候選批是暫態（§3.1 註記）。",
    ),
    BackupTableRule(
        table="character_backup_jobs",
        classification=_RUNTIME,
        reason="本系列自己的 job 表——備份不備份備份工作。",
    ),
    # ------------------------------------------------------------------
    # §3.4 不帶（跨角色＋部署綁定＋還原即懸空）
    # ------------------------------------------------------------------
    BackupTableRule(
        table="character_relationships",
        classification=_B_LAYER,
        reason="跨角色四表之一：每列同屬兩角色，目標實例沒有對方。",
    ),
    BackupTableRule(
        table="character_peer_profiles",
        classification=_B_LAYER,
        reason="跨角色四表之一：對另一角色的認識，對方不在就懸空。",
    ),
    BackupTableRule(
        table="character_encounters",
        classification=_B_LAYER,
        reason="跨角色四表之一：兩角色的相遇紀錄。",
    ),
    BackupTableRule(
        table="character_encounter_intents",
        classification=_B_LAYER,
        reason="跨角色四表之一：對另一角色的相遇意向。",
    ),
    BackupTableRule(
        table="fusion_stories",
        classification=_B_LAYER,
        reason=(
            "character_ids_json 多角色共著，連 per-character 查詢入口"
            "都沒有。"
        ),
    ),
    BackupTableRule(
        table="fusion_story_beats",
        classification=_B_LAYER,
        reason="fusion_stories 子表（focus_character_ids_json 同理）。",
    ),
    BackupTableRule(
        table="fusion_story_versions",
        classification=_B_LAYER,
        reason="fusion_stories 子表。",
    ),
    BackupTableRule(
        table="branching_dramas",
        classification=_B_LAYER,
        reason="character_ids_json 多角色共著，同 fusion_stories。",
    ),
    BackupTableRule(
        table="branching_drama_nodes",
        classification=_B_LAYER,
        reason=(
            "branching_dramas 子表（appearing_character_ids_json 同"
            "理）。"
        ),
    ),
    BackupTableRule(
        table="branching_drama_sessions",
        classification=_B_LAYER,
        reason="branching_dramas 子表。",
    ),
    BackupTableRule(
        table="messaging_accounts",
        classification=_B_LAYER,
        reason=(
            "通訊帳號 credential 與 webhook 是部署資產（同 .lumecard "
            "B 層不帶的理由）。"
        ),
    ),
    BackupTableRule(
        table="channel_bindings",
        classification=_B_LAYER,
        reason="messaging_accounts 子表，同上。",
    ),
    BackupTableRule(
        table="character_event_inbox",
        classification=_B_LAYER,
        reason=(
            "FK → world_events：目標實例沒有同一批全域事件，還原必懸"
            "空（§3.1 註記）。"
        ),
    ),
    BackupTableRule(
        table="character_event_mentions",
        classification=_B_LAYER,
        reason=(
            "FK → world_events：同 character_event_inbox，目標實例沒有"
            "同一批全域事件，還原必懸空。它記的也是「這個部署裡角色講過"
            "哪則新聞」，本身就是部署綁定的事實。"
        ),
    ),
)


BACKUP_TABLE_RULES_BY_NAME: Mapping[str, BackupTableRule] = (
    MappingProxyType(
        {rule.table: rule for rule in CHARACTER_BACKUP_TABLE_RULES},
    )
)


def carried_table_rules() -> tuple[BackupTableRule, ...]:
    """CARRY rules in restore landing order (parent-before-child)."""
    return tuple(
        rule
        for rule in CHARACTER_BACKUP_TABLE_RULES
        if rule.classification is BackupClassification.CARRY
    )


# ======================================================================
# Storage axis — object-key prefixes a character owns
# ======================================================================


@dataclass(frozen=True)
class CharacterStoragePrefix:
    """One object-key prefix whose whole subtree belongs to a character.

    ``template`` is a ``str.format`` template taking ``character_id``;
    :func:`character_storage_prefixes` is the only sanctioned way to
    build the concrete prefix so no consumer re-derives the key shape.
    """

    template: str
    reason: str
    writers: tuple[str, ...]
    """Where keys under this prefix are actually written — the sites this
    entry has to stay true to."""


#: Prefixes that are *entirely* one character's. Every consumer that has
#: to reason about a character's stored objects — the backup exporter
#: (collect), the restore planner (rewrite), a future delete sweep
#: (purge) — needs the same set, which is why it lives here rather than
#: in whichever one of them happened to need it first. Same argument as
#: ``contracts/object_storage.EPHEMERAL_OBJECT_KEY_PREFIXES``: a prefix
#: restated at a second site is a prefix that can drift, and the drift is
#: silent in both directions — a writer whose keys no sweep reaches (data
#: that outlives the character), or a sweep aimed at a prefix nothing
#: writes (a delete that quietly does nothing).
CHARACTER_STORAGE_PREFIXES: tuple[CharacterStoragePrefix, ...] = (
    CharacterStoragePrefix(
        template="characters/{character_id}/",
        reason=(
            "角色圖片主目錄：上傳／生成的角色圖、候選批子目錄 "
            "(candidates/)、工具產出 (tools/)、還原後的媒體 "
            "(restored/) 全在同一棵子樹下。"
        ),
        writers=(
            "application/services/character_image_service.py"
            "::CharacterImageService.add_image",
            "application/services/character_image_service.py"
            "::CharacterImageService._generate_candidates",
            "application/services/character_image_service.py"
            "::CharacterImageService._candidate_destination_key",
            "infrastructure/tools/comfyui/tool.py::_generate",
            "application/services/character_backup_restore_plan.py"
            "::rewrite_media_key",
        ),
    ),
    CharacterStoragePrefix(
        template="feed/{character_id}/",
        reason="LumeGram 貼文的圖片與影片原檔。",
        writers=(
            "application/services/feed_composer_service.py"
            "::_store_feed_image",
            "application/services/feed_composer_service.py"
            "::_write_video_bytes",
        ),
    ),
    CharacterStoragePrefix(
        template="tts/{character_id}/",
        reason=(
            "語音合成快取（key 為設定摘要 digest，非可讀檔名）。"
            "純衍生物：刪掉只是失去快取，不會失去事實。"
        ),
        writers=(
            "application/services/tts_service.py"
            "::TTSService._cache_object_key",
        ),
    ),
)


#: Media that a character's rows point at but that does **not** live
#: under any character prefix. Chat attachments are written under the
#: *operator's* tree (``users/{user_id}/chat-uploads/<uuid>.png``, see
#: ``api/routes/chat.py::upload_chat_attachments``) because one upload
#: can be sent to several characters — so no per-character prefix sweep
#: can ever reach them.
#:
#: This is the entire reason :data:`CHARACTER_MEDIA_URL_FIELDS` must be
#: complete: prefix sweeps are the cheap bulk path, the DB reverse
#: lookup is the only path that covers operator-scoped media. A consumer
#: that implements only the prefix sweep leaves every chat attachment
#: behind, silently.
OPERATOR_SCOPED_MEDIA_PREFIXES: tuple[str, ...] = (
    "users/{user_id}/chat-uploads/",
)


def character_storage_prefixes(character_id: str) -> tuple[str, ...]:
    """Concrete owned prefixes for ``character_id``."""
    return tuple(
        entry.template.format(character_id=character_id)
        for entry in CHARACTER_STORAGE_PREFIXES
    )


def operator_scoped_media_prefixes(user_id: str) -> tuple[str, ...]:
    """Concrete operator-scoped prefixes for ``user_id``.

    The counterpart to :func:`character_storage_prefixes` for media a
    character's rows point at but does not own. **Always per-operator**:
    the delete sweep uses these two functions together as its containment
    boundary, and a caller that widened the operator half to "any user"
    would let one operator's row point at — and therefore destroy —
    another operator's uploads.

    ``user_id`` must already be the **key-safe segment** the write path
    stored, not the raw id off the row. The writer folds it through
    ``infrastructure.storage.keys.safe_key_segment`` (see
    ``api/routes/chat.py::_safe_user_dir``), so a hosted operator id
    ``"cloud:abc"`` is stored as ``users/cloudabc/chat-uploads/…``; a
    caller that formats the raw id here builds a prefix no key can start
    with, and the containment check then silently matches nothing. This
    module is on the DTO axis and must not import the infrastructure
    sanitiser itself — the erasure service applies it before calling
    (``application/services/character_data_erasure.py``
    ``::_owned_key_prefixes``).
    """
    return tuple(
        template.format(user_id=user_id)
        for template in OPERATOR_SCOPED_MEDIA_PREFIXES
    )


# ======================================================================
# Media axis — DB columns holding media URLs (reverse-lookup harvest)
# ======================================================================


class MediaUrlShape(str, Enum):
    """How to get URLs out of a column value."""

    SCALAR = "scalar"
    """The column value *is* the URL (or empty/NULL)."""

    STRING_LIST_JSON = "string_list_json"
    """JSON array of URL strings."""

    ATTACHMENT_LIST_JSON = "attachment_list_json"
    """JSON array of attachment dicts; take each dict's ``url``."""


@dataclass(frozen=True)
class MediaUrlField:
    """A column whose value(s) resolve to stored objects."""

    table: str
    column: str
    shape: MediaUrlShape
    reason: str


#: The DB-reverse-lookup harvest surface. Kept identical to what
#: ``CharacterBackupExportService._observe_record`` folds into its media
#: worklist — ``tests/unit/test_character_backup_consumer_policies.py``
#: pins the two together in **both** directions, so neither a new media
#: column here nor a new harvest branch there can land alone.
#:
#: Export collects these; a delete sweep must purge them. Note that
#: ``messages.attachments_json`` is the operator-scoped case the prefix
#: sweep cannot reach at all.
CHARACTER_MEDIA_URL_FIELDS: tuple[MediaUrlField, ...] = (
    MediaUrlField(
        table="characters",
        column="image_urls",
        shape=MediaUrlShape.STRING_LIST_JSON,
        reason="角色主圖與圖庫（`characters/{id}/` 前綴內）。",
    ),
    MediaUrlField(
        table="messages",
        column="attachments_json",
        shape=MediaUrlShape.ATTACHMENT_LIST_JSON,
        reason=(
            "玩家在聊天中傳的附件——寫在 operator 前綴 "
            "`users/{user_id}/chat-uploads/`，前綴掃描永遠掃不到，"
            "只有這條 DB 反查蓋得到。"
        ),
    ),
    MediaUrlField(
        table="feed_posts",
        column="image_url",
        shape=MediaUrlShape.SCALAR,
        reason="LumeGram 貼文圖（`feed/{id}/` 前綴內）。",
    ),
    MediaUrlField(
        table="feed_posts",
        column="video_url",
        shape=MediaUrlShape.SCALAR,
        reason="LumeGram 貼文影片（`feed/{id}/` 前綴內）。",
    ),
    MediaUrlField(
        table="character_album_items",
        column="url",
        shape=MediaUrlShape.SCALAR,
        reason="相簿項目指向的原檔。",
    ),
)
# Deliberately NOT listed: ``pending_feed_videos.first_frame_url`` (CV4).
# This list is the *exporter's* harvest list — every entry must sit on a
# CARRY table, because the reverse lookup exists to catch media a backup
# would otherwise leave behind, and it is cross-checked against the
# exporter's own field handling. The in-flight start frame is written
# inside the character's own ``feed/{character_id}/`` prefix, so the
# prefix sweep already reclaims it on delete; adding it here would only
# break the CARRY-only invariant it depends on.


def media_url_fields_by_table() -> Mapping[str, tuple[MediaUrlField, ...]]:
    """Harvest fields grouped by table (declaration order preserved)."""
    grouped: dict[str, tuple[MediaUrlField, ...]] = {}
    for entry in CHARACTER_MEDIA_URL_FIELDS:
        grouped[entry.table] = grouped.get(entry.table, ()) + (entry,)
    return MappingProxyType(grouped)
