"""Security-fix reproductions for the ``.lumebackup`` restore pipeline.

The backup import surface is「attacker delivers a file + password, victim
imports」: the manifest and every archived byte are attacker-controlled.
These pin the three restore-landing hardenings that operate on that hostile
input directly:

* **S1** — media content types are forced through the served-image
  allow-list, so a manifest declaring ``text/html`` (or a ``.html`` key, or
  SVG) lands as inert ``application/octet-stream`` instead of a same-origin
  executable document;
* **S2** — restored URL columns / attachments are scheme-guarded, so an
  unmapped ``javascript:``/``data:`` URL cannot ride an ``<a :href>`` into
  same-origin script;
* **S5** — the character row's daily limits are clamped through the exact
  domain clamp, so a backup carrying ``proactive_daily_limit=100000``
  cannot uncap an account's proactive-message LLM spend.
"""

from __future__ import annotations

import io
import json

import pytest

from kokoro_link.application.dto.character_backup import (
    BACKUP_SCHEMA_VERSION,
    BACKUP_TABLE_RULES_BY_NAME,
    CharacterBackupManifest,
)
from kokoro_link.application.media_content_types import (
    coerce_served_image_content_type,
)
from kokoro_link.application.services.character_backup_restore_pipeline import (
    CharacterRestorePipeline,
    RestoreContext,
    sanitise_landed_url,
)
from kokoro_link.infrastructure.character_backup.packager import (
    BackupArchiveReader,
    BackupArchiveWriter,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

_FALLBACK = "application/octet-stream"


# ---------------------------------------------------------------------------
# S1 — served-image content-type allow-list (helper + pipeline)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declared,expected",
    [
        ("image/png", "image/png"),
        ("image/jpeg", "image/jpeg"),
        ("image/webp", "image/webp"),
        ("image/gif", "image/gif"),
        ("IMAGE/PNG; charset=binary", "image/png"),
        ("text/html", _FALLBACK),
        ("image/svg+xml", _FALLBACK),  # image by any naive test, scripts
        ("application/xhtml+xml", _FALLBACK),
        ("", _FALLBACK),
        (None, _FALLBACK),
    ],
)
def test_coerce_served_image_content_type(declared, expected) -> None:  # noqa: ANN001
    assert coerce_served_image_content_type(
        declared, fallback=_FALLBACK,
    ) == expected


def _record_json(**fields) -> bytes:  # noqa: ANN003
    base = {
        "id": "char-x",
        "user_id": "someone",
        "name": "角色",
        "created_at": "2026-08-05T00:00:00+00:00",
    }
    base.update(fields)
    return json.dumps(base, ensure_ascii=False).encode("utf-8")


def _archive_with_media(entries: list[dict], image_urls: list[str]) -> io.BytesIO:
    """One inner archive: a characters row referencing ``image_urls`` plus
    an ``assets/`` member per entry and a manifest media inventory."""
    buffer = io.BytesIO()
    with BackupArchiveWriter(buffer) as writer:
        writer.write_manifest(json.dumps({
            "schema_version": BACKUP_SCHEMA_VERSION,
            "character_id": "char-x",
            "media": entries,
        }, ensure_ascii=False))
        with writer.open_data_file("characters.jsonl") as sink:
            sink.write(_record_json(image_urls=json.dumps(image_urls)) + b"\n")
        for entry in entries:
            writer.write_asset(entry["key"], io.BytesIO(b"the-bytes"))
    buffer.seek(0)
    return buffer


