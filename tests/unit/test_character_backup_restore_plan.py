"""Unit tests for the pure CB3 restore rules (no IO, no DB).

Pinned: media-key relocation (collision-free by construction), uuid
token rewriting against the single id map, and the D5 folding decisions
(write-time markers only).
"""

from __future__ import annotations

import json

from kokoro_link.application.services.character_backup_restore_plan import (
    MESSAGE_FOLD_DROPPED,
    MESSAGE_FOLD_KEEP,
    MESSAGE_FOLD_REPLACED,
    TokenRewriter,
    fold_message_kwargs,
    memory_is_nsfw_flagged,
    operator_profile_field_is_nsfw_flagged,
    pending_follow_up_is_nsfw_flagged,
    rewrite_media_key,
)

OLD_CHAR = "5f0c9d1e-1111-4222-8333-444455556666"
NEW_CHAR = "aabbccdd-9999-4888-b777-666655554444"


# ---------------------------------------------------------------------------
# Media key relocation
# ---------------------------------------------------------------------------


def test_character_scoped_prefixes_are_preserved() -> None:
    assert rewrite_media_key(
        f"characters/{OLD_CHAR}/album/a1.png",
        old_character_id=OLD_CHAR,
        new_character_id=NEW_CHAR,
        importer_segment="bob",
    ) == f"characters/{NEW_CHAR}/album/a1.png"
    assert rewrite_media_key(
        f"feed/{OLD_CHAR}/post1.png",
        old_character_id=OLD_CHAR,
        new_character_id=NEW_CHAR,
        importer_segment="bob",
    ) == f"feed/{NEW_CHAR}/post1.png"


def test_users_prefix_is_rebased_and_namespaced() -> None:
    """Chat uploads move under the importer AND under the new character:
    re-importing your own backup on the same instance must never write
    (or, on cleanup, delete) the original object's key."""
    new_key = rewrite_media_key(
        "users/alice/chat-uploads/photo.png",
        old_character_id=OLD_CHAR,
        new_character_id=NEW_CHAR,
        importer_segment="alice",
    )
    assert new_key == (
        f"users/alice/restored/{NEW_CHAR}/chat-uploads/photo.png"
    )
    assert new_key != "users/alice/chat-uploads/photo.png"


def test_unknown_prefix_is_namespaced_under_the_new_character() -> None:
    assert rewrite_media_key(
        "legacy/things/x.png",
        old_character_id=OLD_CHAR,
        new_character_id=NEW_CHAR,
        importer_segment="bob",
    ) == f"characters/{NEW_CHAR}/restored/legacy/things/x.png"


# ---------------------------------------------------------------------------
# Token rewriting
# ---------------------------------------------------------------------------


def test_token_rewriter_maps_both_uuid_shapes_and_keeps_foreign() -> None:
    dashed_old = "0d9f8c7b-6a5e-4d3c-9b2a-1f0e9d8c7b6a"
    hex_old = "0123456789abcdef0123456789abcdef"
    dashed_new = "11111111-2222-4333-8444-555555555555"
    hex_new = "ffffffffffffffffffffffffffffffff"
    rewriter = TokenRewriter({dashed_old: dashed_new, hex_old: hex_new})
    payload = json.dumps({
        "added_memory_ids": [dashed_old],
        "note": f"see {hex_old} and keep deadbeef",
        "foreign": "9999aaaa-bbbb-4ccc-8ddd-eeeeffff0000",
    })
    out = rewriter.rewrite(payload)
    assert dashed_new in out and dashed_old not in out
    assert hex_new in out and hex_old not in out
    # Unmapped uuid-shaped token passes through untouched.
    assert "9999aaaa-bbbb-4ccc-8ddd-eeeeffff0000" in out
    assert "deadbeef" in out


def test_token_rewriter_never_rewrites_inside_longer_hex_runs() -> None:
    hex_old = "0123456789abcdef0123456789abcdef"
    rewriter = TokenRewriter({hex_old: "f" * 32})
    digest = hex_old + "aa11bb22cc33dd44ee55ff6600112233"  # 64-hex sha256
    assert rewriter.rewrite(f'{{"sha256": "{digest}"}}') == (
        f'{{"sha256": "{digest}"}}'
    )


# ---------------------------------------------------------------------------
# D5 folding
# ---------------------------------------------------------------------------


def test_fold_replaces_with_safe_summary_and_clears_attachments() -> None:
    outcome, folded = fold_message_kwargs(
        {
            "content": "raw nsfw text",
            "content_mode": "nsfw",
            "safe_summary": "兩人聊得很開心。",
            "attachments_json": '[{"url": "/uploads/x.png"}]',
        },
        cloud_mode=True,
    )
    assert outcome == MESSAGE_FOLD_REPLACED
    assert folded["content"] == "兩人聊得很開心。"
    assert folded["content_mode"] == "normal"
    assert folded["attachments_json"] == "[]"


def test_fold_drops_without_summary_and_self_host_keeps_verbatim() -> None:
    nsfw = {"content": "raw", "content_mode": "nsfw", "safe_summary": ""}
    assert fold_message_kwargs(dict(nsfw), cloud_mode=True)[0] == (
        MESSAGE_FOLD_DROPPED
    )
    outcome, kept = fold_message_kwargs(dict(nsfw), cloud_mode=False)
    assert outcome == MESSAGE_FOLD_KEEP
    assert kept["content"] == "raw"
    assert kept["content_mode"] == "nsfw"


def test_memory_flag_reads_the_tag_not_the_content() -> None:
    assert memory_is_nsfw_flagged('["content_mode:nsfw"]') is True
    assert memory_is_nsfw_flagged('["nsfw prose mentioned"]') is False
    assert memory_is_nsfw_flagged("not-json") is False


def test_operator_profile_field_flag_reads_the_mode_column() -> None:
    # Write-time column only — the ``value`` text is never inspected.
    assert operator_profile_field_is_nsfw_flagged("nsfw") is True
    assert operator_profile_field_is_nsfw_flagged("normal") is False
    assert operator_profile_field_is_nsfw_flagged("") is False


def test_pending_follow_up_flag_reads_any_embedded_message_mode() -> None:
    nsfw_row = json.dumps([
        {"content": "safe hi", "content_mode": "normal"},
        {"content": "raw nsfw", "content_mode": "nsfw",
         "safe_summary": "兩人聊得很開心。"},
    ])
    normal_row = json.dumps([
        {"content": "hi", "content_mode": "normal"},
        {"content": "how are you", "content_mode": "normal"},
    ])
    # Any single nsfw-marked queued turn flags the whole row — the row's
    # unmarked reply free-text belongs to the same exchange.
    assert pending_follow_up_is_nsfw_flagged(nsfw_row) is True
    assert pending_follow_up_is_nsfw_flagged(normal_row) is False
    # A prose string mentioning "nsfw" is NOT the marker (no sniffing).
    assert pending_follow_up_is_nsfw_flagged(
        json.dumps([{"content": "we talked about nsfw stuff",
                     "content_mode": "normal"}]),
    ) is False
    assert pending_follow_up_is_nsfw_flagged("[]") is False
    assert pending_follow_up_is_nsfw_flagged("not-json") is False
