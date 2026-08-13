"""Application-level tests for the CB3 import chain.

Real seams end to end, mirroring the CB2 export tests: SQLite behind the
export reader / restore writer / SA job repository, the in-memory object
storage adapter, CB1's real cipher, and the CB2 export service producing
the very archives the import consumes (the round-trip contract).

Pinned here (the ticket's acceptance criteria):

1. export → import round-trip: per-table counts and representative
   content survive, every id is fresh, operator-scoped rows rebind to
   the importer, media lands under new keys with URLs rewritten;
2. cloud_mode: zero NSFW raw text lands, and preview == manifest ==
   actual fold counts;
3. a mid-restore failure leaves *nothing* — no rows in any CARRY table,
   no new storage objects;
4. the create gates refuse at confirm time (400 seam), staging is
   ephemeral-prefixed, and the whole error grammar
   (wrong password / tampered / too-new / non-magic / oversize) maps to
   typed errors.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

# Register every ORM table before ``create_all`` (registry pin-test trick).
from kokoro_link.infrastructure.persistence import (  # noqa: F401
    branching_drama_models as _branching_drama_models,
    character_backup_models as _character_backup_models,
    fusion_story_models as _fusion_story_models,
    rss_models as _rss_models,
)

from kokoro_link.application.dto.character_backup import (
    BACKUP_TABLE_RULES_BY_NAME,
    carried_table_rules,
)
from kokoro_link.application.services.character_backup_export_service import (
    CharacterBackupExportService,
)
from kokoro_link.application.services.character_backup_import_service import (
    CharacterBackupImportPreview,
    CharacterBackupImportService,
    CharacterBackupImportTooLargeError,
    CharacterBackupInvalidFileError,
    CharacterBackupPreviewRateLimitedError,
    CharacterBackupSchemaTooNewError,
    CharacterBackupStagedFileNotFoundError,
    CharacterBackupUploadLedgerNotConfiguredError,
    CharacterBackupUploadRateLimitedError,
)
from kokoro_link.application.services.character_service import (
    CharacterValidationError,
)
from kokoro_link.contracts.character_backup_jobs import (
    BACKUP_JOB_KIND_RESTORE,
    BACKUP_JOB_STATUS_FAILED,
    BACKUP_JOB_STATUS_RUNNING,
    BACKUP_JOB_STATUS_SUCCEEDED,
    CharacterBackupJob,
)
from kokoro_link.domain.entities.character import FREEZE_REASON_RESTORE
from kokoro_link.contracts.object_storage import (
    EPHEMERAL_OBJECT_KEY_PREFIXES,
)
from kokoro_link.infrastructure.character_backup.packager import (
    BackupArchiveWriter,
)
from kokoro_link.infrastructure.persistence.engine import (
    build_session_factory,
)
from kokoro_link.infrastructure.persistence.models import (
    Base,
    CharacterAlbumItemRow,
    CharacterRow,
    ConversationRow,
    DailyScheduleRow,
    FeedPostRow,
    MemoryItemRow,
    MessageRow,
    OperatorProfileFieldRow,
    PendingFollowUpRow,
    ScheduleActivityRow,
    StorySeedRow,
)
from kokoro_link.infrastructure.persistence.sa_character_backup_export_reader import (
    SACharacterBackupExportReader,
)
from kokoro_link.infrastructure.persistence.sa_character_backup_job_repository import (
    SACharacterBackupJobRepository,
)
from kokoro_link.infrastructure.persistence.sa_character_backup_restore_writer import (
    SACharacterBackupRestoreWriter,
)
from kokoro_link.infrastructure.security.backup_cipher import (
    BackupIntegrityError,
    BackupWrongPasswordError,
    EnvelopeParams,
    HEADER_LEN,
    decrypt_stream,
    encrypt_stream,
)
from kokoro_link.infrastructure.storage.in_memory import (
    InMemoryObjectStorage,
)

EXPORTER = "default"
IMPORTER = "bob"
PASSWORD = "correct-horse-battery"

_FAST_PARAMS = EnvelopeParams(
    scrypt_n=2**8, scrypt_r=8, scrypt_p=1, chunk_size=64 * 1024,
)

_NSFW_RAW_WITH_SUMMARY = "（限制級原文）"
_NSFW_RAW_NO_SUMMARY = "（限制級原文，無安全摘要）"
_NSFW_MEMORY_RAW = "那一夜。"
_NSFW_PROFILE_FIELD_RAW = "（限制級畫像原文）"
_NSFW_FOLLOW_UP_QUEUED_RAW = "（限制級延後回覆原文）"
_NSFW_FOLLOW_UP_RESOLVED_RAW = "（限制級完整回覆原文）"


class _GateStub:
    """CharacterService gate seam double (the two methods CB3 uses).

    ``usage`` (when wired by the env fixture) mirrors what the real
    ``character_service`` does: every recorded create event lands in the
    account-runtime-usage ledger — the A4 dedup pre-check reads it."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.checks: list[str] = []
        self.recorded: list[str] = []
        self.recorded_resource_ids: list[str | None] = []
        self.usage = None

    async def ensure_character_create_allowed(
        self, user_id: str, *, now=None,  # noqa: ANN001
    ):
        self.checks.append(user_id)
        if self.error is not None:
            raise self.error
        return None

    async def record_character_create_event(
        self,
        *,
        character_id: str,
        user_id: str,
        profile,  # noqa: ANN001
        now=None,  # noqa: ANN001
        resource_id: str | None = None,
    ) -> None:
        self.recorded.append(character_id)
        self.recorded_resource_ids.append(resource_id)
        if self.usage is not None:
            await self.usage.record_event(
                operator_id=user_id,
                event_type="character_create",
                occurred_at=now or datetime.now(timezone.utc),
                resource_id=resource_id or character_id,
            )


