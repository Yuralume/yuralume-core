"""The pre-composed release must land on the binding its gate authorised.

A busy-defer / scheduled-promise release decides ONE thing before it
composes — may this message carry the player's own declaration? — by asking
which external binding it would reach. The compose that follows is an LLM
call with a tool loop in it and can run for tens of seconds. Anything that
bumps a ``ChannelBinding.updated_at`` inside that window (the owner flipping
``accepts_proactive``, a ``with_conversation`` self-heal) re-orders the
"most recently touched" candidate list, so a second resolution at send time
can answer with a *different* binding than the gate saw.

These tests pin the whole path down with a real ``ProactiveDispatcher`` and
a composer that mutates the bindings while it "thinks":

* the ordering flipping mid-compose must not redirect the send;
* a binding that only became eligible mid-compose must not receive a
  web-only message the gate cleared for the owner's own session;
* the pinned binding losing its eligibility must drop the external half
  rather than fall back to whatever binding is now on top;
* a resolution that *failed* — no pin at all — must not be retried into a
  second, unpinned verdict, which was the residual hole the pin left open;
* and the scheduled-promise kind is held to the same rule as busy-defer,
  because the two release paths are two copies of one decision.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.pending_follow_up_dispatcher import (
    PendingFollowUpDispatcher,
)
from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeInput,
    PendingFollowUpComposeOutput,
    PendingFollowUpComposerPort,
)
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
    ScheduledPromiseComposeOutput,
    ScheduledPromiseComposerPort,
)
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpMessage,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.player_persona_note import PlayerPersonaNote
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_player_persona_notes import (
    InMemoryPlayerPersonaNoteRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_line_account,
    create_telegram_account,
)

NOTE = "我是大學生，白天在圖書館打工"
NOW = datetime(2026, 5, 16, 15, 0, tzinfo=timezone.utc)


class _MutatingComposer(PendingFollowUpComposerPort):
    """A composer that changes the world while it "thinks".

    ``during_compose`` stands in for everything a real compose window is
    long enough to overlap with: an owner toggling a binding in the UI, an
    inbound message self-healing a conversation id.
    """

    def __init__(self, during_compose=None, response: str = "剛忙完，回你") -> None:  # noqa: ANN001
        self._during_compose = during_compose
        self.response = response
        self.calls: list[PendingFollowUpComposeInput] = []

    async def compose(self, payload):  # noqa: ANN001, ANN201
        self.calls.append(payload)
        if self._during_compose is not None:
            await self._during_compose()
        return PendingFollowUpComposeOutput(content_text=self.response)


class _MutatingPromiseComposer(ScheduledPromiseComposerPort):
    """The same world-changing composer, on the other release kind."""

    def __init__(self, during_compose=None, response: str = "該吃藥囉") -> None:  # noqa: ANN001
        self._during_compose = during_compose
        self.response = response
        self.calls: list[ScheduledPromiseComposeInput] = []

    async def compose(self, payload):  # noqa: ANN001, ANN201
        self.calls.append(payload)
        if self._during_compose is not None:
            await self._during_compose()
        return ScheduledPromiseComposeOutput(content_text=self.response)


async def _proactive_character(harness, *, accepts_web_proactive: bool = False):  # noqa: ANN001, ANN201
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    updated = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=NOW - timedelta(minutes=60),
        ),
        proactive_enabled=True,
        accepts_web_proactive=accepts_web_proactive,
    )
    await harness.character_repository.save(updated)
    return updated


async def _dm_binding(
    harness,
    *,
    character_id: str,
    touched_at: datetime,
    chat_ref: str = "dm-chat",
) -> ChannelBinding:
    """The owner's own Telegram DM — one identified recipient."""
    account = await create_telegram_account(
        harness,
        character_id=character_id,
        allowed_sender_refs=("tg-owner",),
    )
    return await _bind(
        harness, account_id=account.id, chat_ref=chat_ref, touched_at=touched_at,
    )


