"""The public snapshot document.

This is the producer half of a contract shared with the Cloud customer
portal: the portal's public landing page fetches this JSON and renders
it. **Field names here are the contract** — renaming one silently blanks
a section of the public page, so the shape is written out once, in
:func:`build_snapshot`, and nowhere else.

```json
{
  "generated_at": "…",
  "character": {"name": {…}, "persona": {…}, "avatar_url": "…",
                "portrait_url": "…", "stats": {"days", "memories", "stories"}},
  "now": {"time": "22:10", "text": {…}} | null,
  "schedule": [{"time": "07:30", "text": {…}, "live": false}],
  "posts": [{"id", "kind", "created_at", "text": {…}, "image_url"?}]
}
```

Two rules the builder enforces rather than trusts:

* **No partial locales on a post.** A post arriving without every target
  locale is dropped with a warning. A wall where the Japanese visitor
  hits a Chinese paragraph mid-scroll is worse than a shorter wall. The
  caller (the control plane) owns the translation cache and decides when
  to pay for a missing locale; this function decides what ships.
* **Absolute URLs only.** Stored media URLs are server-relative
  (``/v1/public/…``); a page on another origin cannot resolve those, so
  ``base_url`` is applied here and a URL that cannot be made absolute is
  dropped rather than shipped broken.

Pure: everything it needs arrives as arguments, which is what makes the
schema testable without a database or a model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from kokoro_link.application.services.showcase.filters import PublicActivity

DEFAULT_LOCALES: tuple[str, ...] = ("zh", "en", "ja")

SOURCE_LOCALE = "zh"
"""The characters write in Chinese; every other locale is derived. The
snapshot's footer says so — honest labelling, not a per-post badge."""


class SnapshotError(RuntimeError):
    """Inputs that make a correct document impossible (locales without the
    source language, an unusable base URL). Always fatal — the alternative
    is publishing a document the portal renders wrong."""


@dataclass(frozen=True, slots=True)
class CharacterCard:
    """Character-side facts, already resolved from the domain layer.

    ``persona`` is the character summary; ``days`` / ``memories`` /
    ``stories`` are the three numbers that make the wall's claim concrete
    ("she has been alive for 214 days") — see
    :mod:`kokoro_link.application.services.showcase.service` for how each
    is counted.
    """

    name: str
    persona: str
    portrait_url: str | None
    days: int
    memories: int
    stories: int
    avatar_url: str | None = None
    """Defaults to the portrait when unset: both come from
    ``characters.image_urls[0]``, and the portal picks a rendition via the
    existing ``?v=`` variant query rather than being handed two files."""


@dataclass(frozen=True, slots=True)
class SnapshotPost:
    """An approved post as the publisher hands it over.

    ``text`` is locale → final text, already carrying whatever the owner
    approved (the original or their rewrite) plus its translations. This
    type deliberately has no notion of "decision" or "review": approval
    happened in the control plane, and a post that reaches here is one
    that was said yes to.
    """

    id: str
    kind: str
    created_at: str
    text: Mapping[str, str]
    image_url: str | None = None


@dataclass(slots=True)
class SnapshotResult:
    document: dict[str, object]
    warnings: list[str] = field(default_factory=list)
    skipped_posts: list[str] = field(default_factory=list)

    @property
    def post_count(self) -> int:
        posts = self.document.get("posts")
        return len(posts) if isinstance(posts, list) else 0


def build_snapshot(
    *,
    card: CharacterCard,
    posts: Sequence[SnapshotPost],
    now_entry: PublicActivity | None,
    schedule: Sequence[PublicActivity],
    base_url: str,
    locales: Sequence[str] = DEFAULT_LOCALES,
    translate=None,  # noqa: ANN001 — (text, locale) -> str | None
    generated_at: datetime | None = None,
) -> SnapshotResult:
    """Assemble the document.

    ``posts`` arrive fully translated — this function never calls a model
    for post bodies, only for the ephemeral strings (card, now, schedule)
    that change every run and therefore cannot be cached against an
    approval.
    """
    resolved_locales = _validate_locales(locales)
    root = _validate_base_url(base_url)
    result = SnapshotResult(document={})
    portrait = _absolute(card.portrait_url, root, result, label="portrait_url")
    avatar_source = card.avatar_url or card.portrait_url
    avatar = _absolute(avatar_source, root, result, label="avatar_url")

    result.document = {
        "generated_at": _iso(generated_at or datetime.now(timezone.utc)),
        "character": {
            "name": _live_text(card.name, resolved_locales, translate, result),
            "persona": _live_text(
                card.persona, resolved_locales, translate, result,
            ),
            "avatar_url": avatar or "",
            "portrait_url": portrait or "",
            "stats": {
                "days": card.days,
                "memories": card.memories,
                "stories": card.stories,
            },
        },
        "now": _activity_json(
            now_entry, resolved_locales, translate, result, include_live=False,
        ),
        "schedule": [
            entry
            for entry in (
                _activity_json(
                    activity,
                    resolved_locales,
                    translate,
                    result,
                    include_live=True,
                )
                for activity in schedule
            )
            if entry is not None
        ],
        "posts": _posts_json(posts, resolved_locales, root, result),
    }
    return result


