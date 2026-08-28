"""The send half of the LINE dormant-reactivation campaign (LR T2).

An operator selects dormant characters in the console and presses send;
this service turns that press into a durable ledger row plus one
background task that walks the selection **serially**, calling the
ordinary proactive dispatcher once per character.

Four decisions the plan pins down, and where each one lives here:

* **D1 — the list is not a licence.** An operator reads the candidate
  list, selects, confirms; the walk then takes as long as a few hundred
  model calls take. A player can come back inside that window, so every
  row is re-checked against D1 immediately before its send (see
  :mod:`.dormancy`) and a returned player is recorded as skipped rather
  than interrupted mid-conversation.
* **D4 — no shortcut around arbitration.** The runner's only send verb is
  :meth:`ProactiveEvaluatorPort.evaluate`, the same entry point a
  scheduler tick uses. Every gate, quota and channel rule downstream
  applies unchanged; this module owns *which* characters and *in what
  order*, never *whether the message is allowed*.
* **D5 — idempotency lives in the ledger, not in the runner.** ``outcome
  IS NULL`` is the pending marker and ``(campaign_id, character_id)`` is
  the item key, so "what still needs doing" is a query rather than
  in-memory state. A Core restart mid-campaign loses the task and
  nothing else: the next POST of the same id resumes exactly the rows
  that never got an outcome.
* **D6 — a blocked row is a result, not a retry.** Whatever the
  dispatcher answers is written down verbatim, including
  ``gate_blocked`` from the night-hours floor. Re-sending is an operator
  decision, expressed as a new campaign.

**Where the cross-replica fence is, and where it is not.** Hosted runs
more than one API replica behind a round-robin with no stickiness, so the
same ``campaign_id`` can be POSTed to replica A and then, on the
console's retry, to replica B — which resumes it and walks the same
pending rows A is still working through. ``record_outcome``'s ``WHERE
outcome IS NULL`` does **not** save that situation: it fences the write,
which happens *after* the message is already in front of the player. The
fence that matters is :meth:`…RepositoryPort.claim_item`, taken before
the dispatcher is called at all; :attr:`_runners` is only the
same-process convenience guard against a double-click.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from kokoro_link.application.services.line_reactivation.dormancy import (
    RecallTargetGuard,
)
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.line_reactivation import (
    CAMPAIGN_STATUS_RUNNING,
    LineReactivationCampaign,
    LineReactivationCampaignConflictError,
    LineReactivationCampaignRepositoryPort,
)
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger

_LOGGER = logging.getLogger(__name__)

MAX_DETAIL_CHARS = 500
"""Ceiling on the per-item ``detail`` string.

The column is ``TEXT`` so this is not a storage limit — it is a report
limit. ``detail`` is read by a human scanning a table of a few hundred
rows; a stack-trace-length gate reason turns that table into a wall.
"""

MAX_CAMPAIGN_ID_CHARS = 64
"""Matches ``line_reactivation_campaigns.campaign_id``'s ``String(64)``.

Enforced here rather than left to the database because the database's
answer is a driver-level ``DataError`` on commit — a 500 for what is a
malformed request. The console mints UUIDs (36 chars); anything longer
is a client defect, and it should read as one.
"""

DEFAULT_ITEM_CLAIM_LEASE = timedelta(minutes=15)
"""How long one runner's claim on an item keeps other runners off it.

Must exceed the worst-case time for a single recall message — a full
proactive generation: intention judge, decider, composition, quality and
honesty gates, then the channel round trip. Fifteen minutes is far above
the observed ceiling; the cost of being generous is only that a row
orphaned by a replica that died mid-send waits that long before a resume
can re-run it, while the cost of being stingy is the double send this
lease exists to prevent.
"""

OUTCOME_SKIPPED_NOT_DORMANT = "skipped_not_dormant"
"""Recorded when the pre-send D1 re-check refuses a row.