async def _group_binding(
    harness,
    *,
    character_id: str,
    touched_at: datetime,
    chat_ref: str = "group-chat",
) -> ChannelBinding:
    """A shared LINE chat — several humans behind one account.

    A second *platform* rather than a second Telegram account because one
    account per platform per character is the invariant the account service
    enforces; a two-Telegram-account character is not a state that exists.
    """
    account = await create_line_account(
        harness,
        character_id=character_id,
        allowed_sender_refs=("line-owner", "line-friend"),
    )
    return await _bind(
        harness, account_id=account.id, chat_ref=chat_ref, touched_at=touched_at,
    )


async def _bind(
    harness,  # noqa: ANN001
    *,
    account_id: str,
    chat_ref: str,
    touched_at: datetime,
    accepts_proactive: bool = True,
) -> ChannelBinding:
    binding = ChannelBinding.create(
        account_id=account_id,
        chat_ref=chat_ref,
        accepts_proactive=accepts_proactive,
        now=touched_at,
    )
    await harness.binding_repository.save(binding)
    return binding


def _row(character_id: str) -> PendingFollowUp:
    return PendingFollowUp.new(
        character_id=character_id,
        conversation_id="conv-1",
        first_message=PendingFollowUpMessage.new(
            content="晚餐吃什麼", queued_at=NOW - timedelta(minutes=30),
        ),
        brief_reply="等等回你",
        defer_reason="會議中",
        scheduled_for=NOW - timedelta(minutes=5),
        activity_id="act-1",
        now=NOW - timedelta(minutes=30),
    )


def _build(  # noqa: ANN201
    harness,  # noqa: ANN001
    *,
    composer,  # noqa: ANN001
    notes,  # noqa: ANN001
    external_delivery=None,  # noqa: ANN001
    hosted_identity_resolver=None,  # noqa: ANN001
    scheduled_promise_composer=None,  # noqa: ANN001
):
    proactive = ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=None,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        player_persona_note_repository=notes,
        external_delivery=external_delivery,
        hosted_identity_resolver=hosted_identity_resolver,
    )
    repository = InMemoryPendingFollowUpRepository()
    release = PendingFollowUpDispatcher(
        repository=repository,
        composer=composer,
        proactive_dispatcher=proactive,
        character_repository=harness.character_repository,
        player_persona_note_repository=notes,
        scheduled_promise_composer=scheduled_promise_composer,
    )
    return release, repository


async def _notes_with(character) -> InMemoryPlayerPersonaNoteRepository:  # noqa: ANN001
    notes = InMemoryPlayerPersonaNoteRepository()
    await notes.upsert(
        PlayerPersonaNote.create(
            character_id=character.id,
            operator_id=getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID,
            note=NOTE,
        ),
    )
    return notes


@pytest.mark.asyncio
async def test_release_stays_on_the_gated_dm_when_a_group_overtakes_it() -> None:
    """The exact leak: a group binding bumped mid-compose wins the re-sort."""
    harness = build_messaging_harness()
    character = await _proactive_character(harness)
    dm = await _dm_binding(
        harness, character_id=character.id,
        touched_at=NOW - timedelta(minutes=1),
    )
    group = await _group_binding(
        harness, character_id=character.id,
        touched_at=NOW - timedelta(hours=5),
    )
    notes = await _notes_with(character)

    async def bump_the_group() -> None:
        await harness.binding_repository.save(
            group.with_conversation("conv-group", now=NOW),
        )

    composer = _MutatingComposer(during_compose=bump_the_group)
    release, repository = _build(harness, composer=composer, notes=notes)
    row = _row(character.id)
    await repository.add(row)

    assert await release.tick(now=NOW) == 1

    # The gate saw the single-recipient DM, so the message was written with
    # the player's declaration in it…
    assert composer.calls[0].player_persona_note == NOTE
    # …and that is the only chat allowed to hear it.
    assert [m.chat_ref for m in harness.telegram_adapter.sent] == ["dm-chat"]
    assert harness.line_adapter.sent == []
    stored = await repository.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.RESOLVED
    assert dm.id != group.id