class _UsageStub:
    """AccountRuntimeUsage ledger double (record + count only)."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def record_event(
        self,
        *,
        operator_id: str,
        event_type: str,
        occurred_at,  # noqa: ANN001
        resource_id: str | None = None,
    ) -> None:
        self.events.append({
            "operator_id": operator_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "resource_id": resource_id,
        })

    async def count_events(
        self,
        *,
        operator_id: str,
        event_type: str,
        since,  # noqa: ANN001
        until=None,  # noqa: ANN001
        resource_id: str | None = None,
    ) -> int:
        count = 0
        for event in self.events:
            if event["operator_id"] != operator_id:
                continue
            if event["event_type"] != event_type:
                continue
            if resource_id is not None and event["resource_id"] != resource_id:
                continue
            count += 1
        return count


@pytest_asyncio.fixture
async def env():  # noqa: ANN201
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = build_session_factory(engine)
    harness = SimpleNamespace(
        session_factory=session_factory,
        storage=InMemoryObjectStorage(),
        jobs=SACharacterBackupJobRepository(session_factory),
        reader=SACharacterBackupExportReader(session_factory),
        gate=_GateStub(),
        usage=_UsageStub(),
    )
    harness.gate.usage = harness.usage
    try:
        yield harness
    finally:
        await engine.dispose()


def _import_service(env, **overrides) -> CharacterBackupImportService:
    kwargs = dict(
        job_repository=env.jobs,
        restore_writer=SACharacterBackupRestoreWriter(env.session_factory),
        export_reader=env.reader,
        object_storage=env.storage,
        character_service=env.gate,
        account_runtime_usage_repository=env.usage,
    )
    kwargs.update(overrides)
    return CharacterBackupImportService(**kwargs)


async def _seed_full_character(env) -> dict[str, str]:
    """The CB2 test's representative character (media + NSFW rows +
    schedule + private seed), owned by EXPORTER."""
    now = datetime.now(timezone.utc)
    stage = await env.storage.put_bytes(
        object_key="characters/char-1/stage0.png",
        content=b"stage-image-bytes",
        content_type="image/png",
    )
    chat_upload = await env.storage.put_bytes(
        object_key="users/default/chat-uploads/photo.png",
        content=b"chat-upload-bytes",
        content_type="image/png",
    )
    nsfw_upload = await env.storage.put_bytes(
        object_key="users/default/chat-uploads/photo2.png",
        content=b"nsfw-upload-bytes",
        content_type="image/png",
    )
    album = await env.storage.put_bytes(
        object_key="characters/char-1/album/a1.png",
        content=b"album-bytes",
        content_type="image/png",
    )
    feed = await env.storage.put_bytes(
        object_key="feed/char-1/post1.png",
        content=b"feed-bytes",
        content_type="image/png",
    )
    async with env.session_factory() as session:
        session.add(CharacterRow(
            id="char-1",
            user_id=EXPORTER,
            name="芊璃",
            image_urls=json.dumps([stage.url]),
            created_at=now,
        ))
        session.add(ConversationRow(id="conv-1", character_id="char-1"))
        session.add(MessageRow(
            conversation_id="conv-1", position=0, role="user",
            content="午安，今天過得如何？", created_at=now,
            attachments_json=json.dumps([
                {"kind": "image", "url": chat_upload.url,
                 "mime_type": "image/png"},
            ]),
        ))
        session.add(MessageRow(
            conversation_id="conv-1", position=1, role="assistant",
            content=_NSFW_RAW_WITH_SUMMARY, content_mode="nsfw",
            safe_summary="兩人聊得很開心。", created_at=now,
        ))
        session.add(MessageRow(
            conversation_id="conv-1", position=2, role="assistant",
            content=_NSFW_RAW_NO_SUMMARY, content_mode="nsfw",
            created_at=now,
            attachments_json=json.dumps([
                {"kind": "image", "url": nsfw_upload.url,
                 "mime_type": "image/png"},
            ]),
        ))
        session.add(MemoryItemRow(
            id="mem-1", character_id="char-1", kind="fact",
            content="喜歡奶茶。", created_at=now,
        ))
        session.add(MemoryItemRow(
            id="mem-2", character_id="char-1", kind="event",
            content=_NSFW_MEMORY_RAW,
            tags=json.dumps(["content_mode:nsfw"]),
            created_at=now,
        ))
        session.add(CharacterAlbumItemRow(
            id="alb-1", character_id="char-1", url=album.url,
            created_at=now,
        ))
        session.add(FeedPostRow(
            id="post-1", character_id="char-1",
            content_text="今天的天空很好看。",
            image_url=feed.url, created_at=now,
        ))
        session.add(DailyScheduleRow(
            id="sched-1", character_id="char-1", date="2026-08-05",
            generated_at=now,
        ))
        session.add(ScheduleActivityRow(
            id="act-1", schedule_id="sched-1", position=0,
            start_at=now, end_at=now + timedelta(hours=1),
            description="picnic", category="rest",
        ))
        session.add(StorySeedRow(
            id="seed-private", seed_text="她想起舊書店的約定。",
            character_id="char-1", external_id="core:test:001",
            created_at=now, updated_at=now,
        ))
        session.add(StorySeedRow(
            id="seed-global", seed_text="全域種子。",
            character_id=None, created_at=now, updated_at=now,
        ))
        await session.commit()
    return {
        "stage": stage.object_key,
        "chat_upload": chat_upload.object_key,
        "nsfw_upload": nsfw_upload.object_key,
        "album": album.object_key,
        "feed": feed.object_key,
    }


async def _export_artifact(env) -> bytes:
    service = CharacterBackupExportService(
        job_repository=env.jobs,
        export_reader=env.reader,
        object_storage=env.storage,
        app_version="1.2.3-test",
        envelope_params=_FAST_PARAMS,
    )
    job = await service.start_export(
        "char-1", operator_id=EXPORTER, password=PASSWORD,
    )
    await service.wait_until_idle()
    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED
    return await env.storage.get_bytes(
        object_key=stored.artifact_object_key,
    )


async def _reexport_artifact(env, character_id: str, operator_id: str) -> bytes:
    """Export a *landed* character again (owned by the importer) — used to
    prove no NSFW original survived the hosted fold anywhere at rest."""
    service = CharacterBackupExportService(
        job_repository=env.jobs,
        export_reader=env.reader,
        object_storage=env.storage,
        app_version="1.2.3-test",
        envelope_params=_FAST_PARAMS,
    )
    job = await service.start_export(
        character_id, operator_id=operator_id, password=PASSWORD,
    )
    await service.wait_until_idle()
    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error
    return await env.storage.get_bytes(
        object_key=stored.artifact_object_key,
    )


async def _stage(service, data: bytes, *, operator_id: str = IMPORTER) -> str:
    async def _chunks():
        step = 64 * 1024
        for start in range(0, len(data), step):
            yield data[start:start + step]

    staged = await service.stage_upload(_chunks(), operator_id=operator_id)
    return staged.object_key


async def _run_import(env, service, staged_key: str) -> CharacterBackupJob:
    job = await service.start_import(
        staged_object_key=staged_key,
        operator_id=IMPORTER,
        password=PASSWORD,
    )
    await service.wait_until_idle()
    stored = await env.jobs.get(job.id)
    assert stored is not None
    return stored


async def _rows_for(env, table: str, character_id: str) -> list:
    rule = BACKUP_TABLE_RULES_BY_NAME[table]
    return [
        record
        async for record in env.reader.stream_rows(
            rule, character_id=character_id,
        )
    ]


# ---------------------------------------------------------------------------
# 驗收 1 — round-trip：逐表筆數與內容、全新 id、operator 重映射、URL 改寫
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_restores_every_table_with_fresh_ids(env) -> None:  # noqa: ANN001
    media_keys = await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env)

    staged_key = await _stage(service, artifact)
    assert staged_key.startswith(EPHEMERAL_OBJECT_KEY_PREFIXES[1])
    assert "/staging/" in staged_key

    stored = await _run_import(env, service, staged_key)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error
    assert stored.kind == BACKUP_JOB_KIND_RESTORE
    new_id = stored.character_id
    assert new_id and new_id != "char-1"
    assert dict(stored.payload) == {}  # 終態即抹除

    # 逐表筆數（self-host：不折疊）。
    expected = {
        "characters": 1, "conversations": 1, "messages": 3,
        "memory_items": 2, "character_album_items": 1, "feed_posts": 1,
        "daily_schedules": 1, "schedule_activities": 1, "story_seeds": 1,
    }
    for rule in carried_table_rules():
        rows = await _rows_for(env, rule.table, new_id)
        assert len(rows) == expected.get(rule.table, 0), rule.table

    # 全新 id ＋ operator 重映射到匯入者。
    (character,) = await _rows_for(env, "characters", new_id)
    assert character.id == new_id
    assert character.user_id == IMPORTER
    (conversation,) = await _rows_for(env, "conversations", new_id)
    assert conversation.id != "conv-1"
    memory_ids = {
        row.id for row in await _rows_for(env, "memory_items", new_id)
    }
    assert memory_ids and not memory_ids & {"mem-1", "mem-2"}
    (seed,) = await _rows_for(env, "story_seeds", new_id)
    assert seed.id != "seed-private"
    assert seed.external_id is None  # 私有種子不奪 pack identity
    (activity,) = await _rows_for(env, "schedule_activities", new_id)
    assert activity.id != "act-1"

    # 原角色（匯出方）不動。
    original_rows = await _rows_for(env, "characters", "char-1")
    assert len(original_rows) == 1

    # 媒體：新 key、新 URL、原位元組；變體規則見專屬測試。
    image_urls = json.loads(character.image_urls)
    assert len(image_urls) == 1
    new_stage_key = env.storage.object_key_from_url(image_urls[0])
    assert new_stage_key == f"characters/{new_id}/stage0.png"
    assert await env.storage.get_bytes(object_key=new_stage_key) == (
        b"stage-image-bytes"
    )
    (album_item,) = await _rows_for(env, "character_album_items", new_id)
    assert env.storage.object_key_from_url(album_item.url) == (
        f"characters/{new_id}/album/a1.png"
    )
    (post,) = await _rows_for(env, "feed_posts", new_id)
    assert env.storage.object_key_from_url(post.image_url) == (
        f"feed/{new_id}/post1.png"
    )
    messages = await _rows_for(env, "messages", new_id)
    attachments = json.loads(messages[0].attachments_json)
    attachment_key = env.storage.object_key_from_url(attachments[0]["url"])
    assert attachment_key == (
        f"users/{IMPORTER}/restored/{new_id}/chat-uploads/photo.png"
    )
    assert await env.storage.get_bytes(object_key=attachment_key) == (
        b"chat-upload-bytes"
    )
    # 舊 key 的原檔完好（同實例重匯不還原到原位）。
    assert await env.storage.get_bytes(
        object_key=media_keys["chat_upload"],
    ) == b"chat-upload-bytes"

    # 閘記帳＋staged 消耗。
    assert env.gate.recorded == [new_id]
    assert await env.storage.stat(object_key=staged_key) is None

    # self-host 不折疊：NSFW 訊息的附件照搬。
    nsfw_attachments = json.loads(messages[2].attachments_json)
    assert env.storage.object_key_from_url(nsfw_attachments[0]["url"]) == (
        f"users/{IMPORTER}/restored/{new_id}/chat-uploads/photo2.png"
    )

    report = stored.progress["report"]
    assert report["media_transferred"] == 5
    assert report["media_missing"] == []
    assert report["rows_landed"]["messages"] == 3
    assert report["embedding"]["status"] == "pending"


# ---------------------------------------------------------------------------
# 驗收 2 — cloud_mode：零 NSFW 原文落地；預覽==manifest==實際
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_mode_folds_and_counts_match_preview(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env, cloud_mode=True)
    staged_key = await _stage(service, artifact)

    preview = await service.preview(
        staged_object_key=staged_key,
        operator_id=IMPORTER,
        password=PASSWORD,
    )
    assert isinstance(preview, CharacterBackupImportPreview)
    assert preview.character_name == "芊璃"
    assert preview.will_collapse_nsfw is True
    assert preview.nsfw_collapse.messages_replaced == 1
    assert preview.nsfw_collapse.messages_dropped == 1
    assert preview.nsfw_collapse.memories_skipped == 1
    assert preview.table_counts["messages"] == 3

    stored = await _run_import(env, service, staged_key)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error
    new_id = stored.character_id

    # 實際折疊數 == 預覽 == manifest。
    report = stored.progress["report"]
    assert report["nsfw_messages_replaced"] == 1
    assert report["nsfw_messages_dropped"] == 1
    assert report["nsfw_memories_skipped"] == 1

    messages = await _rows_for(env, "messages", new_id)
    assert len(messages) == 2  # dropped 那則不落地
    contents = {row.content for row in messages}
    assert "兩人聊得很開心。" in contents  # safe_summary 取代原文
    assert _NSFW_RAW_WITH_SUMMARY not in contents
    assert _NSFW_RAW_NO_SUMMARY not in contents
    assert all(row.content_mode == "normal" for row in messages)

    memories = await _rows_for(env, "memory_items", new_id)
    assert len(memories) == 1
    assert memories[0].content == "喜歡奶茶。"
    assert _NSFW_MEMORY_RAW not in {row.content for row in memories}

    # dropped 訊息的附件（photo2）不上傳；normal 訊息的附件照常。
    restored_prefix = f"users/{IMPORTER}/restored/{new_id}/"
    restored_keys = [
        key for key in env.storage._objects  # noqa: SLF001 — test seam
        if key.startswith(restored_prefix)
    ]
    assert restored_keys == [
        f"{restored_prefix}chat-uploads/photo.png",
    ]
    # stage + chat_upload + album + feed；NSFW 附件缺席。
    assert report["media_transferred"] == 4


@pytest.mark.asyncio
async def test_self_host_preview_does_not_promise_folding(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env)
    staged_key = await _stage(service, artifact)
    preview = await service.preview(
        staged_object_key=staged_key,
        operator_id=IMPORTER,
        password=PASSWORD,
    )
    assert preview.will_collapse_nsfw is False
    # 統計仍在（manifest 事實），只是不會被套用。
    assert preview.nsfw_collapse.messages_replaced == 1


# ---------------------------------------------------------------------------
# D5 補洞 — operator_profile_fields ／ pending_follow_ups 也是 NSFW 原文載體
# ---------------------------------------------------------------------------


async def _seed_nsfw_carrier_character(env) -> None:
    """A character carrying the two *non-message* NSFW carriers the CB
    spec-review flagged, each with one nsfw-flagged row + one normal row:

    - ``operator_profile_fields.content_mode`` — the extracted operator
      fact lands verbatim in ``value`` (no safe form → whole-row skip),
    - ``pending_follow_ups.messages_json`` — verbatim queued turns plus
      the row's reply free-text (``resolved_message``) are the same nsfw
      exchange (whole-row skip).
    """
    now = datetime.now(timezone.utc)
    async with env.session_factory() as session:
        session.add(CharacterRow(
            id="char-1", user_id=EXPORTER, name="芊璃", created_at=now,
        ))
        session.add(ConversationRow(id="conv-1", character_id="char-1"))
        session.add(OperatorProfileFieldRow(
            id="opf-nsfw", character_id="char-1", operator_id=EXPORTER,
            layer=3, field_key="intimate_fact",
            value=_NSFW_PROFILE_FIELD_RAW, confidence=0.9,
            content_mode="nsfw", created_at=now, updated_at=now,
        ))
        session.add(OperatorProfileFieldRow(
            id="opf-normal", character_id="char-1", operator_id=EXPORTER,
            layer=1, field_key="likes_tea", value="喜歡奶茶。",
            confidence=0.8, content_mode="normal", created_at=now,
            updated_at=now,
        ))
        session.add(PendingFollowUpRow(
            id="pfu-nsfw", character_id="char-1", conversation_id="conv-1",
            brief_reply="等等回你。",
            # ensure_ascii=False mirrors the production repository serializer
            # (raw CJK at rest), so the raw-text assertions are meaningful.
            messages_json=json.dumps([
                {"content": _NSFW_FOLLOW_UP_QUEUED_RAW, "content_mode": "nsfw",
                 "queued_at": now.isoformat()},
            ], ensure_ascii=False),
            resolved_message=_NSFW_FOLLOW_UP_RESOLVED_RAW,
            scheduled_for=now, queued_at=now, updated_at=now,
        ))
        session.add(PendingFollowUpRow(
            id="pfu-normal", character_id="char-1", conversation_id="conv-1",
            brief_reply="待會兒聊。",
            messages_json=json.dumps([
                {"content": "晚點記得提醒我", "content_mode": "normal",
                 "queued_at": now.isoformat()},
            ], ensure_ascii=False),
            scheduled_for=now, queued_at=now, updated_at=now,
        ))
        await session.commit()


def _archive_text(artifact: bytes) -> str:
    """The decrypted inner zip's raw bytes as text — used to prove no
    NSFW original survives folding anywhere in the landed archive."""
    plain = io.BytesIO()
    decrypt_stream(io.BytesIO(artifact), plain, password=PASSWORD)
    return plain.getvalue().decode("utf-8", errors="ignore")


@pytest.mark.asyncio
async def test_cloud_skips_nsfw_profile_fields_and_follow_ups(env) -> None:  # noqa: ANN001
    """cloud_mode: the nsfw operator-profile-field and pending-follow-up
    rows never land at rest (D5); the normal siblings do. Preview ==
    manifest == actual fold counts."""
    await _seed_nsfw_carrier_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env, cloud_mode=True)
    staged_key = await _stage(service, artifact)

    preview = await service.preview(
        staged_object_key=staged_key,
        operator_id=IMPORTER,
        password=PASSWORD,
    )
    assert preview.will_collapse_nsfw is True
    assert preview.nsfw_collapse.operator_profile_fields_skipped == 1
    assert preview.nsfw_collapse.pending_follow_ups_skipped == 1
    assert preview.table_counts["operator_profile_fields"] == 2
    assert preview.table_counts["pending_follow_ups"] == 2

    stored = await _run_import(env, service, staged_key)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error
    new_id = stored.character_id

    report = stored.progress["report"]
    assert report["nsfw_operator_profile_fields_skipped"] == 1
    assert report["nsfw_pending_follow_ups_skipped"] == 1

    # Only the normal rows landed.
    profile_fields = await _rows_for(env, "operator_profile_fields", new_id)
    assert [f.field_key for f in profile_fields] == ["likes_tea"]
    assert all(f.content_mode == "normal" for f in profile_fields)
    assert _NSFW_PROFILE_FIELD_RAW not in {f.value for f in profile_fields}

    follow_ups = await _rows_for(env, "pending_follow_ups", new_id)
    assert len(follow_ups) == 1
    assert _NSFW_FOLLOW_UP_QUEUED_RAW not in follow_ups[0].messages_json
    assert (follow_ups[0].resolved_message or "") != _NSFW_FOLLOW_UP_RESOLVED_RAW

    # And nothing NSFW survives anywhere in a re-export of the landed data.
    reexport = await _reexport_artifact(env, new_id, IMPORTER)
    landed_text = _archive_text(reexport)
    assert _NSFW_PROFILE_FIELD_RAW not in landed_text
    assert _NSFW_FOLLOW_UP_QUEUED_RAW not in landed_text
    assert _NSFW_FOLLOW_UP_RESOLVED_RAW not in landed_text


@pytest.mark.asyncio
async def test_self_host_keeps_nsfw_profile_fields_and_follow_ups(env) -> None:  # noqa: ANN001
    """self-host (cloud_mode=False): both carriers land verbatim — the
    fold is a hosted-only policy, never a format-layer erasure."""
    await _seed_nsfw_carrier_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env)  # cloud_mode=False
    staged_key = await _stage(service, artifact)

    stored = await _run_import(env, service, staged_key)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error
    new_id = stored.character_id

    report = stored.progress["report"]
    assert report["nsfw_operator_profile_fields_skipped"] == 0
    assert report["nsfw_pending_follow_ups_skipped"] == 0

    profile_fields = await _rows_for(env, "operator_profile_fields", new_id)
    assert len(profile_fields) == 2
    assert _NSFW_PROFILE_FIELD_RAW in {f.value for f in profile_fields}

    follow_ups = await _rows_for(env, "pending_follow_ups", new_id)
    assert len(follow_ups) == 2
    assert any(
        _NSFW_FOLLOW_UP_QUEUED_RAW in f.messages_json for f in follow_ups
    )
    assert any(
        (f.resolved_message or "") == _NSFW_FOLLOW_UP_RESOLVED_RAW
        for f in follow_ups
    )


# ---------------------------------------------------------------------------
# 驗收 3 — 中途失敗不留半個角色
# ---------------------------------------------------------------------------


class _MediaRefusingStorage(InMemoryObjectStorage):
    """Refuses the media re-puts (real prefixes) but allows staging."""

    async def put_bytes(self, **kwargs):  # noqa: ANN003, ANN201
        key = kwargs["object_key"]
        if not key.startswith("character-backups/"):
            raise RuntimeError("media store down")
        return await super().put_bytes(**kwargs)


@pytest.mark.asyncio
async def test_failure_mid_restore_leaves_nothing(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    artifact = await _export_artifact(env)

    refusing = _MediaRefusingStorage()
    # The archive fetch reads from the same adapter — mirror the objects.
    for key, data in env.storage._objects.items():  # noqa: SLF001
        await InMemoryObjectStorage.put_bytes(
            refusing, object_key=key, content=data,
            content_type="application/octet-stream",
        )
    service = _import_service(env, object_storage=refusing)
    staged_key = await _stage(service, artifact)
    keys_before = set(refusing._objects)  # noqa: SLF001

    stored = await _run_import(env, service, staged_key)
    assert stored.status == BACKUP_JOB_STATUS_FAILED
    assert stored.error is not None and "media store down" in stored.error
    assert dict(stored.payload) == {}
    failed_id = stored.character_id
    assert failed_id  # bound early so cleanup has its anchor

    # 逐表查無新角色蹤跡。
    for rule in carried_table_rules():
        assert await _rows_for(env, rule.table, failed_id) == [], rule.table
    async with env.session_factory() as session:
        count = await session.execute(
            select(func.count()).select_from(
                Base.metadata.tables["characters"],
            ),
        )
        assert count.scalar_one() == 1  # 只剩匯出方的 char-1

    # storage 無新物件（staged 檔仍在，交給 TTL）。
    assert set(refusing._objects) == keys_before  # noqa: SLF001
    # 沒收錢：失敗不記 create event。
    assert env.gate.recorded == []


# ---------------------------------------------------------------------------
# 驗收 4 — 槽位/日限閘、錯誤文法、staging 前綴
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slot_wall_refuses_at_confirm_time(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    env.gate.error = CharacterValidationError(
        "account runtime profile character limit reached (3)",
    )
    service = _import_service(env)
    staged_key = await _stage(service, artifact)
    with pytest.raises(CharacterValidationError):
        await service.start_import(
            staged_object_key=staged_key,
            operator_id=IMPORTER,
            password=PASSWORD,
        )
    # 沒有 job 被排進去。
    assert await env.jobs.list_running() == []


@pytest.mark.asyncio
async def test_stage_upload_rejects_non_magic_and_oversize(env) -> None:  # noqa: ANN001
    service = _import_service(env)

    async def _bad():
        yield b"PK\x03\x04 definitely-a-zip-not-an-envelope"

    with pytest.raises(CharacterBackupInvalidFileError):
        await service.stage_upload(_bad(), operator_id=IMPORTER)

    small = _import_service(env, import_max_bytes=16)

    async def _big():
        yield b"LUMEBAK1"
        yield b"x" * 64

    with pytest.raises(CharacterBackupImportTooLargeError):
        await small.stage_upload(_big(), operator_id=IMPORTER)


@pytest.mark.asyncio
async def test_preview_error_grammar(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env)
    staged_key = await _stage(service, artifact)

    with pytest.raises(BackupWrongPasswordError):
        await service.preview(
            staged_object_key=staged_key,
            operator_id=IMPORTER,
            password="not-the-password",
        )

    # 竄改 payload（header 之後）→ 整檔拒收。
    tampered = bytearray(artifact)
    tampered[HEADER_LEN + 32] ^= 0xFF
    tampered_key = await _stage(service, bytes(tampered))
    with pytest.raises(BackupIntegrityError):
        await service.preview(
            staged_object_key=tampered_key,
            operator_id=IMPORTER,
            password=PASSWORD,
        )

    # 別人的 staged key 一律當作不存在。
    with pytest.raises(CharacterBackupStagedFileNotFoundError):
        await service.preview(
            staged_object_key=staged_key.replace(
                f"/{IMPORTER}/", "/mallory/",
            ),
            operator_id="mallory",
            password=PASSWORD,
        )


@pytest.mark.asyncio
async def test_preview_rejects_a_newer_schema(env) -> None:  # noqa: ANN001
    service = _import_service(env)
    inner = io.BytesIO()
    with BackupArchiveWriter(inner) as writer:
        writer.write_manifest(json.dumps({
            "schema_version": 999, "character_id": "x",
        }))
    inner.seek(0)
    encrypted = io.BytesIO()
    encrypt_stream(
        inner, encrypted, password=PASSWORD, params=_FAST_PARAMS,
    )
    staged_key = await _stage(service, encrypted.getvalue())
    with pytest.raises(CharacterBackupSchemaTooNewError):
        await service.preview(
            staged_object_key=staged_key,
            operator_id=IMPORTER,
            password=PASSWORD,
        )


# ---------------------------------------------------------------------------
# 啟動恢復：清理後整個重跑
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_reruns_an_interrupted_restore(env) -> None:  # noqa: ANN001
    import base64

    from kokoro_link.infrastructure.security.backup_cipher import (
        verify_and_unwrap,
    )

    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env)
    staged_key = await _stage(service, artifact)
    unwrapped = verify_and_unwrap(io.BytesIO(artifact), password=PASSWORD)

    job = CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_RESTORE,
        operator_id=IMPORTER,
        payload={
            "file_key_b64": base64.b64encode(
                unwrapped.file_key,
            ).decode("ascii"),
            "staged_object_key": staged_key,
        },
    ).with_artifact(staged_key)
    await env.jobs.add(job)

    report = await service.recover()
    await service.wait_until_idle()
    assert report["resumed"] == 1

    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error
    assert stored.attempts == 2
    assert stored.character_id
    rows = await _rows_for(env, "characters", stored.character_id)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_recover_fails_terminally_when_staged_upload_expired(env) -> None:  # noqa: ANN001
    service = _import_service(env)
    job = CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_RESTORE,
        operator_id=IMPORTER,
        payload={
            "file_key_b64": "eA==",
            "staged_object_key": (
                f"character-backups/{IMPORTER}/staging/gone.lumebackup"
            ),
        },
    )
    await env.jobs.add(job)
    report = await service.recover()
    assert report["failed"] == 1
    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_FAILED
    assert "expired" in (stored.error or "")


# ---------------------------------------------------------------------------
# 變體 fan-out：restore put 走非 ephemeral 前綴 → 裝飾器自動重生
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_put_triggers_variant_fanout(env) -> None:  # noqa: ANN001
    pytest.importorskip("PIL")
    from PIL import Image

    from kokoro_link.infrastructure.storage.image_variants import (
        derive_variant_key,
    )
    from kokoro_link.infrastructure.storage.variant_aware import (
        VariantAwareObjectStorage,
    )

    # Reseed the character's stage image with a real decodable PNG.
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 120, 40)).save(buffer, format="PNG")
    png = buffer.getvalue()
    now = datetime.now(timezone.utc)
    stage = await env.storage.put_bytes(
        object_key="characters/char-1/stage0.png",
        content=png,
        content_type="image/png",
    )
    async with env.session_factory() as session:
        session.add(CharacterRow(
            id="char-1", user_id=EXPORTER, name="芊璃",
            image_urls=json.dumps([stage.url]), created_at=now,
        ))
        await session.commit()

    artifact = await _export_artifact(env)
    service = _import_service(
        env, object_storage=VariantAwareObjectStorage(env.storage),
    )
    staged_key = await _stage(service, artifact)
    stored = await _run_import(env, service, staged_key)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error

    new_key = f"characters/{stored.character_id}/stage0.png"
    assert await env.storage.get_bytes(object_key=new_key) == png
    # The decorator regenerated at least the unconditional "full" tier.
    assert await env.storage.stat(
        object_key=derive_variant_key(new_key, "full"),
    ) is not None
    # …while the staged upload itself (ephemeral prefix) never fanned out.
    assert await env.storage.stat(
        object_key=derive_variant_key(staged_key, "full"),
    ) is None


# ---------------------------------------------------------------------------
# A1 — 跨 replica recover fencing（per-job lease）＋ finalize 輸掉 CAS 的補刪
# ---------------------------------------------------------------------------


def _lease_pair(prefix: str = "backup:"):
    """Two replicas' lease views over ONE shared backend."""
    from kokoro_link.application.services.studio_execution_lease import (
        StudioExecutionLease,
    )
    from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
        InMemoryBackgroundCoordinatorLease,
    )

    backend = InMemoryBackgroundCoordinatorLease()
    return (
        StudioExecutionLease(
            backend, owner_id="replica-a", name_prefix=prefix,
        ),
        StudioExecutionLease(
            backend, owner_id="replica-b", name_prefix=prefix,
        ),
    )