Deliberately *not* a :class:`ProactiveOutcome`: nothing was dispatched,
so there is no proactive attempt to name. The ``detail`` carries which
condition changed (``no_longer_dormant``, ``frozen``, …).
"""


class LineReactivationEmptySelectionError(ValueError):
    """A campaign was started with no characters.

    Refused rather than accepted as a zero-item run: an empty selection
    is a console bug or a lost payload, and recording it as a completed
    campaign would make the ledger agree that nothing was meant to
    happen. The API layer maps this to 400.
    """


class LineReactivationInvalidCampaignIdError(ValueError):
    """``campaign_id`` was blank or longer than the column allows.

    A separate error from the empty selection because it is a different
    client defect with a different fix, and because leaving it to the
    database turns a malformed request into a 500. The API layer maps
    this to 400 ``invalid_campaign_id``.
    """


class LineReactivationUnknownCharactersError(ValueError):
    """The selection named characters that do not exist.

    Validated up front so the item rows' foreign key can never be the
    thing that reports it. That failure arrives as an ``IntegrityError``
    indistinguishable from a re-used ``campaign_id``, and answering it
    with 409 ``campaign_conflict`` would send the console hunting a fresh
    id for a condition no id can fix — a stale candidate list, most
    likely, naming a character deleted since it was loaded.
    """

    def __init__(self, missing_character_ids: Sequence[str]) -> None:
        self.missing_character_ids = tuple(missing_character_ids)
        super().__init__(
            "unknown character ids in selection: "
            + ", ".join(self.missing_character_ids),
        )


class ProactiveEvaluatorPort(Protocol):
    """The one dispatcher method this service needs.

    Narrowed deliberately: the concrete ``ProactiveDispatcher`` is a very
    large object, and stating the dependency as "something that can
    evaluate one character" keeps the runner testable with a stub and
    makes it obvious that no other dispatcher capability is reachable
    from here.
    """

    async def evaluate(
        self,
        *,
        character_id: str,
        trigger: ProactiveTrigger,
        now: datetime | None = ...,
        logical_slot: str | None = ...,
    ) -> ProactiveAttempt:
        ...


@dataclass(frozen=True, slots=True)
class CampaignStartResult:
    """What the POST answers: enough to start polling, nothing more."""

    campaign_id: str
    status: str
    total: int
    resumed: bool
    """``True`` when this id already existed and the same selection was
    re-submitted — the console's retry after a dropped response, or an
    operator resuming a campaign a restart interrupted."""


@dataclass(frozen=True, slots=True)
class _ItemVerdict:
    """What one character's walk decided, before it is written down.

    A named shape rather than a tuple because ``detail`` and
    ``message_text`` are both optional strings sitting next to each
    other: transposed positionally, the report would show the gate reason
    where the operator expects the message and nothing would fail.
    """

    outcome: str
    detail: str | None
    message_text: str | None = None


@dataclass(frozen=True, slots=True)
class CampaignItemReport:
    character_id: str
    character_name: str
    outcome: str | None
    detail: str | None
    attempted_at: datetime | None
    message_text: str | None = None
    """The verbatim body this character sent, or ``None``.

    Non-null only on ``outcome == "sent"`` — the operator reads this
    column to judge whether the recall lands before releasing the rest of
    the selection, and a body that was blocked or never delivered would
    make that judgement about a message no player saw."""


@dataclass(frozen=True, slots=True)
class CampaignReport:
    campaign_id: str
    status: str
    actor: str
    created_at: datetime
    completed_at: datetime | None
    total: int
    done: int
    items: tuple[CampaignItemReport, ...]


class LineReactivationCampaignService:
    """Start, resume and report on reactivation campaigns."""

    def __init__(
        self,
        *,
        repository: LineReactivationCampaignRepositoryPort,
        dispatcher: ProactiveEvaluatorPort,
        character_repository: CharacterRepositoryPort,
        profile_resolver: AccountRuntimeProfileResolverPort,
        clock: ClockPort | None = None,
        claim_lease: timedelta = DEFAULT_ITEM_CLAIM_LEASE,
    ) -> None:
        self._repository = repository
        self._dispatcher = dispatcher
        self._characters = character_repository
        self._clock = clock
        self._claim_lease = claim_lease
        self._guard = RecallTargetGuard(
            character_repository=character_repository,
            profile_resolver=profile_resolver,
        )
        self._runners: dict[str, asyncio.Task[None]] = {}
        # Serialises the read-then-create in :meth:`start`. Without it two
        # simultaneous POSTs of a new id both see ``get() is None`` and
        # both try to create; the repository would refuse the second with
        # a conflict, which is the *wrong* answer for an identical
        # selection (it should read as a resume).
        self._start_lock = asyncio.Lock()

    async def start(
        self,
        *,
        campaign_id: str,
        character_ids: Sequence[str],
        actor: str,
        now: datetime | None = None,
    ) -> CampaignStartResult:
        """Create-or-resume one campaign and make sure a runner is walking it.

        Raises :class:`LineReactivationInvalidCampaignIdError` for a
        malformed id, :class:`LineReactivationEmptySelectionError` for an
        empty selection, :class:`LineReactivationUnknownCharactersError`
        when the selection names characters that do not exist, and
        :class:`LineReactivationCampaignConflictError` when the id is
        already bound to a *different* set of characters (D5).
        """

        campaign_id = _validate_campaign_id(campaign_id)
        selection = _normalise_ids(character_ids)
        if not selection:
            raise LineReactivationEmptySelectionError(
                "character_ids must contain at least one character",
            )

        async with self._start_lock:
            existing = await self._repository.get(campaign_id)
            if existing is None:
                # Before creating, not after failing: the item rows'
                # foreign key would report a missing character as an
                # ``IntegrityError`` the adapter cannot tell apart from a
                # duplicate id. Only the create path needs this — a
                # resume's rows already exist, and a character deleted
                # since then is handled by the runner's pre-send check.
                await self._assert_characters_exist(selection)
                campaign = LineReactivationCampaign(
                    campaign_id=campaign_id,
                    actor=actor,
                    status=CAMPAIGN_STATUS_RUNNING,
                    created_at=self._resolve_now(now),
                    total=len(selection),
                )
                try:
                    await self._repository.create(campaign, selection)
                except LineReactivationCampaignConflictError:
                    # Another process won the race between our ``get``
                    # and this ``create``. Fall through to the resume
                    # branch, which re-reads and applies the same
                    # same-selection test — a genuine mismatch still 409s.
                    return await self._resume(campaign_id, selection)
                self._ensure_runner(campaign_id)
                return CampaignStartResult(
                    campaign_id=campaign_id,
                    status=campaign.status,
                    total=campaign.total,
                    resumed=False,
                )
            return await self._resume(campaign_id, selection, existing=existing)

    async def report(self, campaign_id: str) -> CampaignReport | None:
        """The console's polling view, or ``None`` for an unknown id."""

        campaign = await self._repository.get(campaign_id)
        if campaign is None:
            return None
        items = await self._repository.list_items(campaign_id)
        names = await self._resolve_names(
            tuple(item.character_id for item in items),
        )
        rendered = tuple(
            CampaignItemReport(
                character_id=item.character_id,
                character_name=names.get(
                    item.character_id, item.character_id,
                ),
                outcome=item.outcome,
                detail=item.detail,
                message_text=item.message_text,
                attempted_at=item.attempted_at,
            )
            for item in items
        )
        return CampaignReport(
            campaign_id=campaign.campaign_id,
            status=campaign.status,
            actor=campaign.actor,
            created_at=campaign.created_at,
            completed_at=campaign.completed_at,
            total=campaign.total,
            # Counted from the rows rather than trusted from ``total``:
            # ``done`` is what the progress bar reads, and it must be the
            # ledger's answer, not a number this process remembers.
            done=sum(1 for item in rendered if item.outcome is not None),
            items=rendered,
        )

    async def wait_for_idle(self) -> None:
        """Await every in-process runner. Test seam; no production caller."""

        while True:
            tasks = [task for task in self._runners.values() if not task.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # resume / runner plumbing
    # ------------------------------------------------------------------

    async def _resume(
        self,
        campaign_id: str,
        selection: tuple[str, ...],
        *,
        existing: LineReactivationCampaign | None = None,
    ) -> CampaignStartResult:
        campaign = existing or await self._repository.get(campaign_id)
        if campaign is None:  # pragma: no cover - defensive
            raise LineReactivationCampaignConflictError(
                f"campaign {campaign_id!r} vanished mid-resume",
            )
        items = await self._repository.list_items(campaign_id)
        stored = {item.character_id for item in items}
        if stored != set(selection):
            raise LineReactivationCampaignConflictError(
                f"campaign {campaign_id!r} already exists with a different "
                f"selection ({len(stored)} stored vs {len(selection)} sent)",
            )
        if campaign.running:
            # Two situations land here and both want a runner: a Core
            # restart (the ledger still says running, and no task in this
            # process owns it because the process is new), and a POST that
            # round-robined onto a second replica while the first is still
            # walking. The second one is safe *because* of the per-item
            # claim — this walker will simply find every row the other one
            # currently owns unclaimable and pass over it.
            self._ensure_runner(campaign_id)
        return CampaignStartResult(
            campaign_id=campaign_id,
            status=campaign.status,
            total=campaign.total,
            resumed=True,
        )

    def _ensure_runner(self, campaign_id: str) -> None:
        existing = self._runners.get(campaign_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run(campaign_id),
            name=f"line-reactivation-campaign:{campaign_id}",
        )
        self._runners[campaign_id] = task
        task.add_done_callback(self._forget_runner)

    def _forget_runner(self, task: asyncio.Task[None]) -> None:
        for campaign_id, candidate in list(self._runners.items()):
            if candidate is task:
                self._runners.pop(campaign_id, None)
                break
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            # The per-item loop already absorbs per-character failures, so
            # anything arriving here broke the walk itself (ledger read,
            # most likely). Surface it; the campaign stays ``running`` and
            # a re-POST resumes it.
            _LOGGER.error(
                "line reactivation campaign runner failed task=%s",
                task.get_name(),
                exc_info=error,
            )

    async def _run(self, campaign_id: str) -> None:
        pending = await self._repository.list_pending_items(campaign_id)
        for item in pending:
            await self._attempt(campaign_id, item.character_id)
        # Re-read rather than assuming the loop drained everything. Three
        # things leave a row behind: an outcome write that failed, a row
        # another replica's walker is still holding, and a row whose
        # claim this walker lost. Marking the campaign completed over any
        # of them would tell the operator a character was handled when it
        # was not — so the last walker standing (the one whose re-read
        # finds nothing pending) is the one that closes the campaign, and
        # ``mark_completed`` is itself fenced on ``status = running`` so
        # two of them finishing together still stamp it once.
        if await self._repository.list_pending_items(campaign_id):
            _LOGGER.warning(
                "line reactivation campaign %s still has pending items after "
                "a full pass; leaving it running for a resume",
                campaign_id,
            )
            return
        await self._repository.mark_completed(
            campaign_id, completed_at=self._resolve_now(None),
        )

    async def _attempt(self, campaign_id: str, character_id: str) -> None:
        """Claim, then decide, then record — strictly in that order.

        The claim comes first because it is the only thing that makes the
        decide step exclusive; every earlier version of this walk decided
        first and discovered the collision at record time, by which point
        the player had the message.
        """

        now = self._resolve_now(None)
        if not await self._claim(campaign_id, character_id, now):
            return
        verdict = await self._decide(campaign_id, character_id, now)
        await self._record(campaign_id, character_id, verdict)

    async def _claim(
        self, campaign_id: str, character_id: str, now: datetime,
    ) -> bool:
        try:
            return await self._repository.claim_item(
                campaign_id,
                character_id,
                now=now,
                lease=self._claim_lease,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A ledger blip must not end the walk, and it must not be
            # read as permission either: no claim, no send.
            _LOGGER.exception(
                "line reactivation claim failed campaign=%s character=%s",
                campaign_id,
                character_id,
            )
            return False

    async def _decide(
        self, campaign_id: str, character_id: str, now: datetime,
    ) -> _ItemVerdict:
        """What to write for this character."""

        try:
            skip_reason = await self._guard.check(character_id, now=now)
            if skip_reason is not None:
                # D1 re-asserted at send time: the player came back, or
                # the character was frozen/deleted since selection. Not
                # dispatched at all — recorded so the operator sees why.
                return _ItemVerdict(
                    outcome=OUTCOME_SKIPPED_NOT_DORMANT, detail=skip_reason,
                )
            attempt = await self._dispatcher.evaluate(
                character_id=character_id,
                trigger=ProactiveTrigger.ADMIN_REACTIVATION,
                # No ``logical_slot``: slot claiming (P3-Dedup) exists so
                # two scheduler replicas cannot both take the same tick.
                # This item is already single-owner — the ledger claim
                # says so — and claiming a slot would make one operator
                # press collide with the character's ordinary tick for no
                # benefit; same choice the player-facing evaluate-now
                # route makes.
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One character's blow-up must not end the campaign; the
            # remaining selection is still worth sending.
            _LOGGER.exception(
                "line reactivation dispatch failed campaign=%s character=%s",
                campaign_id,
                character_id,
            )
            return _ItemVerdict(
                outcome=ProactiveOutcome.ERRORED.value,
                detail=_truncate(f"{type(exc).__name__}: {exc}"),
            )
        return _ItemVerdict(
            outcome=attempt.outcome.value,
            detail=_truncate(attempt.reason),
            # Gated on ``SENT`` rather than taken from the attempt
            # wholesale: the dispatcher also attaches the composed body to
            # an ``ERRORED`` row when delivery itself raised, and putting
            # *that* text in the report would have the operator judging
            # the reunion on a message no player ever received.
            #
            # Deliberately not passed through ``_truncate``: ``detail`` is
            # a scan column and earns its 500-char ceiling, but this is
            # the artefact under review, and a message clipped mid-thought
            # reads exactly like a message that ends badly.
            # ``==`` and not ``is``: ``ProactiveOutcome`` is a frozen
            # dataclass, not an enum, so a value round-tripped through
            # ``from_string`` is an equal but distinct object and an
            # identity test would silently blank every message.
            message_text=(
                attempt.message
                if attempt.outcome == ProactiveOutcome.SENT
                else None
            ),
        )

    async def _record(
        self,
        campaign_id: str,
        character_id: str,
        verdict: _ItemVerdict,
    ) -> None:
        try:
            await self._repository.record_outcome(
                campaign_id,
                character_id,
                outcome=verdict.outcome,
                detail=verdict.detail,
                message_text=verdict.message_text,
                attempted_at=self._resolve_now(None),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The send may well have happened; only the bookkeeping
            # failed. Leave the item pending — the claim lapses after
            # ``claim_lease`` and a resume re-attempts it, which is the
            # lesser harm against silently reporting an outcome we never
            # wrote.
            _LOGGER.exception(
                "line reactivation outcome write failed campaign=%s "
                "character=%s outcome=%s",
                campaign_id,
                character_id,
                verdict.outcome,
            )

    async def _resolve_names(
        self, character_ids: Sequence[str],
    ) -> dict[str, str]:
        """One bulk lookup, never one aggregate load per row.

        This runs on the console's polling path: a few-hundred-item
        campaign polled every few seconds. Fetching each character
        aggregate to read its ``name`` off would turn an operator
        watching a progress bar into a load generator against the
        characters table.

        Ids with no row are simply absent from the answer — a character
        deleted mid-campaign still owes the operator a row, and the
        caller renders the id rather than a blank cell that would read
        like a rendering bug.
        """

        return await self._characters.list_names(tuple(character_ids))

    async def _assert_characters_exist(
        self, character_ids: Sequence[str],
    ) -> None:
        known = await self._characters.list_names(character_ids)
        missing = tuple(
            character_id
            for character_id in character_ids
            if character_id not in known
        )
        if missing:
            raise LineReactivationUnknownCharactersError(missing)

    def _resolve_now(self, now: datetime | None) -> datetime:
        if now is not None:
            return ensure_utc(now)
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)


def _validate_campaign_id(campaign_id: str) -> str:
    """Trim, then refuse blank or over-long ids as bad requests."""

    trimmed = campaign_id.strip()
    if not trimmed:
        raise LineReactivationInvalidCampaignIdError(
            "campaign_id must be non-empty",
        )
    if len(trimmed) > MAX_CAMPAIGN_ID_CHARS:
        raise LineReactivationInvalidCampaignIdError(
            f"campaign_id must be at most {MAX_CAMPAIGN_ID_CHARS} characters, "
            f"got {len(trimmed)}",
        )
    return trimmed


def _normalise_ids(character_ids: Sequence[str]) -> tuple[str, ...]:
    """Trim, drop blanks, de-duplicate, preserve the console's order.

    De-duplication matters beyond tidiness: ``total`` is derived from this
    tuple, and a payload that listed a character twice would otherwise
    promise one more send than the item table can ever record.
    """

    cleaned = (raw.strip() for raw in character_ids)
    return tuple(dict.fromkeys(value for value in cleaned if value))


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) <= MAX_DETAIL_CHARS:
        return stripped
    return stripped[: MAX_DETAIL_CHARS - 1] + "…"


__all__ = [
    "DEFAULT_ITEM_CLAIM_LEASE",
    "MAX_CAMPAIGN_ID_CHARS",
    "MAX_DETAIL_CHARS",
    "OUTCOME_SKIPPED_NOT_DORMANT",
    "CampaignItemReport",
    "CampaignReport",
    "CampaignStartResult",
    "LineReactivationCampaignService",
    "LineReactivationEmptySelectionError",
    "LineReactivationInvalidCampaignIdError",
    "LineReactivationUnknownCharactersError",
    "ProactiveEvaluatorPort",
]