@pytest.mark.asyncio
async def test_web_only_release_ignores_a_binding_that_appears_mid_compose() -> None:
    """No external sink at gate time is what *permits* the declaration."""
    harness = build_messaging_harness()
    character = await _proactive_character(harness, accepts_web_proactive=True)
    notes = await _notes_with(character)
    created: dict[str, ChannelBinding] = {}

    async def add_a_group() -> None:
        created["group"] = await _group_binding(
            harness, character_id=character.id, touched_at=NOW,
        )

    composer = _MutatingComposer(during_compose=add_a_group)
    release, repository = _build(harness, composer=composer, notes=notes)
    row = _row(character.id)
    await repository.add(row)

    assert await release.tick(now=NOW) == 1

    assert composer.calls[0].player_persona_note == NOTE
    assert created["group"].chat_ref == "group-chat"
    assert harness.telegram_adapter.sent == []
    assert harness.line_adapter.sent == []
    stored = await repository.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.RESOLVED


@pytest.mark.asyncio
async def test_release_drops_the_external_half_when_the_pin_loses_eligibility() -> None:
    """Withdrawn endpoint → web only, never "the next binding along"."""
    harness = build_messaging_harness()
    character = await _proactive_character(harness, accepts_web_proactive=True)
    dm = await _dm_binding(
        harness, character_id=character.id,
        touched_at=NOW - timedelta(minutes=1),
    )
    await _group_binding(
        harness, character_id=character.id,
        touched_at=NOW - timedelta(hours=5),
    )
    notes = await _notes_with(character)

    async def withdraw_the_dm() -> None:
        await harness.binding_repository.save(
            dm.with_accepts_proactive(False, now=NOW),
        )

    composer = _MutatingComposer(during_compose=withdraw_the_dm)
    release, repository = _build(harness, composer=composer, notes=notes)
    row = _row(character.id)
    await repository.add(row)

    assert await release.tick(now=NOW) == 1

    # Neither the withdrawn DM nor the group that is now top of the list.
    assert harness.telegram_adapter.sent == []
    assert harness.line_adapter.sent == []
    stored = await repository.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.RESOLVED


@pytest.mark.asyncio
async def test_group_only_release_blanks_the_note_and_still_reaches_the_group() -> None:
    """The pin narrows *where*, never *whether*: an authorised group still
    gets its message — just without the owner's declaration in it."""
    harness = build_messaging_harness()
    character = await _proactive_character(harness)
    await _group_binding(
        harness, character_id=character.id,
        touched_at=NOW - timedelta(hours=5),
    )
    notes = await _notes_with(character)

    composer = _MutatingComposer()
    release, repository = _build(harness, composer=composer, notes=notes)
    row = _row(character.id)
    await repository.add(row)

    assert await release.tick(now=NOW) == 1

    assert composer.calls[0].player_persona_note == ""
    assert [m.chat_ref for m in harness.line_adapter.sent] == ["group-chat"]


class _FakeHostedPort:
    """Cloud-Channel delivery double: records whether it was consulted."""

    def __init__(self) -> None:
        self.eligibility_calls = 0
        self.accepted: list[object] = []

    async def check_eligibility(self, character_id: str):  # noqa: ANN201
        from kokoro_link.contracts.external_proactive import DeliveryEligibility

        self.eligibility_calls += 1
        return DeliveryEligibility(eligible=True)

    async def accept(self, envelope):  # noqa: ANN001, ANN201
        from kokoro_link.contracts.external_proactive import (
            DeliveryAcceptance,
            DeliveryAcceptanceStatus,
        )

        self.accepted.append(envelope)
        return DeliveryAcceptance(
            status=DeliveryAcceptanceStatus.ACCEPTED, delivery_id="chan-1",
        )


@pytest.mark.asyncio
async def test_a_withdrawn_pin_does_not_fall_through_to_the_hosted_channel() -> None:
    """Losing the authorised endpoint means no external route at all.

    Substituting the Cloud Channel would be the same defect one door along:
    a sink the gate never looked at. The row goes back to ``queued`` so the
    next tick re-gates from scratch — the promise is delayed, not misrouted.
    """
    harness = build_messaging_harness()
    character = await _proactive_character(harness)  # web off
    dm = await _dm_binding(
        harness, character_id=character.id,
        touched_at=NOW - timedelta(minutes=1),
    )
    notes = await _notes_with(character)
    port = _FakeHostedPort()

    async def withdraw_the_dm() -> None:
        await harness.binding_repository.save(
            dm.with_accepts_proactive(False, now=NOW),
        )

    async def hosted_identity(_character_id: str):  # noqa: ANN202
        return ("tenant-7", "account-9")

    composer = _MutatingComposer(during_compose=withdraw_the_dm)
    release, repository = _build(
        harness, composer=composer, notes=notes,
        external_delivery=port, hosted_identity_resolver=hosted_identity,
    )
    row = _row(character.id)
    await repository.add(row)

    assert await release.tick(now=NOW) == 0

    assert port.eligibility_calls == 0
    assert port.accepted == []
    stored = await repository.get(row.id)
    assert stored is not None
    assert stored.status == PendingFollowUpStatus.QUEUED