@pytest.mark.asyncio
async def test_recover_skips_a_restore_leased_by_another_replica(env) -> None:  # noqa: ANN001
    """A1 reproduction: during a drain roll, replica A is still landing
    rows for a running restore when replica B restarts. B's recover()
    must NOT tear down A's in-flight landing — the per-job lease held by
    A makes B skip the job entirely."""
    replica_a, replica_b = _lease_pair()
    service = _import_service(env, execution_lease=replica_b)

    now = datetime.now(timezone.utc)
    async with env.session_factory() as session:
        session.add(CharacterRow(
            id="half-landed", user_id=IMPORTER, name="landing…",
            created_at=now,
        ))
        await session.commit()
    job = CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_RESTORE,
        operator_id=IMPORTER,
        payload={
            "file_key_b64": "eA==",
            "staged_object_key": (
                f"character-backups/{IMPORTER}/staging/live.lumebackup"
            ),
        },
    ).with_character_id("half-landed")
    await env.jobs.add(job)
    # Replica A is mid-restore: it holds the job's lease.
    assert await replica_a.acquire(job.id) is not None

    report = await service.recover()
    await service.wait_until_idle()

    assert report == {"resumed": 0, "failed": 0, "lease_skipped": 1}
    # A 的落地資料一根汗毛都不能少；job 也不能被動過。
    async with env.session_factory() as session:
        assert await session.get(CharacterRow, "half-landed") is not None
    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_RUNNING
    assert stored.attempts == 1


