"""LumeGram + album backup DTOs (plan §3.1「LumeGram」「相簿」).

``feed_posts`` + ``feed_reactions`` + ``feed_comments`` and
``character_album_items``. Media URLs inside these rows are collected as
archive assets by the export job (DB is the authoritative media index)
and rewritten to the importer's storage on restore.
"""

from __future__ import annotations

from datetime import datetime

from kokoro_link.application.dto.character_backup.base import BackupRecord


class FeedPostBackupRecord(BackupRecord):
    id: str
    character_id: str
    kind: str = "daily"
    content_text: str
    source_kind: str = "manual"
    source_ref_id: str | None = None
    image_url: str | None = None
    image_prompt: str | None = None
    video_url: str | None = None
    video_prompt: str | None = None
    likes_count: int = 0
    comments_count: int = 0
    reactions_seen_at: datetime | None = None
    viewed_at: datetime | None = None
    created_at: datetime


class FeedReactionBackupRecord(BackupRecord):
    id: str
    post_id: str
    liker_id: str
    created_at: datetime


class FeedCommentBackupRecord(BackupRecord):
    id: str
    post_id: str
    author_id: str
    content_text: str
    created_at: datetime


class CharacterAlbumItemBackupRecord(BackupRecord):
    id: str
    character_id: str
    url: str
    source: str = "tool"
    caption: str | None = None
    byte_size: int | None = None
    created_at: datetime
