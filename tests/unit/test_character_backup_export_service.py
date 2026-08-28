"""Application-level tests for the CB2 export chain.

Real seams end to end (the ticket's test contract): SQLite behind the
read-only export reader + the SA job repository, the in-memory object
storage adapter, CB1's real cipher (small scrypt params so tests stay
fast — decrypt reads them back from the header), and CB0's packager to
reopen the produced archive.

Pinned here:

1. the artifact opens with the right password and per-table counts match
   the manifest (and the seeded reality, including cross-character /
   global-seed scoping),
2. missing media is fail-soft and lands in the export report,
3. one in-flight export per character (409 seam) and the hosted 24h
   throttle (429 seam, ledger fail direction),
4. the password and the plaintext never persist: job payload is scrubbed
   at terminal state, no persisted surface contains the password, and
   the artifact is ciphertext.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

# Register every ORM table before ``create_all`` (registry pin-test trick).
from kokoro_link.infrastructure.persistence import (  # noqa: F401
    branching_drama_models as _branching_drama_models,
    character_backup_models as _character_backup_models,
    fusion_story_models as _fusion_story_models,
    rss_models as _rss_models,
)

from kokoro_link.application.dto.character_backup import carried_table_rules
from kokoro_link.application.services.character_backup_export_service import (
    CharacterBackupExportService,
    CharacterBackupLedgerNotConfiguredError,
    CharacterBackupManagedError,
    CharacterBackupNotFoundError,
    CharacterBackupRateLimitedError,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_EXPORT,
)
from kokoro_link.contracts.character_backup_jobs import (
    BACKUP_JOB_KIND_EXPORT,
    BACKUP_JOB_STATUS_FAILED,
    BACKUP_JOB_STATUS_RUNNING,
    BACKUP_JOB_STATUS_SUCCEEDED,
    BackupJobConflictError,
    CharacterBackupJob,
    MAX_BACKUP_JOB_ATTEMPTS,
)
from kokoro_link.contracts.object_storage import (
    BACKUP_EXPORT_OBJECT_KEY_PREFIX,
)
from kokoro_link.domain.entities.arc_template import (
    ArcTemplate,
    ArcTemplateBeat,
)
from kokoro_link.infrastructure.character_backup.packager import (
    BackupArchiveReader,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import (
    Base,
    CharacterAlbumItemRow,
    CharacterRow,
    ConversationRow,
    DailyScheduleRow,
    DialogueCheckpointRow,
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
from kokoro_link.infrastructure.repositories.in_memory_account_runtime_usage import (
    InMemoryAccountRuntimeUsageRepository,
)
from kokoro_link.infrastructure.security.backup_cipher import (
    BackupWrongPasswordError,
    EnvelopeParams,
    decrypt_stream,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

OPERATOR = "default"
OTHER_OPERATOR = "mallory"
PASSWORD = "correct-horse-battery"

# Tiny scrypt keeps each KDF < 5ms; decrypt reads the params back from
# the envelope header, so nothing else needs to know.
_FAST_PARAMS = EnvelopeParams(
    scrypt_n=2**8, scrypt_r=8, scrypt_p=1, chunk_size=64 * 1024,
)


class _StubArcTemplateRepo:
    def __init__(self, *templates: ArcTemplate) -> None:
        self._templates = {template.id: template for template in templates}

    async def get_for_user(
        self, template_id: str, *, user_id: str,
    ) -> ArcTemplate | None:
        return self._templates.get(template_id)


def _template(template_id: str = "promise-arc") -> ArcTemplate:
    return ArcTemplate(
        id=template_id,
        title="約定之弧",
        premise="一段關於信任慢慢回來的日常劇情。",
        theme="friendship",
        beats=(
            ArcTemplateBeat(
                sequence=0, day_offset=0, title="第一步",
                summary="角色決定要不要開口問起那個約定。",
            ),
        ),
    )


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
    )
    try:
        yield harness
    finally:
        await engine.dispose()


def _service(env, **overrides) -> CharacterBackupExportService:
    kwargs = dict(
        job_repository=env.jobs,
        export_reader=env.reader,
        object_storage=env.storage,
        arc_template_repository=_StubArcTemplateRepo(_template()),
        app_version="1.2.3-test",
        envelope_params=_FAST_PARAMS,
    )
    kwargs.update(overrides)
    return CharacterBackupExportService(**kwargs)


async def _seed_full_character(env) -> dict[str, str]:
    """One character with representative data across the CARRY surface,
    plus a *second* operator's character to prove scoping.

    Returns the object keys of the seeded media."""
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
            user_id=OPERATOR,
            name="芊璃",
            image_urls=json.dumps([stage.url]),
            arc_template_id="promise-arc",
            created_at=now,
        ))
        session.add(ConversationRow(id="conv-1", character_id="char-1"))
        session.add(MessageRow(
            conversation_id="conv-1", position=0, role="user",
            content="午安，今天過得如何？", created_at=now,
        ))
        session.add(MessageRow(
            conversation_id="conv-1", position=1, role="assistant",
            content="（限制級原文）", content_mode="nsfw",
            safe_summary="兩人聊得很開心。", created_at=now,
        ))
        session.add(MessageRow(
            conversation_id="conv-1", position=2, role="assistant",
            content="（限制級原文，無安全摘要）", content_mode="nsfw",
            created_at=now,
            attachments_json=json.dumps([
                {"kind": "image", "url": chat_upload.url,
                 "mime_type": "image/png"},
            ]),
        ))
        session.add(MemoryItemRow(
            id="mem-1", character_id="char-1", kind="fact",
            content="喜歡奶茶。", created_at=now,
        ))
        session.add(MemoryItemRow(
            id="mem-2", character_id="char-1", kind="event",
            content="那一夜。", tags=json.dumps(["content_mode:nsfw"]),
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
        # DH3 — the cumulative dialogue summary. Carried because it is
        # the only place the character's memory of everything older than
        # the loaded window still exists; the messages behind it are in
        # the archive but a summary of them is not re-derivable.
        session.add(DialogueCheckpointRow(
            character_id="char-1",
            operator_id=OPERATOR,
            summary_text="他們約好下個月去舊書店。",
            covers_until_message_key="a" * 32,
            covers_until_created_at=now,
            updated_at=now,
        ))
        session.add(StorySeedRow(
            id="seed-private", seed_text="她想起舊書店的約定。",
            character_id="char-1", created_at=now, updated_at=now,
        ))
        session.add(StorySeedRow(
            id="seed-global", seed_text="全域種子，不屬於任何角色。",
            character_id=None, created_at=now, updated_at=now,
        ))
        # Another operator's character — none of this may leak into the
        # archive (scoping through the parent-linked join included).
        session.add(CharacterRow(
            id="char-2", user_id=OTHER_OPERATOR, name="別人的角色",
            created_at=now,
        ))
        session.add(ConversationRow(id="conv-2", character_id="char-2"))
        for position in range(5):
            session.add(MessageRow(
                conversation_id="conv-2", position=position, role="user",
                content=f"other {position}", created_at=now,
            ))
        await session.commit()
    return {
        "stage": stage.object_key,
        "chat_upload": chat_upload.object_key,
        "album": album.object_key,
        "feed": feed.object_key,
    }


async def _seed_managed_character(env, character_id="char-managed") -> None:
    """A minimal IP-partner (managed) character owned by ``OPERATOR`` —
    EC3's gate reads ``origin_official_card_id`` directly off this row."""
    now = datetime.now(timezone.utc)
    async with env.session_factory() as session:
        session.add(CharacterRow(
            id=character_id,
            user_id=OPERATOR,
            name="託管角色",
            origin_official_card_id="cloud-card-managed",
            created_at=now,
        ))
        await session.commit()