@pytest.mark.asyncio
async def test_recover_still_reruns_when_the_lease_is_free(env) -> None:  # noqa: ANN001
    """The fencing must not break genuine crash recovery: a dead
    process's lease is gone (in-process backend) / expired, so recover
    proceeds exactly as before."""
    import base64

    from kokoro_link.infrastructure.security.backup_cipher import (
        verify_and_unwrap,
    )

    _replica_a, replica_b = _lease_pair()
    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env, execution_lease=replica_b)
    staged_key = await _stage(service, artifact)
    unwrapped = verify_and_unwrap(io.BytesIO(artifact), password=PASSWORD)

    job = CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_RESTORE,
        operator_id=IMPORTER,
        payload={
            "file_key_b64": base64.b64encode(
                unwrapped.file_key,
            ).decode("ascii"),
            "staged_object_key": staged_key,
        },
    ).with_artifact(staged_key)
    await env.jobs.add(job)

    report = await service.recover()
    await service.wait_until_idle()
    assert report["resumed"] == 1
    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error


@pytest.mark.asyncio
async def test_success_finalize_race_removes_this_runs_landing(env) -> None:  # noqa: ANN001
    """A1 second half: ``finalize_scrubbed`` returning ``False`` means
    another finalizer won — this run's landed rows / media are orphans
    and must be torn down, never silently ignored."""
    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env)
    staged_key = await _stage(service, artifact)
    keys_before = set(env.storage._objects)  # noqa: SLF001

    async def _deny_finalize(job):  # noqa: ANN001, ANN202
        return False  # 另一個 finalizer 先到

    env.jobs.finalize_scrubbed = _deny_finalize

    job = await service.start_import(
        staged_object_key=staged_key,
        operator_id=IMPORTER,
        password=PASSWORD,
    )
    await service.wait_until_idle()

    # 本輪 ledger 已記（發生在 finalize 之前），從它拿到新角色 id。
    assert env.gate.recorded
    new_id = env.gate.recorded[0]
    for rule in carried_table_rules():
        assert await _rows_for(env, rule.table, new_id) == [], rule.table
    # 媒體補刪：storage 回到 staging 後的狀態（staged 檔也不消耗）。
    assert set(env.storage._objects) == keys_before  # noqa: SLF001
    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_RUNNING  # 這輪沒有 finalize 成功