def test_a_confirmed_pin_returns_the_snapshot_not_the_fresh_row() -> None:
    """Liveness is checked on the fresh listing; the *destination* still
    comes from the snapshot, so a binding re-pointed at another chat
    mid-window cannot carry the message to the new one."""
    from dataclasses import replace

    from kokoro_link.application.services.proactive_delivery import (
        ResolvedProactiveSink,
    )
    from kokoro_link.domain.entities.messaging_account import MessagingAccount

    account = MessagingAccount.create(
        character_id="c1",
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "t"},
        allowed_sender_refs=("tg-owner",),
    )
    pinned = ChannelBinding.create(
        account_id=account.id, chat_ref="dm-chat", accepts_proactive=True,
        now=NOW - timedelta(minutes=1),
    )
    repointed = replace(pinned, chat_ref="somewhere-else", updated_at=NOW)
    sink = ResolvedProactiveSink.of((pinned, account))

    confirmed = sink.confirm_against(((repointed, account),))
    assert confirmed is not None
    assert confirmed[0].chat_ref == "dm-chat"

    # Gone from the eligible set = withdrawn endpoint = nothing external.
    assert sink.confirm_against(()) is None
    assert ResolvedProactiveSink.of(None).confirm_against(
        ((pinned, account),),
    ) is None


class _FlakyResolver:
    """``resolve_proactive_sink`` that misses once, then works.

    The exact shape of a transient lookup failure: the real method
    swallows its own exception and answers ``None``, so "nothing external
    to pin" and "the query just broke" are the *same* answer to the
    caller. A gate that reacts to that ``None`` by asking again is not
    being resilient — it is holding a second, unpinned opinion, and the
    second try is the one that succeeds.
    """

    def __init__(self, inner) -> None:  # noqa: ANN001
        self._inner = inner
        self.calls = 0

    async def __call__(self, character_id: str):  # noqa: ANN204
        self.calls += 1
        if self.calls == 1:
            return None
        return await self._inner(character_id)


@pytest.mark.asyncio
async def test_a_failed_resolution_is_never_retried_into_a_disclosure() -> None:
    """The residual hole the pin left open, in the probe's own shape.

    Owner DM newest, group oldest, group bumped mid-compose — and the
    first resolution comes back empty. The old fallback then re-asked the
    dispatcher, got the DM (the group had not been bumped yet), decided
    "yes", and wrote the declaration into the body. Nothing was pinned, so
    the send resolved for itself afterwards and handed that body to the
    group that had meanwhile climbed to the top. One unlucky query turned
    a private declaration into a group broadcast.

    Whatever else happens to this release, the declaration is not in it.
    """
    harness = build_messaging_harness()
    character = await _proactive_character(harness)
    await _dm_binding(
        harness, character_id=character.id,
        touched_at=NOW - timedelta(minutes=1),
    )
    group = await _group_binding(
        harness, character_id=character.id,
        touched_at=NOW - timedelta(hours=5),
    )
    notes = await _notes_with(character)

    async def bump_the_group() -> None:
        await harness.binding_repository.save(
            group.with_conversation("conv-group", now=NOW),
        )

    composer = _MutatingComposer(during_compose=bump_the_group)
    release, repository = _build(harness, composer=composer, notes=notes)
    proactive = release._proactive_dispatcher
    flaky = _FlakyResolver(proactive.resolve_proactive_sink)
    proactive.resolve_proactive_sink = flaky
    row = _row(character.id)
    await repository.add(row)

    await release.tick(now=NOW)

    # Asked once, answered "no". The old fallback's second question is
    # what this count forbids — the leak needed both halves.
    assert flaky.calls == 1
    assert composer.calls[0].player_persona_note == ""
    delivered = [*harness.telegram_adapter.sent, *harness.line_adapter.sent]
    assert all(NOTE not in message.text for message in delivered)