async def _run_export(env, service, character_id="char-1"):
    job = await service.start_export(
        character_id, operator_id=OPERATOR, password=PASSWORD,
    )
    await service.wait_until_idle()
    stored = await env.jobs.get(job.id)
    assert stored is not None
    return stored


def _open_archive(encrypted: bytes, password: str) -> io.BytesIO:
    plain = io.BytesIO()
    decrypt_stream(io.BytesIO(encrypted), plain, password=password)
    plain.seek(0)
    return plain


# ---------------------------------------------------------------------------
# 驗收 1 — 正確密碼開得起來、逐表筆數與 manifest 一致
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_round_trip_counts_match_manifest(env) -> None:  # noqa: ANN001
    media_keys = await _seed_full_character(env)
    service = _service(env)

    stored = await _run_export(env, service)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED
    assert stored.error is None
    assert stored.artifact_object_key is not None
    assert stored.artifact_object_key.startswith(
        BACKUP_EXPORT_OBJECT_KEY_PREFIX,
    )

    encrypted = await env.storage.get_bytes(
        object_key=stored.artifact_object_key,
    )
    with BackupArchiveReader(_open_archive(encrypted, PASSWORD)) as reader:
        manifest = reader.read_manifest()

        # Actual jsonl line counts == manifest counts, table by table.
        actual_counts: dict[str, int] = {}
        for name, stream in reader.iter_data_files():
            table = name.removesuffix(".jsonl")
            lines = stream.read().decode("utf-8").splitlines()
            actual_counts[table] = sum(1 for line in lines if line.strip())
        assert actual_counts == manifest["table_counts"]

        # Every CARRY table has a data file, empty tables included.
        assert set(actual_counts) == {
            rule.table for rule in carried_table_rules()
        }

        # Seeded reality — including scoping: char-2's five messages and
        # the global seed never enter the archive.
        assert manifest["table_counts"]["characters"] == 1
        assert manifest["table_counts"]["conversations"] == 1
        assert manifest["table_counts"]["messages"] == 3
        assert manifest["table_counts"]["memory_items"] == 2
        assert manifest["table_counts"]["character_album_items"] == 1
        assert manifest["table_counts"]["feed_posts"] == 1
        assert manifest["table_counts"]["daily_schedules"] == 1
        assert manifest["table_counts"]["schedule_activities"] == 1
        assert manifest["table_counts"]["story_seeds"] == 1

        assert manifest["character_id"] == "char-1"
        assert manifest["character_name"] == "芊璃"
        assert manifest["app_version"] == "1.2.3-test"
        assert manifest["exported_at"]

        # NSFW 折疊統計（供 CB3 hosted 預覽零成本）。
        assert manifest["nsfw_collapse"]["messages_replaced"] == 1
        assert manifest["nsfw_collapse"]["messages_dropped"] == 1
        assert manifest["nsfw_collapse"]["memories_skipped"] == 1

        # 媒體原檔全數在 assets/，內容逐位元相同。
        assets = {key: stream.read() for key, stream in reader.iter_assets()}
        assert assets[media_keys["stage"]] == b"stage-image-bytes"
        assert assets[media_keys["chat_upload"]] == b"chat-upload-bytes"
        assert assets[media_keys["album"]] == b"album-bytes"
        assert assets[media_keys["feed"]] == b"feed-bytes"
        assert set(entry["key"] for entry in manifest["media"]) == set(
            media_keys.values(),
        )
        assert manifest["export_report"]["skipped_media_keys"] == []

        # 綁定的 arc template 以 YAML 隨檔。
        arc_files = dict(reader.iter_arc_templates())
        assert "promise-arc.yaml" in arc_files
        assert "約定之弧" in arc_files["promise-arc.yaml"]
        assert manifest["bundled_arc_template_ids"] == ["promise-arc"]