# ---------------------------------------------------------------------------
# A3 — 落地期間凍結（reason=restore），成功收尾還原匯出時的凍結狀態
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_lands_character_frozen_then_unfreezes_on_success(env) -> None:  # noqa: ANN001
    """A3 reproduction: the character row is visible after the first row
    batch, minutes before its data follows. It must sit frozen under the
    ``restore`` provenance for the whole landing and come out unfrozen
    once the restore succeeds."""
    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env)
    staged_key = await _stage(service, artifact)

    observed: dict = {}
    pipeline = service._pipeline  # noqa: SLF001
    original_transfer = pipeline.transfer_media

    async def _spying_transfer(reader, ctx, uploaded_keys):  # noqa: ANN001, ANN202
        # rows 已落、restore 未完 —— 正是玩家已能看到角色的窗口。
        async with env.session_factory() as session:
            row = await session.get(CharacterRow, ctx.new_character_id)
            observed["frozen"] = row.frozen
            observed["reason"] = row.frozen_reason
        return await original_transfer(reader, ctx, uploaded_keys)

    pipeline.transfer_media = _spying_transfer

    stored = await _run_import(env, service, staged_key)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error

    assert observed == {"frozen": True, "reason": FREEZE_REASON_RESTORE}
    async with env.session_factory() as session:
        row = await session.get(CharacterRow, stored.character_id)
        assert row.frozen is False
        assert row.frozen_at is None
        assert row.frozen_reason is None