@pytest.mark.asyncio
async def test_scheduled_promise_release_pins_its_target_too() -> None:
    """The other release kind, same rule.

    ``_release_scheduled_promise`` is a second copy of the busy-defer
    release's decision, and a fix applied to one copy is a fix that half
    the feature never received. Same mid-compose bump, same expectation:
    the declaration was cleared for the owner's own DM, so the owner's own
    DM is the only chat that hears it.
    """
    harness = build_messaging_harness()
    character = await _proactive_character(harness)
    await _dm_binding(
        harness, character_id=character.id,
        touched_at=NOW - timedelta(minutes=1),
    )
    group = await _group_binding(
        harness, character_id=character.id,
        touched_at=NOW - timedelta(hours=5),
    )
    notes = await _notes_with(character)

    async def bump_the_group() -> None:
        await harness.binding_repository.save(
            group.with_conversation("conv-group", now=NOW),
        )

    composer = _MutatingPromiseComposer(during_compose=bump_the_group)
    release, repository = _build(
        harness, composer=_MutatingComposer(), notes=notes,
        scheduled_promise_composer=composer,
    )
    row = PendingFollowUp.new_promise(
        character_id=character.id,
        conversation_id="conv-1",
        promise_intent="提醒對方吃藥",
        scheduled_for=NOW - timedelta(minutes=5),
        source_message_content="晚點提醒我吃藥",
        now=NOW - timedelta(hours=2),
    )
    await repository.add(row)

    assert await release.tick(now=NOW) == 1

    assert composer.calls[0].player_persona_note == NOTE
    assert [m.chat_ref for m in harness.telegram_adapter.sent] == ["dm-chat"]
    assert harness.line_adapter.sent == []


@pytest.mark.asyncio
async def test_scheduled_promise_release_fails_closed_without_a_pin() -> None:
    """And the unpinned half of the rule, on that same second copy."""
    harness = build_messaging_harness()
    character = await _proactive_character(harness)
    await _dm_binding(
        harness, character_id=character.id,
        touched_at=NOW - timedelta(minutes=1),
    )
    notes = await _notes_with(character)

    composer = _MutatingPromiseComposer()
    release, repository = _build(
        harness, composer=_MutatingComposer(), notes=notes,
        scheduled_promise_composer=composer,
    )
    proactive = release._proactive_dispatcher
    proactive.resolve_proactive_sink = _FlakyResolver(
        proactive.resolve_proactive_sink,
    )
    row = PendingFollowUp.new_promise(
        character_id=character.id,
        conversation_id="conv-1",
        promise_intent="提醒對方吃藥",
        scheduled_for=NOW - timedelta(minutes=5),
        now=NOW - timedelta(hours=2),
    )
    await repository.add(row)

    await release.tick(now=NOW)

    assert composer.calls[0].player_persona_note == ""


@pytest.mark.asyncio
async def test_delivery_without_a_pin_resolves_for_itself() -> None:
    """Regression guard for every caller that has no gate of its own: an
    unpinned ``deliver_pre_composed`` still finds the eligible binding."""
    harness = build_messaging_harness()
    character = await _proactive_character(harness)
    await _dm_binding(
        harness, character_id=character.id,
        touched_at=NOW - timedelta(minutes=1),
    )
    notes = InMemoryPlayerPersonaNoteRepository()
    release, _ = _build(harness, composer=_MutatingComposer(), notes=notes)
    proactive = release._proactive_dispatcher

    from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
    from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger

    attempt = await proactive.deliver_pre_composed(
        character_id=character.id,
        text="沒有 target 的舊呼叫",
        trigger=ProactiveTrigger.PENDING_FOLLOW_UP,
        now=NOW,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    assert [m.chat_ref for m in harness.telegram_adapter.sent] == ["dm-chat"]