async def _seed_extra_nsfw_carriers(env) -> None:
    """A minimal character carrying the *non-message* NSFW carriers
    (operator_profile_fields column + pending_follow_ups.messages_json),
    each with one nsfw-flagged row and one normal row."""
    now = datetime.now(timezone.utc)
    async with env.session_factory() as session:
        session.add(CharacterRow(
            id="char-c", user_id=OPERATOR, name="載體角色", created_at=now,
        ))
        session.add(ConversationRow(id="conv-c", character_id="char-c"))
        # operator_profile_fields: 1 nsfw (skipped) + 1 normal (kept).
        session.add(OperatorProfileFieldRow(
            id="opf-nsfw", character_id="char-c", operator_id=OPERATOR,
            layer=3, field_key="intimate_fact", value="（限制級畫像原文）",
            confidence=0.9, content_mode="nsfw", created_at=now, updated_at=now,
        ))
        session.add(OperatorProfileFieldRow(
            id="opf-normal", character_id="char-c", operator_id=OPERATOR,
            layer=1, field_key="likes_tea", value="喜歡奶茶。",
            confidence=0.8, content_mode="normal", created_at=now,
            updated_at=now,
        ))
        # pending_follow_ups: 1 nsfw (any queued turn nsfw) + 1 normal.
        session.add(PendingFollowUpRow(
            id="pfu-nsfw", character_id="char-c", conversation_id="conv-c",
            brief_reply="等等回你。",
            messages_json=json.dumps([
                {"content": "（限制級延後回覆原文）", "content_mode": "nsfw",
                 "queued_at": now.isoformat()},
            ], ensure_ascii=False),
            resolved_message="（限制級完整回覆原文）",
            scheduled_for=now, queued_at=now, updated_at=now,
        ))
        session.add(PendingFollowUpRow(
            id="pfu-normal", character_id="char-c", conversation_id="conv-c",
            brief_reply="待會兒聊。",
            messages_json=json.dumps([
                {"content": "晚點記得提醒我", "content_mode": "normal",
                 "queued_at": now.isoformat()},
            ], ensure_ascii=False),
            scheduled_for=now, queued_at=now, updated_at=now,
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_export_counts_nsfw_profile_fields_and_follow_ups(env) -> None:  # noqa: ANN001
    """The D5 collapse stats cover every marked carrier — not just
    messages/memories — so the hosted preview promise is exact: one nsfw
    operator-profile-field and one nsfw pending-follow-up are counted for
    folding, the normal siblings are not."""
    await _seed_extra_nsfw_carriers(env)
    service = _service(env, arc_template_repository=None)

    stored = await _run_export(env, service, character_id="char-c")
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error
    encrypted = await env.storage.get_bytes(
        object_key=stored.artifact_object_key,
    )
    with BackupArchiveReader(_open_archive(encrypted, PASSWORD)) as reader:
        manifest = reader.read_manifest()
        assert manifest["table_counts"]["operator_profile_fields"] == 2
        assert manifest["table_counts"]["pending_follow_ups"] == 2
        assert manifest["nsfw_collapse"]["operator_profile_fields_skipped"] == 1
        assert manifest["nsfw_collapse"]["pending_follow_ups_skipped"] == 1
        # No message/memory carriers seeded here → those stay zero.
        assert manifest["nsfw_collapse"]["messages_replaced"] == 0
        assert manifest["nsfw_collapse"]["memories_skipped"] == 0


@pytest.mark.asyncio
async def test_wrong_password_cannot_open_artifact(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    stored = await _run_export(env, _service(env))
    encrypted = await env.storage.get_bytes(
        object_key=stored.artifact_object_key,
    )
    with pytest.raises(BackupWrongPasswordError):
        _open_archive(encrypted, "not-the-password")


# ---------------------------------------------------------------------------
# 驗收 2 — 媒體缺檔 fail-soft 且記入匯出報告
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_media_is_fail_soft_and_reported(env) -> None:  # noqa: ANN001
    media_keys = await _seed_full_character(env)
    # The album original is referenced by the DB but gone from storage —
    # the exact "already-lost original" the report exists for.
    await env.storage.delete(object_key=media_keys["album"])

    stored = await _run_export(env, _service(env))
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED

    encrypted = await env.storage.get_bytes(
        object_key=stored.artifact_object_key,
    )
    with BackupArchiveReader(_open_archive(encrypted, PASSWORD)) as reader:
        manifest = reader.read_manifest()
        assert manifest["export_report"]["skipped_media_keys"] == [
            media_keys["album"],
        ]
        collected = {entry["key"] for entry in manifest["media"]}
        assert media_keys["album"] not in collected
        assert media_keys["stage"] in collected
        assets = {key for key, _stream in reader.iter_assets()}
        assert media_keys["album"] not in assets


# ---------------------------------------------------------------------------
# 驗收 3 — 同角色單一進行中匯出、hosted 頻率限制
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_export_for_same_character_conflicts(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    await env.jobs.add(CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_EXPORT,
        operator_id=OPERATOR,
        character_id="char-1",
        payload={"envelope_header_b64": "x", "file_key_b64": "y"},
    ))

    with pytest.raises(BackupJobConflictError):
        await _service(env).start_export(
            "char-1", operator_id=OPERATOR, password=PASSWORD,
        )


@pytest.mark.asyncio
async def test_unowned_character_answers_not_found(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    with pytest.raises(CharacterBackupNotFoundError):
        await _service(env).start_export(
            "char-2",  # exists, but belongs to OTHER_OPERATOR
            operator_id=OPERATOR,
            password=PASSWORD,
        )


@pytest.mark.asyncio
async def test_managed_character_export_is_refused(env) -> None:  # noqa: ANN001
    """EC3: a managed (IP-partner) character's persona lives only on the
    server — no ``.lumebackup`` may ever exist for it. Distinct from
    ``CharacterBackupNotFoundError``: the character is the caller's own
    and exists, so 404 would misrepresent the refusal (mirrors the
    card exporter's ``CharacterCardManagedError``)."""
    await _seed_managed_character(env)
    with pytest.raises(CharacterBackupManagedError):
        await _service(env).start_export(
            "char-managed", operator_id=OPERATOR, password=PASSWORD,
        )


@pytest.mark.asyncio
async def test_ordinary_character_export_unaffected_by_managed_gate(
    env,  # noqa: ANN001
) -> None:
    """The managed gate must not regress the ordinary path — refusing a
    sibling managed character's export has zero effect on this
    character's own, independently-scoped export call."""
    await _seed_full_character(env)
    await _seed_managed_character(env)
    stored = await _run_export(env, _service(env), character_id="char-1")
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED


@pytest.mark.asyncio
async def test_hosted_rate_limit_blocks_past_the_window(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    usage = InMemoryAccountRuntimeUsageRepository()
    now = datetime.now(timezone.utc)
    for _ in range(2):
        await usage.record_event(
            operator_id=OPERATOR,
            event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_EXPORT,
            occurred_at=now,
        )
    service = _service(
        env, cloud_mode=True, account_runtime_usage_repository=usage,
    )
    with pytest.raises(CharacterBackupRateLimitedError):
        await service.start_export(
            "char-1", operator_id=OPERATOR, password=PASSWORD,
        )


@pytest.mark.asyncio
async def test_hosted_export_records_a_ledger_event(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    usage = InMemoryAccountRuntimeUsageRepository()
    service = _service(
        env, cloud_mode=True, account_runtime_usage_repository=usage,
    )
    stored = await _run_export(env, service)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED
    counted = await usage.count_events(
        operator_id=OPERATOR,
        event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_EXPORT,
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert counted == 1


@pytest.mark.asyncio
async def test_ledger_write_failure_leaves_no_zombie_running_job(env) -> None:  # noqa: ANN001
    """B2 reproduction: ``start_export`` used to commit the running job
    row BEFORE the ledger write and spawn AFTER it — a ledger hiccup
    (DB抖動) surfaced as a 500 while the committed row, driven by no
    task, 409-locked the character until the next startup recovery. The
    failed ledger write must leave the job terminal so the player can
    simply retry."""
    await _seed_full_character(env)

    class _FlakyUsage(InMemoryAccountRuntimeUsageRepository):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_record = True

        async def record_event(self, **kwargs):  # noqa: ANN003, ANN202
            if self.fail_next_record:
                self.fail_next_record = False
                raise RuntimeError("ledger db hiccup")
            return await super().record_event(**kwargs)

    usage = _FlakyUsage()
    service = _service(
        env, cloud_mode=True, account_runtime_usage_repository=usage,
    )
    with pytest.raises(RuntimeError, match="ledger db hiccup"):
        await service.start_export(
            "char-1", operator_id=OPERATOR, password=PASSWORD,
        )
    await service.wait_until_idle()

    # 沒有殭屍 running 列鎖住角色 ——
    assert await env.jobs.get_active_export_for_character("char-1") is None
    assert await env.jobs.list_running() == []
    # —— 再次匯出立即可行，不必等下一次 recover。
    stored = await _run_export(env, service)
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED
    counted = await usage.count_events(
        operator_id=OPERATOR,
        event_type=ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_EXPORT,
        since=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert counted == 1  # 失敗那次沒收錢


@pytest.mark.asyncio
async def test_hosted_without_ledger_fails_closed(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    service = _service(
        env, cloud_mode=True, account_runtime_usage_repository=None,
    )
    with pytest.raises(CharacterBackupLedgerNotConfiguredError):
        await service.start_export(
            "char-1", operator_id=OPERATOR, password=PASSWORD,
        )


@pytest.mark.asyncio
async def test_self_host_neither_counts_nor_needs_a_ledger(env) -> None:  # noqa: ANN001
    """cloud_mode=False: no ledger wired, no throttle, no event."""
    await _seed_full_character(env)
    stored = await _run_export(env, _service(env))
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED


# ---------------------------------------------------------------------------
# 驗收 4 — 密碼與明文中間產物不落地
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_job_payload_is_scrubbed_and_password_never_persists(
    env,  # noqa: ANN001
) -> None:
    await _seed_full_character(env)
    stored = await _run_export(env, _service(env))

    # 終態即抹除：金鑰材料不在 job row 上。
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED
    assert dict(stored.payload) == {}

    # 密碼不出現在任何持久化面 — job row 的每個欄位…
    row_projection = json.dumps({
        "progress": dict(stored.progress),
        "payload": dict(stored.payload),
        "error": stored.error,
        "artifact": stored.artifact_object_key,
    })
    assert PASSWORD not in row_projection
    # …也不出現在唯一落地的物件（產物是 ciphertext）。
    encrypted = await env.storage.get_bytes(
        object_key=stored.artifact_object_key,
    )
    assert PASSWORD.encode("utf-8") not in encrypted
    # 內層 zip 的明文片段（例如訊息原文）也不可見。
    assert "午安".encode("utf-8") not in encrypted


# ---------------------------------------------------------------------------
# 啟動恢復（studio job 前例的鏡像）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_resumes_an_interrupted_export(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    service = _service(env)
    # Simulate a crash: a job row added with real envelope material but
    # no task driving it (as if the process died mid-export).
    from kokoro_link.infrastructure.security.backup_cipher import (
        prepare_envelope,
    )
    import base64

    prepared = prepare_envelope(PASSWORD, params=_FAST_PARAMS)
    job = CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_EXPORT,
        operator_id=OPERATOR,
        character_id="char-1",
        payload={
            "envelope_header_b64": base64.b64encode(
                prepared.header,
            ).decode("ascii"),
            "file_key_b64": base64.b64encode(
                prepared.file_key,
            ).decode("ascii"),
        },
    )
    await env.jobs.add(job)

    report = await service.recover()
    await service.wait_until_idle()

    assert report["resumed"] == 1
    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED
    assert stored.attempts == 2
    # And the resumed artifact still opens with the original password.
    encrypted = await env.storage.get_bytes(
        object_key=stored.artifact_object_key,
    )
    with BackupArchiveReader(_open_archive(encrypted, PASSWORD)) as reader:
        assert reader.read_manifest()["character_id"] == "char-1"


@pytest.mark.asyncio
async def test_recover_skips_an_export_leased_by_another_replica(env) -> None:  # noqa: ANN001
    """A1: during a drain roll the old replica is still exporting this
    job — a restarting replica's recover() must skip it (lease held), not
    spawn a duplicate dump racing the artifact upload."""
    from kokoro_link.application.services.studio_execution_lease import (
        StudioExecutionLease,
    )
    from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
        InMemoryBackgroundCoordinatorLease,
    )

    backend = InMemoryBackgroundCoordinatorLease()
    replica_a = StudioExecutionLease(
        backend, owner_id="replica-a", name_prefix="backup:",
    )
    replica_b = StudioExecutionLease(
        backend, owner_id="replica-b", name_prefix="backup:",
    )
    await _seed_full_character(env)
    service = _service(env, execution_lease=replica_b)

    job = CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_EXPORT,
        operator_id=OPERATOR,
        character_id="char-1",
        payload={"envelope_header_b64": "x", "file_key_b64": "y"},
    )
    await env.jobs.add(job)
    assert await replica_a.acquire(job.id) is not None  # A 正在跑

    report = await service.recover()
    await service.wait_until_idle()

    assert report.get("lease_skipped") == 1
    assert report["resumed"] == 0
    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_RUNNING
    assert stored.attempts == 1  # 沒被第二個 driver 動過


@pytest.mark.asyncio
async def test_recover_fails_jobs_out_of_attempts_or_scrubbed(env) -> None:  # noqa: ANN001
    await _seed_full_character(env)
    service = _service(env)

    exhausted = CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_EXPORT,
        operator_id=OPERATOR,
        character_id="char-1",
        payload={"envelope_header_b64": "x", "file_key_b64": "y"},
    ).with_attempts(MAX_BACKUP_JOB_ATTEMPTS)
    await env.jobs.add(exhausted)
    await env.jobs.save_progress_if_running(exhausted)

    scrubbed = CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_EXPORT,
        operator_id=OPERATOR,
        character_id="char-3",  # different character — no dedup clash
        payload={},  # TTL backstop already erased the key material
    )
    await env.jobs.add(scrubbed)

    report = await service.recover()
    await service.wait_until_idle()

    assert report["failed"] == 2
    for job_id in (exhausted.id, scrubbed.id):
        stored = await env.jobs.get(job_id)
        assert stored is not None
        assert stored.status == BACKUP_JOB_STATUS_FAILED
        assert dict(stored.payload) == {}


@pytest.mark.asyncio
async def test_export_failure_finalizes_failed_and_scrubs(env) -> None:  # noqa: ANN001
    """A mid-pipeline crash (storage put refused) still reaches a terminal
    scrubbed state instead of a stuck running row."""
    await _seed_full_character(env)

    class _RefusingStorage(InMemoryObjectStorage):
        async def put_bytes(self, **kwargs):  # noqa: ANN003, ANN202
            if kwargs["object_key"].startswith(
                BACKUP_EXPORT_OBJECT_KEY_PREFIX,
            ):
                raise RuntimeError("artifact store down")
            return await super().put_bytes(**kwargs)

    refusing = _RefusingStorage()
    # Media reads go through the same adapter — reseed the objects there.
    for key, data in [
        ("characters/char-1/stage0.png", b"stage-image-bytes"),
        ("users/default/chat-uploads/photo.png", b"chat-upload-bytes"),
        ("characters/char-1/album/a1.png", b"album-bytes"),
        ("feed/char-1/post1.png", b"feed-bytes"),
    ]:
        await InMemoryObjectStorage.put_bytes(
            refusing, object_key=key, content=data, content_type="image/png",
        )

    service = _service(env, object_storage=refusing)
    stored = await _run_export(env, service)
    assert stored.status == BACKUP_JOB_STATUS_FAILED
    assert stored.error is not None and "artifact store down" in stored.error
    assert dict(stored.payload) == {}
    assert stored.status != BACKUP_JOB_STATUS_RUNNING


# ---------------------------------------------------------------------------
# S7 — resume after encryption must not re-encrypt (GCM nonce reuse)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_after_encryption_stage_fails_instead_of_reencrypting(
    env,  # noqa: ANN001
) -> None:
    """S7 reproduction: a job crashed with progress already at ``encrypt``
    (or ``upload``) has consumed the envelope's fixed GCM nonces once.
    Re-driving it would re-encrypt a freshly re-dumped plaintext under the
    SAME (file key, nonce) pairs — catastrophic for AES-GCM. Recovery must
    finalize it FAILED with the "start a new export" grammar instead of
    running the export a second time."""
    from kokoro_link.infrastructure.security.backup_cipher import (
        prepare_envelope,
    )
    import base64

    await _seed_full_character(env)
    service = _service(env)
    prepared = prepare_envelope(PASSWORD, params=_FAST_PARAMS)
    job = CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_EXPORT,
        operator_id=OPERATOR,
        character_id="char-1",
        payload={
            "envelope_header_b64": base64.b64encode(
                prepared.header,
            ).decode("ascii"),
            "file_key_b64": base64.b64encode(
                prepared.file_key,
            ).decode("ascii"),
        },
        progress={"stage": "encrypt"},
    )
    await env.jobs.add(job)

    report = await service.recover()
    await service.wait_until_idle()

    assert report["failed"] == 1
    assert report["resumed"] == 0
    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_FAILED
    assert "after encryption" in (stored.error or "")
    # Never re-driven: attempts not bumped, no artifact produced, payload
    # scrubbed by the terminal transition.
    assert stored.attempts == 1
    assert not stored.artifact_object_key
    assert dict(stored.payload) == {}


@pytest.mark.asyncio
async def test_resume_before_encryption_still_reruns(env) -> None:  # noqa: ANN001
    """The S7 guard must not swallow legitimate resumes: a job interrupted
    *before* encryption (stage ``dump``/``media``) re-runs as before — the
    nonces were never consumed."""
    from kokoro_link.infrastructure.security.backup_cipher import (
        prepare_envelope,
    )
    import base64

    await _seed_full_character(env)
    service = _service(env)
    prepared = prepare_envelope(PASSWORD, params=_FAST_PARAMS)
    job = CharacterBackupJob.create(
        kind=BACKUP_JOB_KIND_EXPORT,
        operator_id=OPERATOR,
        character_id="char-1",
        payload={
            "envelope_header_b64": base64.b64encode(
                prepared.header,
            ).decode("ascii"),
            "file_key_b64": base64.b64encode(
                prepared.file_key,
            ).decode("ascii"),
        },
        progress={"stage": "media"},
    )
    await env.jobs.add(job)

    report = await service.recover()
    await service.wait_until_idle()

    assert report["resumed"] == 1
    stored = await env.jobs.get(job.id)
    assert stored is not None
    assert stored.status == BACKUP_JOB_STATUS_SUCCEEDED, stored.error