@pytest.mark.asyncio
async def test_restore_preserves_the_exported_freeze_state(env) -> None:  # noqa: ANN001
    """A deliberately frozen character comes back frozen (the DTO carries
    the columns on purpose) — the A3 restore-freeze must not erase it."""
    await _seed_full_character(env)
    frozen_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    async with env.session_factory() as session:
        row = await session.get(CharacterRow, "char-1")
        row.frozen = True
        row.frozen_at = frozen_at
        row.frozen_reason = "manual"
        await session.commit()

    artifact = await _export_artifact(env)
    service = _import_service(env)
    staged_key = await _stage(service, artifact)
    stored = await _run_import(env, service, staged_key)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error

    async with env.session_factory() as session:
        restored = await session.get(CharacterRow, stored.character_id)
        assert restored.frozen is True
        assert restored.frozen_reason == "manual"
        assert restored.frozen_at is not None


# ---------------------------------------------------------------------------
# A4 — crash 在 finalize 前：staged 檔不得先刪、ledger 以 job.id 冪等
# ---------------------------------------------------------------------------


class _SimulatedCrash(BaseException):
    """Escapes ``except Exception`` exactly like a process death would —
    no failure cleanup, no finalize, the job row stays ``running``."""


@pytest.mark.asyncio
async def test_crash_before_finalize_keeps_staged_and_dedupes_ledger(env) -> None:  # noqa: ANN001
    """A4 reproduction (both halves in one crash):

    - the staged upload must still exist when recovery reruns the job —
      deleting it before the finalize destroyed completed restores;
    - the rerun must not consume a second daily-create ledger slot —
      the event is keyed on the job id and probed before recording."""
    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env)
    staged_key = await _stage(service, artifact)

    real_finalize = service._finalize  # noqa: SLF001
    crashed = {"n": 0}

    async def _crashing_finalize(job, status, *, error=None):  # noqa: ANN001, ANN202
        if crashed["n"] == 0:
            crashed["n"] += 1
            raise _SimulatedCrash("died right before the terminal CAS")
        return await real_finalize(job, status, error=error)

    service._finalize = _crashing_finalize

    job = await service.start_import(
        staged_object_key=staged_key,
        operator_id=IMPORTER,
        password=PASSWORD,
    )
    try:
        await service.wait_until_idle()
    except BaseException:  # noqa: BLE001 — 模擬 crash 可能穿出 gather
        pass

    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_RUNNING  # 沒 finalize 成
    # A4b：staged 檔必須還在，否則 recovery 只能銷毀已完成的還原。
    assert await env.storage.stat(object_key=staged_key) is not None
    assert len(env.gate.recorded) == 1

    service._finalize = real_finalize  # noqa: SLF001
    report = await service.recover()
    await service.wait_until_idle()
    assert report["resumed"] == 1

    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error
    # A4a：重跑冪等 —— 每日創角 ledger 只吃一格，且鍵是 job id。
    creates = [
        event for event in env.usage.events
        if event["event_type"] == "character_create"
    ]
    assert len(creates) == 1
    assert creates[0]["resource_id"] == job.id
    assert len(env.gate.recorded) == 1  # 第二輪被冪等判斷擋下
    # staged 檔在成功 finalize 之後才消耗。
    assert await env.storage.stat(object_key=staged_key) is None


# ---------------------------------------------------------------------------
# A5 — GB 級 scan / land 不得凍住 event loop
# ---------------------------------------------------------------------------


def _all_dropped_messages_archive(count: int):  # noqa: ANN202
    """A pure-CPU inner zip: ``count`` NSFW-no-summary message rows (all
    fold to DROPPED on cloud) and nothing else — zero natural awaits."""
    line = json.dumps({
        "id": 1,
        "conversation_id": "conv-x",
        "position": 0,
        "role": "assistant",
        "content": _NSFW_RAW_NO_SUMMARY,
        "content_mode": "nsfw",
        "created_at": "2026-08-05T00:00:00+00:00",
    }, ensure_ascii=False).encode("utf-8")
    buffer = io.BytesIO()
    with BackupArchiveWriter(buffer) as writer:
        with writer.open_data_file("messages.jsonl") as sink:
            for _ in range(count):
                sink.write(line + b"\n")
    buffer.seek(0)
    return buffer


async def _ticks_during(coro):  # noqa: ANN001, ANN202
    """Run ``coro`` alongside a spinning ticker; how often did the event
    loop get control while it ran?"""
    ticks = 0
    running = True

    async def _ticker() -> None:
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(_ticker())
    await asyncio.sleep(0)  # let the ticker spin up
    baseline = ticks
    try:
        result = await coro
    finally:
        running = False
        await ticker
    return result, ticks - baseline


@pytest.mark.asyncio
async def test_scan_yields_the_event_loop(env) -> None:  # noqa: ANN001
    """A5 reproduction: the scan pass over a large jsonl had zero awaits
    — the whole process (health checks included) froze for its duration.
    The table sweep now runs off-thread, so the loop keeps serving."""
    from kokoro_link.application.dto.character_backup import (
        BACKUP_SCHEMA_VERSION,
        CharacterBackupManifest,
    )
    from kokoro_link.application.services.character_backup_restore_pipeline import (
        CharacterRestorePipeline,
    )
    from kokoro_link.infrastructure.character_backup.packager import (
        BackupArchiveReader,
    )

    buffer = _all_dropped_messages_archive(1200)
    pipeline = CharacterRestorePipeline(
        object_storage=env.storage, cloud_mode=True,
    )
    manifest = CharacterBackupManifest(
        schema_version=BACKUP_SCHEMA_VERSION, character_id="char-x",
    )
    with BackupArchiveReader(buffer) as reader:
        _ctx, ticks = await _ticks_during(
            pipeline.scan(reader, manifest, operator_id=IMPORTER),
        )
    assert ticks >= 1, "scan 進行中 event loop 從未被讓渡"


@pytest.mark.asyncio
async def test_land_rows_yields_even_when_every_row_folds_away(env) -> None:  # noqa: ANN001
    """A5 reproduction, landing half: a stretch of rows that all fold
    away (hosted NSFW drops) fills no insert batch, so the natural flush
    awaits never happen — the loop must still be yielded periodically."""
    from kokoro_link.application.services.character_backup_restore_pipeline import (
        CharacterRestorePipeline,
        RestoreContext,
    )
    from kokoro_link.infrastructure.character_backup.packager import (
        BackupArchiveReader,
    )

    class _NullLanding:
        def reference_columns(self, table):  # noqa: ANN001, ANN202
            return {}

        async def existing_ids(self, table, ids):  # noqa: ANN001, ANN202
            return set()

        async def insert_rows(self, table, rows):  # noqa: ANN001, ANN202
            return (len(rows), 0)

    buffer = _all_dropped_messages_archive(1200)
    pipeline = CharacterRestorePipeline(
        object_storage=env.storage, cloud_mode=True,
    )
    ctx = RestoreContext(
        new_character_id="new-char",
        old_character_id="char-x",
        importer_id=IMPORTER,
        id_map={},
        old_operator_ids=set(),
        url_map={},
        transfers={},
    )
    with BackupArchiveReader(buffer) as reader:
        _result, ticks = await _ticks_during(
            pipeline.land_rows(reader, ctx, _NullLanding()),
        )
    assert ctx.report.nsfw_messages_dropped == 1200
    assert ticks >= 2, "全折疊的表讓 land_rows 一路零讓渡"


# ---------------------------------------------------------------------------
# A6 — hosted 匯入上傳限速（CB2 匯出限速同構）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hosted_upload_throttle_trips_past_the_daily_limit(env) -> None:  # noqa: ANN001
    service = _import_service(
        env, cloud_mode=True, hosted_daily_upload_limit=1,
    )
    staged = await _stage(service, b"LUMEBAK1" + b"x" * 16)
    assert staged
    uploads = [
        event for event in env.usage.events
        if event["event_type"] == "character_backup_upload"
    ]
    assert len(uploads) == 1

    with pytest.raises(CharacterBackupUploadRateLimitedError):
        await _stage(service, b"LUMEBAK1" + b"x" * 16)


@pytest.mark.asyncio
async def test_hosted_upload_without_a_ledger_fails_closed(env) -> None:  # noqa: ANN001
    service = _import_service(
        env, cloud_mode=True, account_runtime_usage_repository=None,
    )
    with pytest.raises(CharacterBackupUploadLedgerNotConfiguredError):
        await _stage(service, b"LUMEBAK1" + b"x" * 16)


