"""Ports for account runtime policy usage events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


ACCOUNT_RUNTIME_EVENT_CHARACTER_CREATE = "character_create"
ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE = "chat_image"
ACCOUNT_RUNTIME_EVENT_FEED_POST = "feed_post"

#: CV5 volume control — one row per feed video generation *attempt* (the
#: decision to enter the async video pipeline / legacy synchronous video
#: path), counted over a rolling 24h window per operator. Recorded at the
#: same choke point the ``video_daily_limit`` check runs
#: (``FeedComposerService._video_volume_allows``), before any storyboard
#: call or first-frame render — mirrors ``daily_chat_image_limit``'s
#: check-then-record shape (``chat_service._reserve_runtime_chat_image_quota``),
#: not ``feed_post``'s atomic claim/discard: a downstream degrade (timeout,
#: Gateway/broker 429) never rolls this back, because the *decision* to
#: attempt a video already happened and self-host never records or counts
#: these (the knob does not exist there).
ACCOUNT_RUNTIME_EVENT_FEED_VIDEO = "feed_video"

#: CB2 hosted export throttle — one row per started ``.lumebackup`` export,
#: counted over a rolling 24h window per operator (mirrors the
#: ``character_create`` ledger precedent in ``character_service``). Cloud-mode
#: only; self-host never records or counts these.
ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_EXPORT = "character_backup_export"

#: CB3 hosted upload throttle (adversarial-review A6) — one row per staged
#: ``.lumebackup`` upload, counted over a rolling 24h window per operator.
#: Same shape and stance as the export throttle above: cloud-mode only,
#: fail-closed when no ledger is wired; self-host never records or counts
#: these. Without it, 2 GiB × unlimited uploads × 24h staging TTL fills the
#: hosted disk overnight.
ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_UPLOAD = "character_backup_upload"

#: S4 hosted preview throttle — one row per ``.lumebackup`` preview, counted
#: over a rolling 24h window per operator. Same shape and fail-closed stance
#: as the upload throttle above. Preview is unthrottled otherwise, yet each
#: call downloads the full staged archive (up to 2 GiB) and runs a scrypt
#: KDF: a loop over one staged upload is a per-call CPU + memory + bandwidth
#: amplifier. Cloud-mode only; self-host never records or counts these.
ACCOUNT_RUNTIME_EVENT_CHARACTER_BACKUP_PREVIEW = "character_backup_preview"

#: AP4 quota-overage purchases. Counted separately from the quota events
#: above because they answer a different question: not "how much of the tier
#: allowance is spent" but "how many extra slots has this player *bought*
#: today", which is what ``daily_overage_limit`` bounds. Sharing the base
#: event type would make the ceiling unenforceable — the base counter is
#: already at its own limit by the time an overage is even considered.
ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE_OVERAGE = "chat_image_overage"
ACCOUNT_RUNTIME_EVENT_FEED_POST_OVERAGE = "feed_post_overage"

#: AP4 consent trail. Every move of an overage switch — by the player at the
#: settings surface, or by the service revoking a stale agreement after a price
#: rise — appends one of these, carrying the action key, the direction and the
#: price in ``resource_id``. Not a limit counter: nothing counts these, they
#: exist so "who agreed to spend what, and when" is answerable from the same
#: append-only ledger that already survives character deletion.
ACCOUNT_RUNTIME_EVENT_OVERAGE_CONSENT = "overage_consent"


@dataclass(frozen=True, slots=True)
class AccountRuntimeUsageEvent:
    operator_id: str
    event_type: str
    occurred_at: datetime
    resource_id: str | None = None


class AccountRuntimeUsageRepositoryPort(Protocol):
    """Record and query account-level events that drive runtime limits."""

    async def record_event(
        self,
        *,
        operator_id: str,
        event_type: str,
        occurred_at: datetime,
        resource_id: str | None = None,
    ) -> None: ...

    async def count_events(
        self,
        *,
        operator_id: str,
        event_type: str,
        since: datetime,
        until: datetime | None = None,
        resource_id: str | None = None,
    ) -> int: ...

    async def claim_event_slot(
        self,
        *,
        operator_id: str,
        event_type: str,
        occurred_at: datetime,
        since: datetime,
        limit: int,
        resource_id: str | None = None,
        resource_limit: int | None = None,
    ) -> str | None:
        """Take one of at most ``limit`` slots in the window, atomically.

        Returns the recorded event's id, or ``None`` when the account window or
        optional per-resource window is full. ``resource_limit`` is applied only
        when ``resource_id`` is non-empty; callers use it to make a character's
        reserved first slot single-flight without inventing another ledger.

        Counting first and recording afterwards cannot enforce a ceiling: two
        concurrent ticks (several characters of one operator, several hosted
        replicas) both read the same "one slot left" and both spend. Claiming
        the slot *by writing it* and only then counting makes every racer
        visible to every other racer, so the window can never exceed ``limit``.
        A tie may cost both racers a free slot; over-denial is the safe
        direction here. Callers must :meth:`discard_event` a claim they end up
        not using.
        """
        ...

    async def discard_event(self, *, event_id: str) -> None:
        """Undo a claim whose work never happened."""
        ...

    async def list_events(
        self,
        *,
        event_type: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[AccountRuntimeUsageEvent]: ...
