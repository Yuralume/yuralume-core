"""Pure rewrite / folding rules for a ``.lumebackup`` restore (CB3).

Everything in this module is deterministic and IO-free so the D4/D5
semantics stay unit-testable on their own:

- **Media key relocation** — where an archived original lands in the
  importer's storage. Prefix-preserving for the two character-scoped
  prefixes (fresh character id ⇒ collision-free), and *namespaced under
  the new character* for everything else (``users/{old}/…`` chat uploads
  and unknown prefixes). The namespacing is load-bearing: re-importing a
  backup into the *same* instance would otherwise re-``put`` — and, on
  failure cleanup, delete — objects the original character still
  references.
- **Id token rewriting** — D4's「單一映射表」: every carried row gets a
  fresh id up front, and every uuid-shaped token inside every carried
  string column is looked up against that one map. Free text mentioning
  an old id is rewritten to the new one on purpose (it refers to the
  same entity); tokens that aren't in the map (foreign / not-carried
  references) pass through untouched.
- **NSFW folding (D5, cloud only)** — write-time markers only, never
  content sniffing: ``messages.content_mode == 'nsfw'`` folds to the
  ``safe_summary`` (mirroring ``content_flow.sanitize_messages_for_tolerance``
  — content replaced, attachments cleared, mode back to normal) or drops
  the row when no summary exists; ``content_mode:nsfw``-tagged memories
  are skipped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from kokoro_link.application.services.nsfw_mode import (
    CONTENT_MODE_NORMAL,
    CONTENT_MODE_NSFW,
    MEMORY_TAG_NSFW_MODE,
)

_CHARACTER_KEY_PREFIXES = ("characters/", "feed/")
_USERS_KEY_PREFIX = "users/"

#: Uuid-shaped tokens as they appear in this codebase's ids:
#: ``str(uuid4())`` (36 chars, dashed) and ``uuid4().hex`` (32 hex).
#: Hex-boundary lookarounds keep the 32-hex alternative from matching
#: *inside* a longer hex run (a sha256 digest in some payload must never
#: be part-rewritten).
_ID_TOKEN_RE = re.compile(
    r"(?<![0-9a-fA-F])"
    r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{32})"
    r"(?![0-9a-fA-F])",
)

MESSAGE_FOLD_KEEP = "keep"
MESSAGE_FOLD_REPLACED = "replaced"
MESSAGE_FOLD_DROPPED = "dropped"


def rewrite_media_key(
    old_key: str,
    *,
    old_character_id: str,
    new_character_id: str,
    importer_segment: str,
) -> str:
    """Map an archived object key to its landing key on this instance."""
    for prefix in _CHARACTER_KEY_PREFIXES:
        scoped = f"{prefix}{old_character_id}/"
        if old_key.startswith(scoped):
            return f"{prefix}{new_character_id}/{old_key[len(scoped):]}"
    if old_key.startswith(_USERS_KEY_PREFIX):
        remainder = old_key[len(_USERS_KEY_PREFIX):]
        _old_user, _, rest = remainder.partition("/")
        rest = rest or _old_user
        return (
            f"{_USERS_KEY_PREFIX}{importer_segment}/restored/"
            f"{new_character_id}/{rest}"
        )
    # Unknown prefix (historic layouts): keep the material, namespace it
    # under the new character so nothing existing can be overwritten.
    return f"characters/{new_character_id}/restored/{old_key}"


def restored_media_key_prefixes(
    new_character_id: str,
    *,
    importer_segment: str,
) -> tuple[str, ...]:
    """Every prefix :func:`rewrite_media_key` can land an object under.

    The crash-recovery media sweep reverse-derives keys from the landed
    rows' URLs — but fail-soft columns keep their *original* URL when a
    referenced object was missing from the archive, and those reverse-
    derive to the **source character's** keys (A7). Deleting anything the
    rewrite could not have produced would destroy live media the original
    character still references, so the sweep must whitelist against this
    exact set — kept next to :func:`rewrite_media_key` so the two can
    never drift apart.
    """
    return (
        f"characters/{new_character_id}/",
        f"feed/{new_character_id}/",
        (
            f"{_USERS_KEY_PREFIX}{importer_segment}/restored/"
            f"{new_character_id}/"
        ),
    )


class TokenRewriter:
    """Single-pass uuid-token substitution against the restore id map."""

    __slots__ = ("_id_map",)

    def __init__(self, id_map: dict[str, str]) -> None:
        self._id_map = id_map

    def rewrite(self, text: str) -> str:
        if not text:
            return text
        id_map = self._id_map

        def _sub(match: re.Match[str]) -> str:
            token = match.group(0)
            return id_map.get(token, token)

        return _ID_TOKEN_RE.sub(_sub, text)


def memory_is_nsfw_flagged(tags_raw: str) -> bool:
    """D5 memory judgement — the write-time tag, nothing else."""
    try:
        parsed = json.loads(tags_raw or "[]")
    except ValueError:
        return False
    if not isinstance(parsed, list):
        return False
    return MEMORY_TAG_NSFW_MODE in parsed


def operator_profile_field_is_nsfw_flagged(content_mode: str) -> bool:
    """D5 operator-profile-field judgement — the write-time column only.

    ``operator_profile_fields`` carries the extracted operator fact
    verbatim in ``value`` (and quoted evidence in ``evidence_json``) and
    has no safe representation, so a hosted import skips the whole row —
    the same whole-row stance as an ``content_mode:nsfw`` memory. The
    read-side projection gate already blocks an nsfw field from prompt
    injection, but that leaves the raw fact *at rest*, which D5 forbids on
    hosted. Reads the column, never the value (no content sniffing)."""
    return (content_mode or "") == CONTENT_MODE_NSFW


def pending_follow_up_is_nsfw_flagged(messages_json: str) -> bool:
    """D5 pending-follow-up judgement — any queued message's write-time mode.

    ``messages_json`` stores the verbatim user turns as
    ``{content, content_mode, safe_summary?}``; the row is nsfw when any
    entry is ``content_mode == 'nsfw'``. The whole row is skipped rather
    than folded entry-by-entry because the row's *other* free-text columns
    — ``brief_reply`` / ``resolved_message`` / ``promise_intent`` — are the
    character's side of that same nsfw exchange (``resolved_message`` can
    be the full explicit reply when the original was community-routed) and
    carry no marker and no safe summary. Reading only the per-message
    write-time marker (never the content) keeps the D5 no-sniffing line."""
    try:
        parsed = json.loads(messages_json or "[]")
    except ValueError:
        return False
    if not isinstance(parsed, list):
        return False
    return any(
        isinstance(entry, dict)
        and entry.get("content_mode") == CONTENT_MODE_NSFW
        for entry in parsed
    )


def fold_message_kwargs(
    kwargs: dict, *, cloud_mode: bool,
) -> tuple[str, dict]:
    """Apply D5 to one ``messages`` row (as ``to_row_kwargs`` output).

    Returns ``(outcome, kwargs)`` where outcome is one of
    ``MESSAGE_FOLD_KEEP`` / ``MESSAGE_FOLD_REPLACED`` /
    ``MESSAGE_FOLD_DROPPED``. Self-host keeps every row verbatim.
    """
    if not cloud_mode:
        return MESSAGE_FOLD_KEEP, kwargs
    if kwargs.get("content_mode") != CONTENT_MODE_NSFW:
        return MESSAGE_FOLD_KEEP, kwargs
    safe_summary = (kwargs.get("safe_summary") or "").strip()
    if not safe_summary:
        return MESSAGE_FOLD_DROPPED, kwargs
    folded = dict(kwargs)
    folded["content"] = safe_summary
    folded["content_mode"] = CONTENT_MODE_NORMAL
    folded["attachments_json"] = "[]"
    return MESSAGE_FOLD_REPLACED, folded


@dataclass
class RestoreReport:
    """Import-report accumulator; serialised into the job's progress."""

    nsfw_messages_replaced: int = 0
    nsfw_messages_dropped: int = 0
    nsfw_memories_skipped: int = 0
    nsfw_operator_profile_fields_skipped: int = 0
    nsfw_pending_follow_ups_skipped: int = 0
    media_transferred: int = 0
    media_missing: list[str] = field(default_factory=list)
    rows_landed: dict[str, int] = field(default_factory=dict)
    rows_skipped: dict[str, int] = field(default_factory=dict)
    landed_arc_template_ids: list[str] = field(default_factory=list)
    landed_arc_series_ids: list[str] = field(default_factory=list)
    seed_refs_cleared: int = 0
    embedding_status: str = "pending"
    embedding_content_vectors: int = 0
    embedding_tag_vectors: int = 0

    def skip(self, table: str, count: int = 1) -> None:
        self.rows_skipped[table] = self.rows_skipped.get(table, 0) + count

    def to_progress(self) -> dict:
        return {
            "nsfw_messages_replaced": self.nsfw_messages_replaced,
            "nsfw_messages_dropped": self.nsfw_messages_dropped,
            "nsfw_memories_skipped": self.nsfw_memories_skipped,
            "nsfw_operator_profile_fields_skipped": (
                self.nsfw_operator_profile_fields_skipped
            ),
            "nsfw_pending_follow_ups_skipped": (
                self.nsfw_pending_follow_ups_skipped
            ),
            "media_transferred": self.media_transferred,
            "media_missing": list(self.media_missing),
            "rows_landed": dict(self.rows_landed),
            "rows_skipped": dict(self.rows_skipped),
            "landed_arc_template_ids": list(self.landed_arc_template_ids),
            "landed_arc_series_ids": list(self.landed_arc_series_ids),
            "seed_refs_cleared": self.seed_refs_cleared,
            "embedding": {
                "status": self.embedding_status,
                "content_vectors": self.embedding_content_vectors,
                "tag_vectors": self.embedding_tag_vectors,
            },
        }