@pytest.mark.asyncio
async def test_self_host_upload_is_unmetered(env) -> None:  # noqa: ANN001
    service = _import_service(env)  # cloud_mode=False
    for _ in range(3):
        await _stage(service, b"LUMEBAK1" + b"x" * 8)
    assert not [
        event for event in env.usage.events
        if event["event_type"] == "character_backup_upload"
    ]


# ---------------------------------------------------------------------------
# S4 — hosted preview throttle + single KDF (DoS surface)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hosted_preview_throttle_trips_past_the_daily_limit(env) -> None:  # noqa: ANN001
    """S4 reproduction: preview was unthrottled while each call downloads
    the whole staged archive and runs a scrypt KDF — a per-call amplifier.
    A hosted operator now hits a per-operator 24h cap, mirroring upload."""
    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(
        env, cloud_mode=True, hosted_daily_preview_limit=1,
    )
    staged_key = await _stage(service, artifact)

    preview = await service.preview(
        staged_object_key=staged_key,
        operator_id=IMPORTER,
        password=PASSWORD,
    )
    assert preview.character_name == "芊璃"
    previews = [
        event for event in env.usage.events
        if event["event_type"] == "character_backup_preview"
    ]
    assert len(previews) == 1

    with pytest.raises(CharacterBackupPreviewRateLimitedError):
        await service.preview(
            staged_object_key=staged_key,
            operator_id=IMPORTER,
            password=PASSWORD,
        )


@pytest.mark.asyncio
async def test_wrong_password_preview_still_consumes_a_slot(env) -> None:  # noqa: ANN001
    """S4: a wrong-password preview is NOT cheap — it downloads the whole
    archive and runs one scrypt — so it must consume a throttle slot too,
    else a wrong-password loop bypasses the limit entirely."""
    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(
        env, cloud_mode=True, hosted_daily_preview_limit=1,
    )
    staged_key = await _stage(service, artifact)

    with pytest.raises(BackupWrongPasswordError):
        await service.preview(
            staged_object_key=staged_key,
            operator_id=IMPORTER,
            password="not-the-password",
        )
    # The failed attempt still burned the day's only slot.
    with pytest.raises(CharacterBackupPreviewRateLimitedError):
        await service.preview(
            staged_object_key=staged_key,
            operator_id=IMPORTER,
            password=PASSWORD,
        )