def _posts_json(
    posts: Sequence[SnapshotPost],
    locales: Sequence[str],
    root: str,
    result: SnapshotResult,
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for post in posts:
        text: dict[str, str] = {}
        missing: list[str] = []
        for locale in locales:
            value = (post.text.get(locale) or "").strip()
            if value:
                text[locale] = value
            else:
                missing.append(locale)
        if missing:
            result.skipped_posts.append(post.id)
            result.warnings.append(
                f"post {post.id}: 缺 {','.join(missing)} 譯文，跳過"
                "（不出殘缺語系）",
            )
            continue
        payload: dict[str, object] = {
            "id": post.id,
            "kind": post.kind,
            "created_at": post.created_at,
            "text": text,
        }
        image = _absolute(
            post.image_url, root, result, label=f"post {post.id} image_url",
        )
        if image:
            payload["image_url"] = image
        out.append(payload)
    return out


def _activity_json(
    activity: PublicActivity | None,
    locales: Sequence[str],
    translate,  # noqa: ANN001
    result: SnapshotResult,
    *,
    include_live: bool,
) -> dict[str, object] | None:
    """``include_live=False`` for ``now``: the contract gives that object
    ``time`` + ``text`` only, and "the current block is live" is a
    tautology anyway."""
    if activity is None:
        return None
    payload: dict[str, object] = {
        "time": activity.time,
        "text": _live_text(activity.text, locales, translate, result),
    }
    if include_live:
        payload["live"] = bool(activity.live)
    return payload


def _live_text(
    source: str,
    locales: Sequence[str],
    translate,  # noqa: ANN001
    result: SnapshotResult,
) -> dict[str, str]:
    """Translate an ephemeral string into every locale.

    Unlike posts, these fall back to the source text rather than being
    dropped: the schema has no way to express "this character has no
    name in English", and a missing key breaks the portal's render. The
    degradation is visible (Chinese on an English page) and warned about,
    which is the right trade for a label — but explicitly *not* the trade
    made for post bodies."""
    text = (source or "").strip()
    values: dict[str, str] = {}
    for locale in locales:
        if locale == SOURCE_LOCALE or not text:
            values[locale] = text
            continue
        translated = translate(text, locale) if translate is not None else None
        if translated is None:
            values[locale] = text
            result.warnings.append(
                f"未能翻譯成 {locale}，該欄位沿用中文原文：{text[:24]!r}",
            )
        else:
            values[locale] = translated
    return values


def _absolute(
    url: str | None, root: str, result: SnapshotResult, *, label: str,
) -> str | None:
    if not url:
        return None
    trimmed = url.strip()
    if trimmed.startswith(("http://", "https://")):
        return trimmed
    if not trimmed.startswith("/"):
        result.warnings.append(
            f"{label}: 無法絕對化的 URL {trimmed!r}，已略過",
        )
        return None
    return f"{root}{trimmed}"


def _validate_locales(locales: Sequence[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for locale in locales:
        value = (locale or "").strip()
        if value and value not in cleaned:
            cleaned.append(value)
    if SOURCE_LOCALE not in cleaned:
        raise SnapshotError(
            f"locales must include {SOURCE_LOCALE!r}: it is the source "
            "language every other locale is derived from",
        )
    return tuple(cleaned)


def _validate_base_url(base_url: str) -> str:
    value = (base_url or "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SnapshotError(
            f"base_url must be an absolute http(s) URL, got {base_url!r}",
        )
    return value


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_LOCALES",
    "SOURCE_LOCALE",
    "CharacterCard",
    "SnapshotError",
    "SnapshotPost",
    "SnapshotResult",
    "build_snapshot",
]