@pytest.mark.asyncio
async def test_restore_media_plan_neutralises_hostile_content_types() -> None:
    """S1 reproduction: the manifest's ``content_type`` is attacker-chosen.
    A ``text/html`` (or SVG) original must be re-``put`` as
    ``application/octet-stream`` — never a same-origin executable — while a
    legitimate ``image/png`` passes untouched."""
    storage = InMemoryObjectStorage()
    evil_key = "characters/char-x/evil.png"
    svg_key = "characters/char-x/mark.png"
    good_key = "characters/char-x/stage0.png"
    entries = [
        {"key": evil_key, "content_type": "text/html",
         "source_urls": ["https://old/evil.png"]},
        {"key": svg_key, "content_type": "image/svg+xml",
         "source_urls": ["https://old/mark.png"]},
        {"key": good_key, "content_type": "image/png",
         "source_urls": ["https://old/stage0.png"]},
    ]
    buffer = _archive_with_media(
        entries,
        image_urls=[
            "https://old/evil.png",
            "https://old/mark.png",
            "https://old/stage0.png",
        ],
    )
    pipeline = CharacterRestorePipeline(object_storage=storage, cloud_mode=False)
    with BackupArchiveReader(buffer) as reader:
        manifest = CharacterBackupManifest.model_validate(
            reader.read_manifest(),
        )
        ctx = await pipeline.scan(reader, manifest, operator_id="importer")

    assert ctx.transfers[evil_key].content_type == _FALLBACK
    assert ctx.transfers[svg_key].content_type == _FALLBACK
    assert ctx.transfers[good_key].content_type == "image/png"


@pytest.mark.asyncio
async def test_restore_media_plan_downgrades_html_extension_guess() -> None:
    """With no declared type, the extension guess is still attacker-chosen:
    a ``.html`` key guesses ``text/html`` and must be downgraded too."""
    storage = InMemoryObjectStorage()
    key = "characters/char-x/payload.html"
    buffer = _archive_with_media(
        [{"key": key, "content_type": "", "source_urls": ["https://old/p"]}],
        image_urls=["https://old/p"],
    )
    pipeline = CharacterRestorePipeline(object_storage=storage, cloud_mode=False)
    with BackupArchiveReader(buffer) as reader:
        manifest = CharacterBackupManifest.model_validate(
            reader.read_manifest(),
        )
        ctx = await pipeline.scan(reader, manifest, operator_id="importer")
    assert ctx.transfers[key].content_type == _FALLBACK


# ---------------------------------------------------------------------------
# S2 — URL scheme guard (helper + pipeline row rewrite)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,kept",
    [
        ("https://host/a.png", True),
        ("http://host/a.png", True),
        ("/relative/path.png", True),
        ("images/a.png", True),
        ("/a/b:c/d.png", True),  # ':' inside a path segment, not a scheme
        ("javascript:alert(document.cookie)", False),
        ("JavaScript:alert(1)", False),
        ("  javascript:alert(1)", False),  # leading spaces trimmed
        ("java\tscript:alert(1)", False),  # tab stripped like a browser
        ("java\nscript:alert(1)", False),
        ("data:text/html,<script>alert(1)</script>", False),
        ("vbscript:msgbox(1)", False),
        ("file:///etc/passwd", False),
    ],
)
def test_sanitise_landed_url(value, kept) -> None:  # noqa: ANN001
    result = sanitise_landed_url(value)
    if kept:
        assert result == value
    else:
        assert result == ""


def _ctx(**url_map) -> RestoreContext:
    return RestoreContext(
        new_character_id="new-char",
        old_character_id="char-x",
        importer_id="importer",
        id_map={},
        old_operator_ids=set(),
        url_map=dict(url_map),
        transfers={},
    )


def _pipeline() -> CharacterRestorePipeline:
    return CharacterRestorePipeline(
        object_storage=InMemoryObjectStorage(), cloud_mode=False,
    )