@pytest.mark.asyncio
async def test_hosted_preview_without_a_ledger_fails_closed(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(
        env, cloud_mode=True, account_runtime_usage_repository=None,
    )
    # Stage needs a ledger too, so hand the staged file in directly by
    # staging with a self-host service, then preview with the ledgerless one.
    stager = _import_service(env)
    staged_key = await _stage(stager, artifact)
    with pytest.raises(CharacterBackupUploadLedgerNotConfiguredError):
        await service.preview(
            staged_object_key=staged_key,
            operator_id=IMPORTER,
            password=PASSWORD,
        )


@pytest.mark.asyncio
async def test_self_host_preview_is_unmetered(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env)  # cloud_mode=False
    staged_key = await _stage(service, artifact)
    for _ in range(3):
        await service.preview(
            staged_object_key=staged_key,
            operator_id=IMPORTER,
            password=PASSWORD,
        )
    assert not [
        event for event in env.usage.events
        if event["event_type"] == "character_backup_preview"
    ]


@pytest.mark.asyncio
async def test_preview_runs_the_scrypt_kdf_only_once(env, monkeypatch) -> None:  # noqa: ANN001
    """S4 reproduction: preview verified the password (one scrypt) and then
    decrypted with the password again (a second scrypt) — the KDF is the
    expensive part, so that doubled every preview's cost. It now unwraps the
    file key once and reuses it for the body."""
    from kokoro_link.infrastructure.security import backup_cipher

    await _seed_full_character(env)
    artifact = await _export_artifact(env)
    service = _import_service(env)
    staged_key = await _stage(service, artifact)

    calls = {"n": 0}
    real_derive = backup_cipher._derive_kek  # noqa: SLF001

    def _counting_derive(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls["n"] += 1
        return real_derive(*args, **kwargs)

    monkeypatch.setattr(backup_cipher, "_derive_kek", _counting_derive)

    await service.preview(
        staged_object_key=staged_key,
        operator_id=IMPORTER,
        password=PASSWORD,
    )
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# S6 — a malformed staged key answers 404, never a 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_staged_key_is_not_found_not_a_crash(env) -> None:  # noqa: ANN001
    """S6 reproduction: a ``.../staging/../x`` key clears the prefix check
    yet the storage adapter's own ``validate_object_key`` rejects it as an
    ``ObjectStorageError`` — unmapped by the route, so it 500s. Validate at
    the ownership gate so it collapses into the documented "not found"."""
    service = _import_service(env)
    prefix = service._staging_prefix(IMPORTER)  # noqa: SLF001
    malformed = f"{prefix}../escaped.lumebackup"
    # Sanity: it does clear the naive prefix guard.
    assert malformed.startswith(prefix)

    with pytest.raises(CharacterBackupStagedFileNotFoundError):
        await service.preview(
            staged_object_key=malformed,
            operator_id=IMPORTER,
            password=PASSWORD,
        )
    with pytest.raises(CharacterBackupStagedFileNotFoundError):
        await service.start_import(
            staged_object_key=malformed,
            operator_id=IMPORTER,
            password=PASSWORD,
        )


# ---------------------------------------------------------------------------
# A7 — crash-recovery 媒體清掃只碰 restore 自己的命名空間
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_recovery_sweep_never_deletes_source_media(env) -> None:  # noqa: ANN001
    """A7 reproduction: a fail-soft URL column kept its ORIGINAL value
    (the referenced object never entered the archive — here: a derived
    WebP rendition, excluded by design). The crash-recovery reverse
    lookup derives the SOURCE character's key from it; deleting that key
    would destroy live media. The sweep must whitelist against the
    ``rewrite_media_key`` output namespaces."""
    from kokoro_link.infrastructure.storage.image_variants import (
        VARIANT_SPECS,
        derive_variant_key,
    )

    await _seed_full_character(env)
    derived_key = derive_variant_key(
        "characters/char-1/stage0.png", VARIANT_SPECS[0].name,
    )
    derived = await env.storage.put_bytes(
        object_key=derived_key,
        content=b"derived-bytes",
        content_type="image/webp",
    )
    now = datetime.now(timezone.utc)
    async with env.session_factory() as session:
        session.add(CharacterAlbumItemRow(
            id="alb-derived", character_id="char-1", url=derived.url,
            created_at=now,
        ))
        await session.commit()

    artifact = await _export_artifact(env)
    service = _import_service(env)
    staged_key = await _stage(service, artifact)
    stored = await _run_import(env, service, staged_key)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error
    new_id = stored.character_id
    # 衍生檔不入 archive → 該列 URL 保留原值（fail-soft 缺件）。
    assert stored.progress["report"]["media_missing"]

    # 模擬 crash-recovery 的反查清掃（uploaded_keys=None 路徑）。
    await service._cleanup_partial(  # noqa: SLF001
        character_id=new_id,
        operator_id=IMPORTER,
        landed_template_ids=[],
        landed_series_ids=[],
    )

    # 來源角色的活媒體毫髮無傷 ——
    assert await env.storage.get_bytes(
        object_key=derived_key,
    ) == b"derived-bytes"
    # —— 而本輪 restore 自己落的媒體確實被掃掉。
    assert await env.storage.stat(
        object_key=f"characters/{new_id}/stage0.png",
    ) is None


# ---------------------------------------------------------------------------
# A8 — arc material 邊落邊回填：失敗路徑吃即時清單
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_land_arc_material_sinks_see_ids_landed_before_a_failure() -> None:
    """A8 reproduction at the seam: the series landing explodes AFTER a
    template already landed — the sink must already hold that id (the
    return value never arrives)."""
    from kokoro_link.application.dto.character_card import (
        CharacterCardArcSeriesBundle,
        CharacterCardArcSeriesMember,
    )
    from kokoro_link.application.services.character_card_import_service import (
        CharacterCardImportService,
    )
    from kokoro_link.domain.entities.arc_template import (
        ArcTemplate,
        ArcTemplateBeat,
    )
    from kokoro_link.infrastructure.character_card.arc_template_yaml import (
        dump_arc_template_to_yaml,
    )

    class _RecordingTemplateRepo:
        def __init__(self) -> None:
            self.saved: list[str] = []

        async def save_for_user(
            self, template, *, user_id, overwrite=False,  # noqa: ANN001
        ) -> None:
            self.saved.append(template.id)

    class _ExplodingSeriesRepo:
        async def save_for_user(
            self, series, *, user_id, overwrite=False,  # noqa: ANN001
        ) -> None:
            raise RuntimeError("series store down")

    service = CharacterCardImportService(
        character_service=None,
        character_image_service=None,
        arc_template_repository=_RecordingTemplateRepo(),
        arc_series_repository=_ExplodingSeriesRepo(),
    )
    template = ArcTemplate.create(
        id="pilot_arc",
        title="Pilot",
        premise="premise",
        beats=[ArcTemplateBeat.create(
            sequence=0, day_offset=0, title="opening",
            summary="setup beat", tension="setup",
        )],
    )
    bundle = CharacterCardArcSeriesBundle(
        id="s1",
        title="Series",
        premise="premise",
        members=[CharacterCardArcSeriesMember(
            template_ref="pilot_arc", position=0,
        )],
    )
    template_sink: list[str] = []
    series_sink: list[str] = []
    with pytest.raises(RuntimeError):
        await service.land_arc_material(
            arc_templates={
                "pilot_arc.yaml": dump_arc_template_to_yaml(template),
            },
            bundled_series=[bundle],
            user_id="alice",
            target_character_ref_map={},
            landed_template_sink=template_sink,
            landed_series_sink=series_sink,
        )
    # 拋錯前已落的 template id 對呼叫端即時可見。
    assert template_sink == ["pilot_arc"]
    assert series_sink == []


@pytest.mark.asyncio
async def test_arc_landing_failure_cleanup_sees_partial_ids(env) -> None:  # noqa: ANN001
    """A8 end-to-end: the restore's arc landing raises midway — the
    failure cleanup must delete the templates that DID land, and the
    failed job's progress must carry them for crash recovery."""

    class _SinkCardStub:
        async def land_arc_material(self, **kwargs):  # noqa: ANN003, ANN202
            kwargs["landed_template_sink"].append("tmpl-landed")
            raise RuntimeError("arc landing exploded")

    class _DeleteRecordingArcRepo:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete_for_user(self, template_id, *, user_id):  # noqa: ANN001, ANN202
            self.deleted.append(template_id)

    inner = io.BytesIO()
    with BackupArchiveWriter(inner) as writer:
        writer.write_manifest(json.dumps({
            "schema_version": 1, "character_id": "char-x",
        }))
        writer.write_arc_template("t1.yaml", "id: t1")
    inner.seek(0)
    encrypted = io.BytesIO()
    encrypt_stream(
        inner, encrypted, password=PASSWORD, params=_FAST_PARAMS,
    )

    arc_repo = _DeleteRecordingArcRepo()
    service = _import_service(
        env,
        card_import_service=_SinkCardStub(),
        arc_template_repository=arc_repo,
    )
    staged_key = await _stage(service, encrypted.getvalue())
    stored = await _run_import(env, service, staged_key)

    assert stored.status == BACKUP_JOB_STATUS_FAILED
    assert "arc landing exploded" in (stored.error or "")
    # 清理拿到即時清單，孤兒模板被刪掉——
    assert arc_repo.deleted == ["tmpl-landed"]
    # —— 而 crash-recovery 用的 progress 也帶著它。
    assert stored.progress["landed_arc_template_ids"] == ["tmpl-landed"]


# ---------------------------------------------------------------------------
# B1 — 解壓預算逐 member 只計費一次（scan + landing 兩個 pass 不重複扣）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_budget_charges_each_member_once_across_passes(env) -> None:  # noqa: ANN001
    """B1 reproduction end to end: an honest backup whose declared total
    fits the budget EXACTLY must import. The restore walks every
    ``data/*.jsonl`` twice (scan pass + landing pass); charging both
    passes used to bill ≈2×data＋assets against the cap and fail the
    restore midway with a "declared sizes were a lie" accusation — and a
    clean rerun hit the identical wall until attempts ran out."""
    await _seed_full_character(env)
    artifact = await _export_artifact(env)

    # 這份誠實檔案「單次完整解壓」的總量 == central directory 宣告值。
    plain = io.BytesIO()
    decrypt_stream(io.BytesIO(artifact), plain, password=PASSWORD)
    plain.seek(0)
    declared_total = sum(
        info.file_size
        for info in zipfile.ZipFile(plain).infolist()
        if not info.is_dir()
    )
    assert declared_total > 0

    # 預算掐在宣告值：與 fast-reject 同一不變量，兩個 pass 都得過。
    service = _import_service(env, max_uncompressed_bytes=declared_total)
    staged_key = await _stage(service, artifact)
    stored = await _run_import(env, service, staged_key)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error
    report = stored.progress["report"]
    assert report["rows_landed"]["messages"] == 3
    assert report["media_transferred"] == 5


# ---------------------------------------------------------------------------
# B3 — 匯出者鑄造的 URL 由 manifest 映射定案，匯入端 adapter 反解不了也能落地
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absolute_urls_the_importer_cannot_reverse_still_land(env) -> None:  # noqa: ANN001
    """B3 reproduction: rows minted under an old ``public_base_url``
    carry absolute URLs (https://old-host/…) the IMPORTING deployment's
    adapter cannot reverse. The bytes ride in ``assets/`` and the
    manifest's exporter-side url→key map must carry the round trip —
    not a whole batch of ``media_missing``."""
    old_host = InMemoryObjectStorage(
        public_base_url="https://old-host/v1/public",
    )
    now = datetime.now(timezone.utc)
    stage = await old_host.put_bytes(
        object_key="characters/char-1/stage0.png",
        content=b"stage-image-bytes",
        content_type="image/png",
    )
    album = await old_host.put_bytes(
        object_key="characters/char-1/album/a1.png",
        content=b"album-bytes",
        content_type="image/png",
    )
    # 前提成立：匯入端 adapter 對這些絕對網址反解不出 key。
    assert env.storage.object_key_from_url(stage.url) is None
    async with env.session_factory() as session:
        session.add(CharacterRow(
            id="char-1", user_id=EXPORTER, name="芊璃",
            image_urls=json.dumps([stage.url]), created_at=now,
        ))
        session.add(CharacterAlbumItemRow(
            id="alb-1", character_id="char-1", url=album.url,
            created_at=now,
        ))
        await session.commit()

    # 匯出端（舊部署）認得自家 base URL —— bytes 完整進 assets/。
    export_service = CharacterBackupExportService(
        job_repository=env.jobs,
        export_reader=env.reader,
        object_storage=old_host,
        app_version="1.2.3-test",
        envelope_params=_FAST_PARAMS,
    )
    export_job = await export_service.start_export(
        "char-1", operator_id=EXPORTER, password=PASSWORD,
    )
    await export_service.wait_until_idle()
    stored_export = await env.jobs.get(export_job.id)
    assert stored_export is not None
    assert stored_export.status == BACKUP_JOB_STATUS_SUCCEEDED
    artifact = await old_host.get_bytes(
        object_key=stored_export.artifact_object_key,
    )

    # 匯入端（本部署、不同 adapter）。
    service = _import_service(env)
    staged_key = await _stage(service, artifact)
    stored = await _run_import(env, service, staged_key)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error
    new_id = stored.character_id

    report = stored.progress["report"]
    assert report["media_missing"] == []
    assert report["media_transferred"] == 2

    # 列的 URL 改寫到匯入端 adapter 的新 key，位元組正確落地。
    (character,) = await _rows_for(env, "characters", new_id)
    (image_url,) = json.loads(character.image_urls)
    new_stage_key = env.storage.object_key_from_url(image_url)
    assert new_stage_key == f"characters/{new_id}/stage0.png"
    assert await env.storage.get_bytes(object_key=new_stage_key) == (
        b"stage-image-bytes"
    )
    (album_item,) = await _rows_for(env, "character_album_items", new_id)
    new_album_key = env.storage.object_key_from_url(album_item.url)
    assert new_album_key == f"characters/{new_id}/album/a1.png"
    assert await env.storage.get_bytes(object_key=new_album_key) == (
        b"album-bytes"
    )