def test_prepare_row_neutralises_unmapped_dangerous_direct_urls() -> None:
    """S2 reproduction: an unmapped URL landed verbatim into ``image_url`` /
    a ``url`` column that a player's ``<a :href>`` renders. A dangerous
    scheme is dropped to ``""``; a safe unmapped http URL (cross-deployment
    dangling reference) is preserved."""
    pipeline = _pipeline()
    ctx = _ctx()

    feed_rule = BACKUP_TABLE_RULES_BY_NAME["feed_posts"]
    kwargs, _ = pipeline._prepare_row(  # noqa: SLF001
        feed_rule,
        {
            "id": "post-1",
            "character_id": "char-x",
            "image_url": "javascript:alert(document.cookie)",
            "video_url": "https://cdn/old/clip.mp4",
            "content_text": "hi",
        },
        {},
        ctx,
    )
    assert kwargs["image_url"] == ""  # dangerous scheme neutralised
    assert kwargs["video_url"] == "https://cdn/old/clip.mp4"  # safe, kept

    album_rule = BACKUP_TABLE_RULES_BY_NAME["character_album_items"]
    kwargs, _ = pipeline._prepare_row(  # noqa: SLF001
        album_rule,
        {"id": "a1", "character_id": "char-x", "url": "vbscript:msgbox(1)"},
        {},
        ctx,
    )
    assert kwargs["url"] == ""


def test_prepare_row_rewrites_mapped_direct_urls() -> None:
    """A mapped URL is always this deployment's own ``public_url`` — it
    rewrites through the plan and passes the scheme guard untouched."""
    pipeline = _pipeline()
    ctx = _ctx(**{"https://old/i.png": "https://new/i.png"})
    feed_rule = BACKUP_TABLE_RULES_BY_NAME["feed_posts"]
    kwargs, _ = pipeline._prepare_row(  # noqa: SLF001
        feed_rule,
        {
            "id": "post-1",
            "character_id": "char-x",
            "image_url": "https://old/i.png",
            "content_text": "hi",
        },
        {},
        ctx,
    )
    assert kwargs["image_url"] == "https://new/i.png"


def test_prepare_row_neutralises_dangerous_attachment_urls() -> None:
    """S2 reproduction on the attachments JSON: a hostile ``javascript:``
    attachment URL must not survive into ``attachments_json``."""
    pipeline = _pipeline()
    ctx = _ctx(**{"https://old/ok.png": "https://new/ok.png"})
    msg_rule = BACKUP_TABLE_RULES_BY_NAME["messages"]
    attachments = [
        {"kind": "image", "url": "javascript:alert(1)"},
        {"kind": "image", "url": "https://old/ok.png"},
    ]
    kwargs, _ = pipeline._prepare_row(  # noqa: SLF001
        msg_rule,
        {
            "conversation_id": "conv-1",
            "position": 0,
            "role": "user",
            "content": "hi",
            "content_mode": "normal",
            "attachments_json": json.dumps(attachments),
        },
        {},
        ctx,
    )
    landed = json.loads(kwargs["attachments_json"])
    assert landed[0]["url"] == ""  # dangerous scheme neutralised
    assert landed[1]["url"] == "https://new/ok.png"  # mapped + rewritten


# ---------------------------------------------------------------------------
# S5 — daily-limit clamp on restore landing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proactive_in,feed_in,proactive_out,feed_out",
    [
        (100000, 99999, 50, 50),
        (-3, -1, 0, 0),
        (7, 12, 7, 12),
    ],
)
def test_prepare_row_clamps_character_daily_limits(
    proactive_in, feed_in, proactive_out, feed_out,  # noqa: ANN001
) -> None:
    """S5 reproduction: restore lands the character row straight through the
    Core insert, bypassing ``Character.create``'s clamp. An out-of-range
    daily limit from the backup DTO must be clamped through the exact domain
    ceiling, never landed raw."""
    pipeline = _pipeline()
    ctx = _ctx()
    char_rule = BACKUP_TABLE_RULES_BY_NAME["characters"]
    kwargs, _ = pipeline._prepare_row(  # noqa: SLF001
        char_rule,
        {
            "id": "char-x",
            "user_id": "someone",
            "name": "角色",
            "image_urls": "[]",
            "proactive_daily_limit": proactive_in,
            "feed_daily_limit": feed_in,
        },
        {},
        ctx,
    )
    assert kwargs["proactive_daily_limit"] == proactive_out
    assert kwargs["feed_daily_limit"] == feed_out
