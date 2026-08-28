"""Two-pass compose → tool → compose loop for promise fulfilment.

Why two passes, when the proactive dispatcher does it in one
--------------------------------------------------------------------
``ProactiveDispatcher`` writes the message first and runs the decider's
tool calls afterwards, attaching whatever came back. That order works
when the tool output is *decoration* (a selfie next to "在咖啡廳耶").

It is the wrong order for a kept promise. When the character said "晚點
幫你查那個規則", the message **is** the tool result — writing the prose
first means asking the model to invent the answer and then hoping the
search agrees with it. So here the model gets the tool list first and
may answer a compose call with ``tool_calls`` instead of prose; we run
them, then compose again with the results in hand.

Shape (deliberately small, and shared — PF2 attaches the busy-defer
follow-up composer to this same loop rather than copying it):

    pass 1: compose(payload + available_tools)
            → tool_calls?  no  → that text is the message, done
                             yes → execute (max 1 call, orchestrator)
    pass 2: compose(payload + tool_results) → final message text

Invariants
----------
- **The adapter never runs a tool.** Composer adapters live in
  ``infrastructure/`` and only talk to models; execution, permission
  checks and audit rows stay behind ``ToolOrchestrator`` in the
  application layer — same split ``ProactiveDispatcher`` uses.
- **Failures are facts, not silence.** A denied, crashed or failed tool
  is fed into pass 2 as a failure outcome so the character can say "相機
  壞了等等再傳" instead of quietly not delivering what it promised.
  Dropping the failure would turn a tool outage into a broken promise.
- **Fail-soft.** Any composer error is the composer's own problem (the
  port contract says it returns empty text rather than raising); the
  loop adds no new raising paths, and an empty final text propagates
  unchanged so the caller can leave the row queued for the next tick.
- **A round that spent something never asks to be repeated.** "Retry
  next tick" is only honest while the round was cheap, and what makes it
  expensive is the *tool having produced an artifact* — not the delivery
  list being non-empty. Those two disagree exactly when a render
  succeeded and no public base URL exists to serve it from, and reading
  the wrong one there re-renders the same picture on every reconcile
  forever (see :meth:`ComposerToolLoop._no_final_text`).
- **Byte-compatible when unwired.** No orchestrator, no registry, or a
  character with no permitted tools → exactly one ``compose(payload)``
  with the payload untouched, identical to the pre-PF1 call.
- **Scarce capacity is scheduled, not hidden (PF3).** Some tools drive a
  GPU, and a background caller may be running outside the ceiling that
  bounds it. Such a caller passes ``schedule_capability``: between pass 1
  and the invocation the loop asks it to take the call over. Note what is
  NOT done — the tool is never dropped from ``available_tools`` merely
  because it is expensive. Hiding it would silently turn "晚點傳照片給你"
  into a text-only apology forever on exactly the deployments that
  promised pictures; deferring only moves the same invocation to where it
  can be counted.
- **A message may not claim what no tool did (HV1).** Both exits below —
  the zero-call one and the pass-2 one — run the composed text past
  :class:`~kokoro_link.application.services.outcome_claim_guard.OutcomeClaimGuard`
  before it can ship. The zero-call exit is the one that mattered in
  production: pass 1 answers in fluent prose, claims a photo was sent,
  and nothing in the mechanical guards can see it, because the sentence
  is perfectly well-formed. A blocked round is re-composed **once**, with
  a correction instruction naming what it overclaimed; a second offence
  withholds the *prose* (S7: not necessarily the whole round — the
  pass-2 exit's ``_park`` still ships attachments a tool already produced
  and made deliverable, with a fixed fallback line in place of the
  prose the model would not stop overclaiming in). No judge wired →
  every line of the old behaviour, byte for byte.
- **A message may not be unreadable either (QG4).** Honesty is not the
  only way a background message fails a player: a leaked schema tag, a
  draft cut mid-sentence by the composer's own character cap, an English
  reply to a 繁體中文 player. Those are a different question over
  different evidence, so they are a different gate — the shared
  ``output_quality`` band, run on the **same final prose** the honesty
  gate is about to see and run **first**, so the honesty judge always
  reviews the draft that would actually ship. Its hard failures end the
  round through the paths this loop already has (empty text = retry next
  tick; :meth:`ComposerToolLoop._no_final_text` when a tool already spent
  something), never through a new park type — a stylistic defect must not
  be able to charge the honesty gate's promise-cancelling attempt budget.
  The empty body is *labelled* rather than reclassified
  (:attr:`ComposedMessage.quality_skipped`, RC): the caller that owns the
  retry has to be able to tell "the composer hiccuped, try again next
  tick" from "our judge rejected two drafts and will reject the third",
  because the second one re-runs an identical prompt on every tick
  forever if it is read as the first.
  The fixed localized fallback lines below are never reviewed: they are
  constants, and the honesty gate's own re-compose is not re-reviewed
  either (accepted residual R-QG-2).
- **A capacity the operator switched off is not offered at all (S1).**
  The one exception to the line above, and it is the operator's own
  sentence: ``BG_CAP_<CAP>=0`` says "this deployment does not run that
  in the background". For a caller that *must* hand the invocation off
  (``schedule_capability`` present), the queue it would hand to is
  closed — the job would be minted and never claimable, so the promise
  would go unanswered forever while every reconcile burned another pass
  1. Withholding the tool from that one caller lets pass 1 write an
  honest "我今天沒辦法拍" instead. Note the shape: ``cap >= 1`` changes
  nothing, and a caller that runs its tools inline (embedded self-host,
  chat) is never filtered — its tools do not depend on that queue.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timezone
from typing import Any, Protocol, TypeVar

from kokoro_link.application.services.outcome_claim_audit import (
    PARK_CORRECTION_CLAIMED_AGAIN,
    PARK_CORRECTION_OVERCLAIMED_AGAIN,
    PARK_CORRECTION_WROTE_NOTHING,
    PARK_NO_CORRECTION_CHANNEL,
    PARK_NO_VERDICT_AFTER_TOOLS,
    PARK_NO_VERDICT_ZERO_CALL,
    OutcomeClaimParkReason,
)
from kokoro_link.application.services.outcome_claim_guard import (
    OutcomeClaimGuard,
)
from kokoro_link.application.services.output_quality import (
    OUTCOME_HARD_SKIPPED,
    OutputQualityOrchestrator,
    OutputQualityPolicy,
    fired_axes,
    script_mix_lines,
)
from kokoro_link.application.services.tool_attachment_delivery import (
    to_outbound_attachments,
)
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)
from kokoro_link.infrastructure.prompt.outcome_claim_honesty import (
    CORRECTION_MISMATCH,
    CORRECTION_ZERO_CALL,
    render_honesty_correction,
)
from kokoro_link.infrastructure.prompt.temporal_evidence import (
    TemporalEvent,
    quoted_event,
    render_temporal_context_lines,
)
from kokoro_link.contracts.messaging import OutboundAttachment
from kokoro_link.contracts.novelty_gate import NoveltyGateContext
from kokoro_link.contracts.pending_follow_up_composer import (
    PendingFollowUpComposeInput,
)
from kokoro_link.contracts.scheduled_promise_composer import (
    ScheduledPromiseComposeInput,
)
from kokoro_link.contracts.outcome_claim import (
    OutcomeClaimEvidence,
    OutcomeClaimVerdict,
)
from kokoro_link.contracts.prompt import PromptToolDescriptor, ToolOutcomeMessage
from kokoro_link.contracts.tool import (
    TOOL_CAPABILITY_NONE,
    ToolRegistryPort,
    tool_capability,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    normalize_content_tolerance,
)
from kokoro_link.domain.value_objects.tool_call import ToolCall

_LOGGER = logging.getLogger(__name__)

SURFACE_PROMISE = "promise"
"""Label a scheduled-promise fulfilment reports under."""

SURFACE_FOLLOW_UP = "follow_up"
"""Label a busy-defer follow-up reports under.

One label per *hook point*, and the reason it is derived from the payload
rather than fixed on the instance is that there is only one instance: the
container builds a single :class:`ComposerToolLoop` and the dispatcher
drives both kinds of pending follow-up through it (that sharing is the
point of the class — see the module docstring). A constructor-fixed
``surface`` therefore recorded every busy-defer disposal, every dropped
attachment and every warning line as a *promise*, which is not a smaller
version of the truth: the busy-defer seam had no series of its own at all,
and the promise seam's hard-skip rate was two seams added together.

The promise label is unchanged, so no existing dashboard or alert loses
its series."""

_PAYLOAD_SURFACES: Mapping[type, str] = {
    ScheduledPromiseComposeInput: SURFACE_PROMISE,
    PendingFollowUpComposeInput: SURFACE_FOLLOW_UP,
}
"""The two shipped compose inputs, by exact type.

Exact type rather than ``isinstance`` because this is a label lookup, not
a behaviour branch: a subclass nobody here has met is exactly the case
that should fall back to the loop's configured surface rather than
silently claim to be one of these two. Everything else this module reads
off a payload stays ``getattr`` — the loop is generic over both inputs and
the fakes tests hand it, and that is unchanged."""

MAX_TOOL_CALLS_PER_COMPOSE = 1
"""One tool per fulfilment, mirroring the chat / proactive contract.
A promise that genuinely needs two tools degrades to doing the first
one and saying so — better than an unbounded background tool budget."""

MAX_COMPOSE_PASSES = 2
"""compose → tools → compose. There is no third pass: the second one
is told to write the message with what it got, not to ask again."""

MAX_HONESTY_RETRIES = 1
"""How many times a blocked round may be re-composed (HV1).

Read by both honesty-gate exits below (:meth:`ComposerToolLoop.
_resolve_zero_call`, :meth:`ComposerToolLoop._deliver_or_correct`) as the
bound on their correction loop — this is the one place "how many tries"
is decided, not a fact re-derived from each exit's own control flow.

One, and the bound is about what a second re-run would mean rather than
about cost. A model that overclaimed, was shown exactly which sentence
overclaimed, and did it again is not going to be talked round by a third
attempt — it has a different idea of what happened than the tool log
does. Sending nothing is then the honest outcome, and for the background
seam that costs one tick. (B-5: this is *why* the value is 1, not a
license to leave it unread — a constant no code branches on is a knob
that does nothing when turned.)"""

MAX_QUALITY_RETRIES = 1
"""How many times a quality-blocked draft may be re-composed (QG4).

The D1 background row's ``max_retries``, spelled here rather than left to
the orchestrator's default so this seam's retry budget is readable next to
:data:`MAX_HONESTY_RETRIES` — the two are independent and a reader has to
be able to see that a round can cost at most one extra compose per gate,
not one shared between them."""

_QUALITY_CORRECTION_HEAD = (
    "⚠️ 上一版草稿沒有通過玩家可見輸出品質檢查，這是重寫的機會。"
)
_QUALITY_CORRECTION_TAIL = (
    "請直接重寫這則訊息本身：修掉上面指出的問題，"
    "不要提到這次檢查、不要為此道歉、不要附加任何說明或標記。"
)


def render_quality_correction(feedback: str) -> str:
    """The instruction a quality-blocked round is re-composed with.

    Deliberately thin — the judge's own ``feedback`` is the content, and
    this only frames it so the model knows the sentence is a review of its
    last draft rather than a new instruction from the player. Written here
    rather than in a prompt-pack template for the same reason the honesty
    correction is (see
    :mod:`~kokoro_link.infrastructure.prompt.outcome_claim_honesty`): a
    correction that only exists on a retry has no business in the text
    every ordinary compose renders, and a gate's wording must match the
    build that contains the gate rather than whatever pack is deployed.
    """
    cleaned = (feedback or "").strip()
    return "\n".join(
        line for line in (
            _QUALITY_CORRECTION_HEAD,
            f"檢查意見：{cleaned}" if cleaned else "",
            _QUALITY_CORRECTION_TAIL,
        ) if line
    )


HONESTY_CORRECTION_FIELD = "honesty_correction"
"""Payload field the correction instruction is injected through.

Both promise-composer inputs carry it (defaulting to ``""``, so every
ordinary compose renders byte-identically). It is a *field* rather than a
prompt-pack template edit on purpose: the correction only exists on a
retry, and the shipped template describes the normal round. A payload
without the field simply cannot be corrected — the loop parks instead of
re-running blind.

QG4 sends the **quality** band's correction through this same field
rather than adding a second one. Both adapters render it identically —
the string is appended, verbatim, at the very tail of the prompt (see
``append_honesty_correction``) — so the field is really "the instruction
this retry exists for", and the two gates never want one at the same
time: quality runs first and hands the honesty gate a *fresh* draft,
whose own correction then replaces this one. The name is HV1's and is
kept because renaming a field on two shipped compose inputs is a change
to contracts this seam does not own."""

DELIVERED_WITHOUT_TEXT_FALLBACK_KEY = "chat.image_tool_final_reply_failed"
"""Said when pass 2 produced no usable text but the tool already produced
an attachment **that ships with this message**.

Deliberately the SAME key the chat loop uses for the same situation
(``chat_service`` — image rendered, final hop still emitted JSON) rather
than a promise-flavoured second one: the two surfaces would drift, and
the sentence ("圖片已經傳好了，只是剛剛想接著說的話卡住了。") is already
exactly what happened here — the picture ships with this very message,
only the prose around it is missing."""

UNDELIVERABLE_ARTIFACT_FALLBACK_KEY = "promise.attachment_undeliverable"
"""Said when the tool produced its artifact but nothing can carry it.

Same cost as the line above — the render happened — and the opposite
truth: :func:`to_outbound_attachments` drops a server-relative URL when
the deployment has no public base URL, so there is no picture attached to
claim credit for. Saying "圖片已經傳好了" here would be a lie the player
can check, hence a second key rather than a reuse."""


class _ToolOrchestratorLike(Protocol):
    async def execute(
        self,
        *,
        character: Character,
        call: ToolCall,
        conversation_id: str | None = None,
        recent_dialogue: str = "",
        user_attachment_urls: tuple[str, ...] = (),
    ) -> tuple[Any, Any]: ...


PayloadT = TypeVar("PayloadT")


@dataclass(frozen=True, slots=True)
class ComposedMessage:
    """What the loop hands back to the dispatcher.

    ``content_text`` keeps the composers' fail-soft contract: empty
    means "no usable output, retry next tick". ``attachments`` are
    already absolutised for external platforms and are only ever
    non-empty alongside a non-empty text — an image with no message
    would arrive as a bare file from nobody."""

    content_text: str
    attachments: tuple[OutboundAttachment, ...] = ()
    deferred_capability: str = TOOL_CAPABILITY_NONE
    """Set when the composer asked for a tool this caller may not run
    inline and the scheduler took ownership of it instead (PF3). The
    text is empty — but unlike a fail-soft empty, nothing is lost: the
    fulfilment is queued to run where that capability is capped. The
    caller leaves the row releasable and does NOT count it as a failure
    against the promise."""
    quality_skipped: bool = False
    """This round's prose was withheld by the QG4 quality band (RC).

    Only ever set alongside a ``hard_skipped`` disposal — never for a
    fail-open, a soft failure, or a composer that simply wrote nothing —
    so a caller reading it knows the *same* thing will happen on the next
    tick unless the composer's output changes. That is the difference
    from an ordinary empty body: a composer hiccup is worth retrying
    immediately, a draft our own judge keeps rejecting is not, and the
    caller owning the retry is the only place that can act on it.

    Carried on a delivered message too (the pass-2 exit can withhold the
    prose and still ship an artifact with a fixed fallback line); it means
    nothing there, and every reader is expected to consult it only when
    ``content_text`` is empty."""


@dataclass(frozen=True, slots=True)
class _ToolRun:
    """What one round of tool execution actually cost and produced.

    ``produced_artifacts`` counts the files the tools handed back, BEFORE
    :func:`to_outbound_attachments` decides which of them this deployment
    can ship. The two numbers diverge exactly when a render succeeded and
    the messaging public base URL is unset — and that gap is the one place
    where "nothing to show" and "nothing was spent" mean opposite things,
    so the loop keeps both instead of re-deriving cost from the delivery
    list."""

    outcomes: tuple[ToolOutcomeMessage, ...] = ()
    attachments: tuple[OutboundAttachment, ...] = ()
    produced_artifacts: int = 0


@dataclass(frozen=True, slots=True)
class _QualityOutcome:
    """What the quality band decided about one draft.

    Two fields rather than the bare ``str | None`` this used to be,
    because the two ways of getting ``None`` are not the same fact for the
    caller that owns the retry: a draft the judge *hard-failed twice*
    will fail again next tick on the same prompt, while an empty or
    unreviewable draft is the composers' ordinary fail-soft. The loop is
    the only place that can tell them apart, so it says which happened
    instead of flattening both into an empty message."""

    text: str | None
    hard_skipped: bool = False


@dataclass(frozen=True, slots=True)
class _ZeroCallResolution:
    """How the zero-call exit ended after the honesty gate looked at it.

    Exactly one of the two fields is populated. ``message`` means the
    round is over — shipped, or parked. ``calls`` is the interesting
    outcome: the correction re-run took the *other* honest road and asked
    for the tool it should have asked for the first time, so the round
    rejoins the ordinary tool path instead of ending here.
    """

    message: ComposedMessage | None = None
    calls: tuple[ToolCall, ...] = ()


class ComposerToolLoop:
    """Runs the two-pass loop for any composer whose input carries
    ``available_tools`` / ``tool_results`` and whose output carries
    ``tool_calls`` (both promise-fulfilment composer ports do).

    Constructed once in the container and shared by every kind of
    pending follow-up; ``tool_registry`` / ``tool_orchestrator`` may be
    ``None`` (fake provider, self-host without tools) in which case the
    loop degrades to a single plain compose.
    """

    def __init__(
        self,
        *,
        tool_registry: ToolRegistryPort | None = None,
        tool_orchestrator: _ToolOrchestratorLike | None = None,
        public_base_url: str = "",
        public_base_url_provider: Callable[[], Awaitable[str]] | None = None,
        # FC1 — the *fallback* label, used for any payload that is not one
        # of the two shipped compose inputs. Those two name themselves; see
        # ``_PAYLOAD_SURFACES``.
        surface: str = SURFACE_PROMISE,
        capability_caps: Mapping[str, int] | None = None,
        outcome_claim_guard: OutcomeClaimGuard | None = None,
        # QG0 — the quality band, which is a different question from the
        # honesty guard beside it ("is this well written" vs "is this
        # true") over different evidence, and stays a separate object for
        # exactly that reason. Order when QG5 adopts it is unchanged from
        # the plan: quality first, honesty second. Stored only, for now.
        output_quality_orchestrator: OutputQualityOrchestrator | None = None,
        # RC — the deployment's own switches for that band, named exactly
        # as every other adopting surface names them. They default to the
        # values this loop used to hard-code, so an unwired caller (tests,
        # self-host) behaves byte-identically.
        reply_quality_gate_enabled: bool = True,
        reply_quality_gate_max_retries: int = MAX_QUALITY_RETRIES,
    ) -> None:
        self._registry = tool_registry
        self._orchestrator = tool_orchestrator
        # HV1. ``None`` = no gate: every exit below behaves exactly as it
        # did before, which is what keeps a deployment with no judge route
        # (and every existing test) untouched.
        self._guard = outcome_claim_guard
        self._output_quality_orchestrator = output_quality_orchestrator
        # RC. ``KOKORO_NOVELTY_GATE_ENABLED=false`` does not produce a
        # ``None`` orchestrator — it produces one wrapping a null gate that
        # passes everything — so without this the batch's rollback switch
        # left a review (and a ``pass`` on the scrape) per message.
        self._quality_gate_enabled = bool(reply_quality_gate_enabled)
        self._quality_gate_max_retries = max(0, int(reply_quality_gate_max_retries))
        self._public_base_url = (public_base_url or "").strip().rstrip("/")
        self._public_base_url_provider = public_base_url_provider
        # FC1 — only the fallback. Every round resolves its own label from
        # the payload it was handed (``_surface_of``), because one instance
        # serves both seams.
        self._default_surface = surface
        # The deployment's §5 per-capability ceilings, as the container read
        # them from the env. Only the *closed* ones (cap 0 = "we do not run
        # this in the background here") are kept: they are the only value a
        # tool list has to react to, and keeping just the closed set makes it
        # impossible to accidentally grow a second cap-enforcement point here.
        # Unset → empty → every tool is offered, exactly as before.
        self._closed_capabilities = frozenset(
            name
            for name, cap in (capability_caps or {}).items()
            if name and cap <= 0
        )

    async def run(
        self,
        *,
        character: Character,
        payload: PayloadT,
        compose: Callable[[PayloadT], Awaitable[Any]],
        conversation_id: str | None = None,
        recent_dialogue: str = "",
        schedule_capability: Callable[[str], Awaitable[bool]] | None = None,
    ) -> ComposedMessage:
        """Compose the message, running at most one tool on the way.

        ``schedule_capability`` is the caller's escape hatch for tools it
        must not run *here*: it is asked, with the capability the chosen
        tool declared, whether it will take ownership of the invocation
        (typically by queueing the fulfilment where that capability is
        capped). ``True`` → this run stops and reports the deferral;
        ``False`` → the tool runs inline exactly as it always has, so a
        caller with nowhere to defer to still keeps the promise. Absent,
        every tool runs inline (embedded self-host, chat surfaces).

        Its presence also decides whether the deployment's closed
        capabilities are withheld from pass 1: only a caller that depends
        on the hand-off is affected by that queue being shut."""
        # FC1 — resolved once, here, and carried down. One instance serves
        # both seams, so the label is a fact about *this round*, never about
        # the loop; deriving it again further down would be one more place
        # for the two to disagree.
        surface = _surface_of(payload, self._default_surface)
        tools = self._describe_tools(
            character,
            surface=surface,
            # A deferring caller depends on the capability's queue existing;
            # a caller that runs its tools inline does not, so the operator's
            # background ceiling is none of its business (that is what keeps
            # embedded self-host untouched no matter what the env says).
            withheld_capabilities=(
                self._closed_capabilities
                if schedule_capability is not None
                else frozenset()
            ),
        )
        if not tools or self._orchestrator is None:
            # Pre-PF1 path, byte-for-byte: the payload is not rebuilt,
            # so a composer that inspects identity sees what it always
            # saw and the prompt renders without any tool section.
            #
            # QG4 gates it all the same. This is the ONLY exit a
            # deployment without a tool registry ever takes (self-host,
            # fake provider), so leaving it out would mean the surface the
            # plan names is gated on hosted and naked everywhere else. The
            # unwired-tools invariant above survives: with no quality
            # orchestrator, or a verdict that passes, there is still
            # exactly one ``compose(payload)`` with the payload untouched.
            first = await compose(payload)
            reviewed = await self._quality_reviewed(
                character=character,
                payload=payload,
                surface=surface,
                text=_content_text(first),
                run=_ToolRun(),
                recompose=compose,
            )
            return ComposedMessage(
                content_text=reviewed.text or "",
                quality_skipped=reviewed.hard_skipped,
            )

        first = await compose(replace(payload, available_tools=tools))
        calls = _tool_calls(first)
        if not calls:
            # The exit that shipped "畫好了，附上照片" with nothing attached.
            resolution = await self._resolve_zero_call(
                character=character,
                payload=payload,
                surface=surface,
                compose=compose,
                tools=tools,
                text=_content_text(first),
            )
            if resolution.message is not None:
                return resolution.message
            # The correction re-run asked for a tool after all — carry on
            # down the ordinary path with the call it finally made.
            calls = resolution.calls
        if len(calls) > MAX_TOOL_CALLS_PER_COMPOSE:
            _LOGGER.info(
                "%s tool loop: composer asked for %d calls — running the "
                "first only (cap=%d)",
                surface, len(calls), MAX_TOOL_CALLS_PER_COMPOSE,
            )
            calls = calls[:MAX_TOOL_CALLS_PER_COMPOSE]

        deferred = await self._maybe_defer(
            calls, schedule_capability, surface=surface,
        )
        if deferred:
            return ComposedMessage(content_text="", deferred_capability=deferred)

        run = await self._execute(
            character=character,
            calls=calls,
            surface=surface,
            conversation_id=conversation_id,
            recent_dialogue=recent_dialogue,
        )
        second = await compose(
            replace(payload, available_tools=(), tool_results=run.outcomes),
        )
        body = _content_text(second)
        if not body:
            return self._no_final_text(
                payload=payload, run=run, surface=surface,
            )
        # QG4 — quality first, over the prose the honesty gate is about to
        # see. A hard failure that survives its regeneration lands in
        # ``_no_final_text`` rather than in a bare empty message for the
        # reason that method exists: by here a tool has usually spent
        # something, and throwing a rendered picture away to retry the
        # round re-renders it on every reconcile forever.
        reviewed = await self._quality_reviewed(
            character=character,
            payload=payload,
            surface=surface,
            text=body,
            run=run,
            recompose=lambda corrected: compose(
                replace(
                    corrected, available_tools=(), tool_results=run.outcomes,
                ),
            ),
        )
        if reviewed.text is None:
            return self._no_final_text(
                payload=payload, run=run, surface=surface,
                quality_skipped=reviewed.hard_skipped,
            )
        return await self._deliver_or_correct(
            character=character,
            payload=payload,
            surface=surface,
            compose=compose,
            tools=tools,
            body=reviewed.text,
            run=run,
        )

    # -- the quality gate (QG4) -------------------------------------------

    async def _quality_reviewed(
        self,
        *,
        character: Character,
        payload: PayloadT,
        surface: str,
        text: str,
        run: _ToolRun,
        recompose: Callable[[PayloadT], Awaitable[Any]],
    ) -> _QualityOutcome:
        """The prose that may go on to the honesty gate, or ``None``.

        A ``None`` :attr:`_QualityOutcome.text` means "this round sends no
        prose" and is the *only* refusal this method has: every caller maps
        it onto the withholding path it already had (an empty body at the
        two cheap exits, :meth:`_no_final_text` at the pass-2 one), so a
        quality skip can never charge the honesty gate's attempt budget —
        that budget is what cancels a promise outright, and a stylistic
        defect must not be able to reach it.

        ``hard_skipped`` rides alongside for the caller that owns the
        *retry* (RC): it is the difference between "the composer hiccuped,
        try again next tick" and "our own judge rejected two drafts of
        this, and will reject the next one too".

        Two short-circuits keep the unwired and the empty cases free: no
        orchestrator (self-host, every pre-QG4 test) returns the text
        untouched, and an empty draft is the composers' own retry-next-tick
        with nothing in it to have an opinion about.
        """
        orchestrator = self._output_quality_orchestrator
        if orchestrator is None or not text:
            return _QualityOutcome(text=text)

        async def regenerate(feedback: str) -> str | None:
            corrected = _with_correction(
                payload, render_quality_correction(feedback),
            )
            if corrected is None:
                # A payload that predates HV1's correction field cannot be
                # told what to fix, so a re-compose would be the identical
                # prompt — see ``_with_correction``.
                return None
            retry = await recompose(corrected)
            if _tool_calls(retry):
                # Off-contract for a *style* complaint: the model answered
                # a "rewrite this sentence" instruction with a tool call.
                # There is no second draft to ship, so the disposal table
                # applies — and at the only exit this can happen (zero
                # call) nothing has been spent, so the round simply
                # retries next tick.
                _LOGGER.info(
                    "%s tool loop: quality regeneration answered with a "
                    "tool call instead of prose character=%s — treating it "
                    "as no second draft", surface, character.id,
                )
                return None
            return _content_text(retry) or None

        review = await orchestrator.review(
            text,
            surface=surface,
            context_for=lambda candidate: self._quality_context(
                candidate, character=character, payload=payload, run=run,
            ),
            regenerate=regenerate,
            policy=OutputQualityPolicy.BACKGROUND_FAIL_CLOSED,
            character=character,
            max_retries=self._quality_gate_max_retries,
            enabled=self._quality_gate_enabled,
        )
        if review.skipped:
            _LOGGER.warning(
                "%s tool loop: quality gate withheld the composed prose "
                "character=%s axes=%s — sending no text this round",
                surface, character.id,
                ",".join(fired_axes(review.final_verdict)) or "-",
            )
            # ``skipped`` and ``hard_skipped`` coincide today (every other
            # outcome hands a candidate back), but the flag the caller acts
            # on is read off the outcome rather than off ``final is None``:
            # a future disposal that withholds for some other reason must
            # not silently inherit a retry policy written for this one.
            return _QualityOutcome(
                text=None,
                hard_skipped=review.outcome == OUTCOME_HARD_SKIPPED,
            )
        return _QualityOutcome(text=review.final or None)

    def _quality_context(
        self,
        candidate: str,
        *,
        character: Character,
        payload: PayloadT,
        run: _ToolRun,
    ) -> NoveltyGateContext:
        """What the nine-axis judge is shown for one draft.

        Built per candidate (the orchestrator calls this again for the
        regeneration) so the per-draft evidence describes the draft being
        judged rather than the one it replaced.

        ``content_tolerance`` is the payload's own, which is what routes
        the judge's model — and the material below obeys the same
        NSFW/frontier substitution the composer prompt does, so a frontier
        review is never handed raw text captured in NSFW mode.
        """
        return NoveltyGateContext(
            character_id=character.id,
            operator_id=getattr(character, "user_id", DEFAULT_OPERATOR_ID),
            response_text=candidate,
            known_material=_known_material(payload, run),
            latest_user_message=_latest_user_message(payload),
            content_tolerance=_content_tolerance(payload),
            persona_context=_persona_context(character),
            operator_primary_language=_operator_language(payload),
            # Deterministic evidence only — the verdict stays with the
            # judge (D6). No tool prompts travel this seam: the promise
            # loop hands the tool its arguments and never shows the player
            # a prompt, so there is nothing for ``tool_prompt_defect`` to
            # look at here.
            mechanical_evidence_lines=script_mix_lines(
                (candidate,),
                primary_language=_operator_language(payload),
            ),
            temporal_context_lines=_temporal_lines(payload),
        )

    # -- the honesty gate (HV1) -------------------------------------------

    async def _resolve_zero_call(
        self,
        *,
        character: Character,
        payload: PayloadT,
        surface: str,
        compose: Callable[[PayloadT], Awaitable[Any]],
        tools: tuple[PromptToolDescriptor, ...],
        text: str,
    ) -> _ZeroCallResolution:
        """Pass 1 answered in prose. May that prose go out?

        The round has an empty evidence list by construction — no tool was
        called — so *any* claim of a completed external action is
        unsupported, and the judge is being asked to tell that apart from
        the three legitimate shapes (a promise about later, an action
        inside the fiction, the player's own material read back).

        An empty ``text`` short-circuits: that is the composers' existing
        "retry next tick", and there is nothing to claim in it.

        QG4 runs the quality band *before* any of that, and before the
        ``self._guard is None`` short-circuit — a deployment with no
        honesty judge still owes the player readable prose. Whatever
        survives (the original, or a regeneration) is what the honesty
        gate below then reviews."""
        reviewed = await self._quality_reviewed(
            character=character,
            payload=payload,
            surface=surface,
            text=text,
            run=_ToolRun(),
            # Same compose shape the honesty correction uses at this exit,
            # tools included: the quality feedback is about the prose, and
            # narrowing the model's options as a side effect of a style
            # complaint would be a second, unrelated decision.
            recompose=lambda corrected: compose(
                replace(corrected, available_tools=tools),
            ),
        )
        if reviewed.text is None:
            # Nothing ran, so this is exactly the composers' empty-text
            # retry-next-tick — no honesty park, no attempt charged.
            return _ZeroCallResolution(
                message=ComposedMessage(
                    content_text="", quality_skipped=reviewed.hard_skipped,
                ),
            )
        text = reviewed.text
        shipped = ComposedMessage(content_text=text)
        if self._guard is None or not text:
            return _ZeroCallResolution(message=shipped)
        evidence = OutcomeClaimEvidence(
            offered_tools=tuple(tool.name for tool in tools),
        )

        def parked(reason: OutcomeClaimParkReason) -> _ZeroCallResolution:
            # Nothing ran, so the three-state fallback always lands on
            # "empty text" here — which is the composers' retry-next-tick.
            return _ZeroCallResolution(
                message=self._park(
                    payload=payload, run=_ToolRun(), surface=surface,
                    reason=reason,
                ),
            )

        verdict = await self._verdict_for(
            text, evidence=evidence, character=character, payload=payload,
        )
        if verdict.consistent:
            return _ZeroCallResolution(message=shipped)
        if verdict.unavailable:
            return parked(PARK_NO_VERDICT_ZERO_CALL)
        self._guard.record_block(after_tools=False)
        _LOGGER.warning(
            "%s tool loop: pass 1 called no tool but claimed %d completed "
            "outcome(s) character=%s — re-composing (max %d attempt(s)) "
            "with a correction",
            surface, len(verdict.unsupported_claims), character.id,
            MAX_HONESTY_RETRIES,
        )
        # B-5: MAX_HONESTY_RETRIES bounds this loop directly rather than
        # being a documented fact the single retry below just happens to
        # match — at its shipped value of 1 the loop runs exactly once,
        # identical to the pre-B-5 shape.
        claims = verdict.unsupported_claims
        for attempt in range(1, MAX_HONESTY_RETRIES + 1):
            corrected = _with_correction(
                payload,
                render_honesty_correction(CORRECTION_ZERO_CALL, claims),
            )
            if corrected is None:
                return parked(PARK_NO_CORRECTION_CHANNEL)
            retry = await compose(replace(corrected, available_tools=tools))
            retry_calls = _tool_calls(retry)
            if retry_calls:
                # It took the first road: the tool it should have called.
                self._guard.record_corrected()
                _LOGGER.info(
                    "%s tool loop: correction turned a claimed outcome "
                    "into a real %s call character=%s",
                    surface, retry_calls[0].name, character.id,
                )
                return _ZeroCallResolution(calls=retry_calls)
            retry_text = _content_text(retry)
            if not retry_text:
                return parked(PARK_CORRECTION_WROTE_NOTHING)
            second = await self._verdict_for(
                retry_text, evidence=evidence, character=character,
                payload=payload,
            )
            if second.consistent:
                self._guard.record_corrected()
                return _ZeroCallResolution(
                    message=ComposedMessage(content_text=retry_text),
                )
            if attempt < MAX_HONESTY_RETRIES:
                # Reoffended again, and the row still has attempts left —
                # one more blocked event, and the next pass corrects
                # against *this* verdict's claims, not the first one's.
                self._guard.record_block(after_tools=False)
                claims = second.unsupported_claims
                continue
            return parked(PARK_CORRECTION_CLAIMED_AGAIN)
        return parked(PARK_CORRECTION_CLAIMED_AGAIN)  # pragma: no cover

    async def _deliver_or_correct(
        self,
        *,
        character: Character,
        payload: PayloadT,
        surface: str,
        compose: Callable[[PayloadT], Awaitable[Any]],
        tools: tuple[PromptToolDescriptor, ...],
        body: str,
        run: _ToolRun,
    ) -> ComposedMessage:
        """Pass 2 wrote a message. Does it match what the tools returned?

        A softer failure than the zero-call one and a commoner one: the
        tool ran, and the prose rounded its result up — a search that
        found nothing written as "查到了", a render whose URL was dropped
        written as "傳過去了". The evidence therefore carries the
        *delivered* attachment count rather than the produced one, since
        that is what the player can check.

        Every refusal here lands in :meth:`_no_final_text` rather than in
        a bare empty message, because by this point a tool has usually
        spent something. Throwing away a rendered picture to retry the
        round would re-render it on the next reconcile, forever."""
        shipped = ComposedMessage(
            content_text=body, attachments=run.attachments,
        )
        if self._guard is None:
            return shipped
        evidence = OutcomeClaimEvidence(
            offered_tools=tuple(tool.name for tool in tools),
            outcomes=run.outcomes,
            delivered_attachments=len(run.attachments),
        )
        verdict = await self._verdict_for(
            body, evidence=evidence, character=character, payload=payload,
        )
        if verdict.consistent:
            return shipped
        if verdict.unavailable:
            return self._park(
                payload=payload, run=run, surface=surface,
                reason=PARK_NO_VERDICT_AFTER_TOOLS,
            )
        self._guard.record_block(after_tools=True)
        _LOGGER.warning(
            "%s tool loop: pass 2 claimed %d outcome(s) the tools did not "
            "deliver character=%s — re-composing (max %d attempt(s)) with "
            "a correction",
            surface, len(verdict.unsupported_claims), character.id,
            MAX_HONESTY_RETRIES,
        )
        # B-5: same single-origin loop as ``_resolve_zero_call`` above.
        claims = verdict.unsupported_claims
        for attempt in range(1, MAX_HONESTY_RETRIES + 1):
            corrected = _with_correction(
                payload,
                render_honesty_correction(CORRECTION_MISMATCH, claims),
            )
            if corrected is None:
                return self._park(
                    payload=payload,
                    run=run,
                    surface=surface,
                    reason=PARK_NO_CORRECTION_CHANNEL,
                    already_blocked=True,
                )
            retry = await compose(
                replace(
                    corrected, available_tools=(), tool_results=run.outcomes,
                ),
            )
            retry_body = _content_text(retry)
            if not retry_body:
                return self._park(
                    payload=payload,
                    run=run,
                    surface=surface,
                    reason=PARK_CORRECTION_WROTE_NOTHING,
                    already_blocked=True,
                )
            second = await self._verdict_for(
                retry_body, evidence=evidence, character=character,
                payload=payload,
            )
            if second.consistent:
                self._guard.record_corrected()
                return ComposedMessage(
                    content_text=retry_body, attachments=run.attachments,
                )
            if attempt < MAX_HONESTY_RETRIES:
                self._guard.record_block(after_tools=True)
                claims = second.unsupported_claims
                continue
            return self._park(
                payload=payload,
                run=run,
                surface=surface,
                reason=PARK_CORRECTION_OVERCLAIMED_AGAIN,
                already_blocked=True,
            )
        return self._park(  # pragma: no cover
            payload=payload,
            run=run,
            surface=surface,
            reason=PARK_CORRECTION_OVERCLAIMED_AGAIN,
            already_blocked=True,
        )

    async def _verdict_for(
        self,
        text: str,
        *,
        evidence: OutcomeClaimEvidence,
        character: Character,
        payload: PayloadT,
    ) -> OutcomeClaimVerdict:
        """One review, with the arguments every caller here passes.

        Both exits ask the same question twice (once for the original
        text, once for the correction), so the four keywords are spelled
        out in one place — a review that quietly stopped forwarding the
        evidence would still typecheck and still return verdicts."""
        assert self._guard is not None  # guarded by every caller
        return await self._guard.review(
            message_text=text,
            evidence=evidence,
            character=character,
            operator_primary_language=_operator_language(payload),
        )

    def _park(
        self, *, payload: PayloadT, run: _ToolRun, surface: str,
        reason: OutcomeClaimParkReason,
        already_blocked: bool = False,
    ) -> ComposedMessage:
        """Withhold the composed *prose*, without throwing away what a
        tool already produced.

        This is NOT unconditionally "send nothing this round" (S7): it
        delegates to :meth:`_no_final_text`, whose middle branch — a tool
        already produced deliverable attachments — ships them anyway,
        with a fixed fallback line standing in for the prose the gate
        just withheld. Recording that as a ``parked`` round (as this
        method did before S7) would have the audit trail and the
        Prometheus scrape both claim a round sent nothing when the player
        in fact received a message with a picture attached — exactly the
        kind of gap this honesty gate exists to not create *about itself*.

        So the record depends on what :meth:`_no_final_text` actually
        did: attachments shipped → this is a delivered-but-blocked round,
        folded into the existing ``blocked_after_tools`` counter rather
        than a new one (``already_blocked`` skips the increment when the
        caller already recorded one for this same verdict, so a single
        offence is not double-counted across the initial block and the
        park that follows it); nothing shipped → the composers' ordinary
        retry-next-tick, recorded as ``parked`` exactly as before.

        ``reason`` is a phrase/class pair rather than a bare string (F1):
        the caller that owns the retry has to treat "the model lied again"
        and "our judge is down" completely differently, and the loop is
        the only place that knows which happened."""
        result = self._no_final_text(
            payload=payload, run=run, surface=surface,
        )
        shipped_with_attachments = bool(result.attachments)
        if self._guard is not None:
            if shipped_with_attachments:
                if not already_blocked:
                    self._guard.record_block(after_tools=True)
                _LOGGER.warning(
                    "%s tool loop: honesty gate withheld the composed "
                    "text (%s) but %d already-produced attachment(s) "
                    "still shipped with a fallback line — not a parked "
                    "round", surface, reason.phrase,
                    len(result.attachments),
                )
            else:
                # HV3 deviation: the guard's ``reason`` param did not
                # exist when this call site was written (HV1). Passed
                # through here — additive, byte-identical for every other
                # caller — so the per-round audit trail can say *why* a
                # round was parked instead of only that it was. See HV3
                # deviations.
                self._guard.record_parked(
                    reason=reason.phrase, park_kind=reason.kind,
                )
                _LOGGER.warning(
                    "%s tool loop: honesty gate withheld the composed "
                    "message (%s)", surface, reason.phrase,
                )
        return result

    # -- internals --------------------------------------------------------

    def _no_final_text(
        self, *, payload: PayloadT, run: _ToolRun, surface: str,
        quality_skipped: bool = False,
    ) -> ComposedMessage:
        """Pass 2 wrote nothing usable. What that costs depends on the tool.

        The question is whether repeating the round is free, and the honest
        answer is "did a tool actually make something" — NOT "is the
        delivery list non-empty". The two part company precisely when a
        render succeeded and the deployment has no public base URL to serve
        it from: the GPU ran, the credits are gone, the file is on disk, and
        the delivery list is empty because the URL was dropped. Judging by
        the delivery list there would return "retry next tick" and re-render
        the same picture every reconcile, forever, on exactly the deployment
        that cannot ship it.

        So:

        * nothing produced → the round only cost a lookup, so it repeats:
          empty text is the composers' "retry next tick";
        * produced and deliverable → ship the picture with a fixed localized
          line in place of the prose, the same trade the chat loop makes
          (:data:`DELIVERED_WITHOUT_TEXT_FALLBACK_KEY`);
        * produced but undeliverable → the promise is still answered, in
          words, and the row is done — but NOT with the line above, which
          claims the picture arrived. See
          :data:`UNDELIVERABLE_ARTIFACT_FALLBACK_KEY`.

        ``quality_skipped`` is forwarded onto the first branch only, which
        is the only one that ends the round with nothing to send — the
        other two ship a message, and a caller reading the flag on a
        delivered message would be reading it about a round that was not
        withheld at all."""
        tools_ran = ", ".join(o.tool_name for o in run.outcomes) or "no tool"
        if not run.produced_artifacts:
            _LOGGER.info(
                "%s tool loop: second pass produced no text after %s",
                surface, tools_ran,
            )
            return ComposedMessage(
                content_text="", quality_skipped=quality_skipped,
            )
        if run.attachments:
            _LOGGER.warning(
                "%s tool loop: second pass produced no text after %s but %d "
                "attachment(s) were already produced — shipping them with the "
                "localized fallback instead of re-running the tool",
                surface, tools_ran, len(run.attachments),
            )
            return ComposedMessage(
                content_text=localized_fallback_text(
                    DELIVERED_WITHOUT_TEXT_FALLBACK_KEY,
                    _operator_language(payload),
                ),
                attachments=run.attachments,
            )
        _LOGGER.warning(
            "%s tool loop: %s produced %d artifact(s) that this deployment "
            "cannot deliver (no messaging public base URL) and the second "
            "pass wrote nothing — answering the promise in words rather than "
            "re-running the tool every reconcile. Set Admin Channel settings "
            "Public Base URL or APP_BASE_URL",
            surface, tools_ran, run.produced_artifacts,
        )
        return ComposedMessage(
            content_text=localized_fallback_text(
                UNDELIVERABLE_ARTIFACT_FALLBACK_KEY,
                _operator_language(payload),
            ),
        )

    async def _maybe_defer(
        self,
        calls: tuple[ToolCall, ...],
        schedule_capability: Callable[[str], Awaitable[bool]] | None,
        *,
        surface: str,
    ) -> str:
        """Return the capability whose invocation the caller took over.

        Empty string = run the calls here. Note the ordering: the model
        has already chosen the tool, so this is not a *prediction* that a
        GPU is wanted — it is the fact, which is why the decision belongs
        here and not at enqueue time.

        Fail-soft in the direction that keeps promises: a scheduler that
        raises, or declines, leaves us running the tool inline. The gate
        it protects is a concurrency ceiling, not a permission check —
        overshooting it briefly is a smaller harm than a character who
        said "晚點傳照片給你" and never did."""
        if schedule_capability is None:
            return TOOL_CAPABILITY_NONE
        for call in calls:
            capability = self._capability_of(call.name, surface=surface)
            if not capability:
                continue
            try:
                taken = await schedule_capability(capability)
            except Exception:  # noqa: BLE001 - isolation is the point
                _LOGGER.exception(
                    "%s tool loop: capability scheduler crashed tool=%s "
                    "capability=%s — running inline",
                    surface, call.name, capability,
                )
                return TOOL_CAPABILITY_NONE
            if taken:
                _LOGGER.info(
                    "%s tool loop: %s deferred to the %s queue — this pass "
                    "sends nothing", surface, call.name, capability,
                )
                return capability
        return TOOL_CAPABILITY_NONE

    def _capability_of(self, tool_name: str, *, surface: str) -> str:
        if self._registry is None:
            return TOOL_CAPABILITY_NONE
        try:
            tool = self._registry.get(tool_name)
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception(
                "%s tool loop: registry get failed tool=%s",
                surface, tool_name,
            )
            return TOOL_CAPABILITY_NONE
        return tool_capability(tool) if tool is not None else TOOL_CAPABILITY_NONE

    def _describe_tools(
        self,
        character: Character,
        *,
        surface: str,
        withheld_capabilities: frozenset[str] = frozenset(),
    ) -> tuple[PromptToolDescriptor, ...]:
        """The tool list pass 1 gets to choose from — the ONLY place one is
        built, so "the character never picked it" and "the character cannot
        run it" can never disagree.

        ``withheld_capabilities`` drops the tools whose capability this
        deployment has switched off. The judgement stays structural (a
        tool's declared ``capability`` vs the operator's cap table); there
        is no per-tool list and nothing is matched on names."""
        if self._registry is None:
            return ()
        try:
            tools = self._registry.list_for_character(character)
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception(
                "%s tool loop: registry lookup failed character=%s",
                surface, character.id,
            )
            return ()
        offered: list[PromptToolDescriptor] = []
        for t in tools:
            capability = tool_capability(t)
            if capability and capability in withheld_capabilities:
                _LOGGER.info(
                    "%s tool loop: withholding %s from character=%s — this "
                    "deployment runs no background %s (cap 0), so the "
                    "fulfilment answers in words instead",
                    surface, t.name, character.id, capability,
                )
                continue
            offered.append(
                PromptToolDescriptor(
                    name=t.name,
                    description=t.description,
                    parameters_schema=t.parameters_schema,
                ),
            )
        return tuple(offered)

    async def _execute(
        self,
        *,
        character: Character,
        calls: tuple[ToolCall, ...],
        surface: str,
        conversation_id: str | None,
        recent_dialogue: str,
    ) -> _ToolRun:
        assert self._orchestrator is not None  # guarded by caller
        public_base_url = await self._resolve_public_base_url(surface=surface)
        outcomes: list[ToolOutcomeMessage] = []
        attachments: list[OutboundAttachment] = []
        produced = 0
        for call in calls:
            try:
                _, result = await self._orchestrator.execute(
                    character=character,
                    call=call,
                    conversation_id=conversation_id,
                    recent_dialogue=recent_dialogue,
                )
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                _LOGGER.exception(
                    "%s tool loop: orchestrator crashed tool=%s",
                    surface, call.name,
                )
                outcomes.append(
                    ToolOutcomeMessage(
                        tool_name=call.name,
                        ok=False,
                        output_text="",
                        error=f"tool crashed: {exc}",
                    ),
                )
                continue
            if not result.ok:
                _LOGGER.info(
                    "%s tool loop: tool %s failed: %s",
                    surface, call.name, result.error,
                )
                outcomes.append(
                    ToolOutcomeMessage(
                        tool_name=call.name,
                        ok=False,
                        output_text="",
                        error=result.error or "unknown error",
                    ),
                )
                continue
            # Counted before the delivery filter: what the tool spent is a
            # fact about the tool, and ``to_outbound_attachments`` can drop
            # every one of these when no public base URL is configured.
            artifacts = tuple(result.attachments)
            produced += len(artifacts)
            delivered = to_outbound_attachments(
                artifacts,
                public_base_url=public_base_url,
                surface=surface,
            )
            attachments.extend(delivered)
            outcomes.append(
                ToolOutcomeMessage(
                    tool_name=call.name,
                    ok=True,
                    output_text=result.output_text,
                    attachment_urls=tuple(a.url for a in delivered),
                ),
            )
        return _ToolRun(
            outcomes=tuple(outcomes),
            attachments=tuple(attachments),
            produced_artifacts=produced,
        )

    async def _resolve_public_base_url(self, *, surface: str) -> str:
        if self._public_base_url_provider is None:
            return self._public_base_url
        try:
            resolved = await self._public_base_url_provider()
        except Exception:
            _LOGGER.exception(
                "%s tool loop: public base URL provider failed; using env "
                "fallback", surface,
            )
            return self._public_base_url
        if not isinstance(resolved, str):
            return self._public_base_url
        resolved = resolved.strip().rstrip("/")
        return resolved or self._public_base_url


def _surface_of(payload: Any, default: str) -> str:
    """Which seam this round belongs to, as an observability label.

    A *label*, never a behaviour switch — nothing in the loop branches on
    the result, which is why an exact-type lookup with a fallback is the
    right shape: an unrecognised payload does not get guessed at, it keeps
    the label the loop was constructed with.
    """
    return _PAYLOAD_SURFACES.get(type(payload), default)


def _content_text(output: Any) -> str:
    return (getattr(output, "content_text", "") or "").strip()


def _tool_calls(output: Any) -> tuple[ToolCall, ...]:
    return tuple(getattr(output, "tool_calls", ()) or ())


def _with_correction(payload: Any, correction: str) -> Any | None:
    """A copy of ``payload`` carrying the retry's correction instruction.

    ``None`` when the payload has no
    :data:`HONESTY_CORRECTION_FIELD` — a stand-in the loop was handed by a
    caller that predates HV1. Re-composing such a payload would send the
    model the *identical* prompt that just produced the overclaim, so the
    only honest answer is to stop rather than to burn a second call on a
    round whose outcome is already known."""
    if not hasattr(payload, HONESTY_CORRECTION_FIELD):
        _LOGGER.warning(
            "composer tool loop: %s payload carries no %r field — cannot "
            "re-compose with a correction",
            type(payload).__name__, HONESTY_CORRECTION_FIELD,
        )
        return None
    return replace(payload, **{HONESTY_CORRECTION_FIELD: correction})


def _content_tolerance(payload: Any) -> str:
    """The payload's own content tolerance, normalised.

    Read rather than assumed because it decides which model the quality
    judge runs on: a community-tolerance round reviewed by a frontier
    provider would come back refused, and the fail-open path would turn
    every NSFW-mode follow-up into an unreviewed one."""
    return normalize_content_tolerance(
        getattr(payload, "content_tolerance", CONTENT_TOLERANCE_FRONTIER),
    )


def _safe_text(
    text: str,
    *,
    content_mode: Any,
    safe_summary: str,
    content_tolerance: str,
) -> str:
    """``text``, or its frontier-safe stand-in when the round is frontier.

    The same substitution both composer prompts make for the same reason:
    text captured while the conversation was in NSFW mode must not reach a
    frontier provider. The quality judge is just another such call, so it
    obeys the same rule — and when there is no safe summary the material
    is simply omitted rather than downgraded, because a partial paraphrase
    invented here would be evidence nobody wrote."""
    if (
        content_mode is MessageContentMode.NSFW
        and normalize_content_tolerance(content_tolerance)
        == CONTENT_TOLERANCE_FRONTIER
    ):
        return (safe_summary or "").strip()
    return (text or "").strip()


def _queued_message_texts(payload: Any) -> tuple[str, ...]:
    """The queued player messages a busy-defer follow-up owes a reply to.

    Empty for the scheduled-promise payload, which has no backlog — the
    loop is generic over both, so every payload read here is a ``getattr``
    rather than a type check."""
    tolerance = _content_tolerance(payload)
    texts: list[str] = []
    for message in getattr(payload, "queued_messages", ()) or ():
        text = _safe_text(
            getattr(message, "content", ""),
            content_mode=getattr(message, "content_mode", None),
            safe_summary=getattr(message, "safe_summary", ""),
            content_tolerance=tolerance,
        )
        if text:
            texts.append(text)
    return tuple(texts)


def _promise_text(payload: Any) -> str:
    """The player's original wording behind a scheduled promise."""
    return _safe_text(
        getattr(payload, "promise_text", ""),
        content_mode=getattr(payload, "promise_content_mode", None),
        safe_summary=getattr(payload, "promise_safe_summary", ""),
        content_tolerance=_content_tolerance(payload),
    )


def _latest_user_message(payload: Any) -> str:
    """What this message is answering, as the judge understands it.

    The newest queued message for a busy-defer follow-up; the original
    request for a scheduled promise. Empty when neither exists, which the
    gate prompt renders as 「（無）」 rather than guessing."""
    queued = _queued_message_texts(payload)
    if queued:
        return queued[-1]
    return _promise_text(payload)


def _temporal_lines(payload: Any) -> tuple[str, ...]:
    """The 時間座標 block for a deferred reply — the surface built to be late.

    Both payloads this loop serves exist *because* time passed: a
    busy-defer follow-up answers messages the character could not get to,
    and a scheduled promise fires at a moment agreed earlier. Whether that
    gap makes the draft absurd is the judge's call, so every instant the
    payload holds is stated as fact and none of it is thresholded here.

    Read by ``getattr`` like every other helper in this file: the loop is
    generic over two unrelated payload types, and each carries the subset
    of anchors its own surface has.
    """
    now = getattr(payload, "now", None)
    events: list[TemporalEvent] = []
    queued = _queued_message_texts(payload)
    first_queued_at = getattr(payload, "queued_at", None)
    if first_queued_at is not None:
        events.append(
            quoted_event(
                "對方第一則等回覆的訊息",
                queued[0] if queued else "",
                first_queued_at,
            ),
        )
    promise_made_at = getattr(payload, "promise_made_at", None)
    if promise_made_at is not None:
        events.append(
            quoted_event("你答應這件事", _promise_text(payload), promise_made_at),
        )
    scheduled_for = getattr(payload, "scheduled_for", None)
    if scheduled_for is not None:
        events.append(("約定的時間", scheduled_for))
    return render_temporal_context_lines(
        now=now,
        local_tz=getattr(payload, "local_tz", timezone.utc),
        events=events,
    )


def _known_material(payload: Any, run: _ToolRun) -> tuple[str, ...]:
    """What this draft was legitimately written from.

    Without it the ``lacks_novelty`` axis has nothing to compare against
    and every faithful reply looks like an invention. Assembled from the
    compose input the loop is already holding — plus, at the pass-2 exit,
    what the tools returned, because that is the material the second pass
    was explicitly told to write from.
    """
    lines: list[str] = []
    for value in (
        getattr(payload, "promise_intent", ""),
        _promise_text(payload),
        getattr(payload, "brief_reply", ""),
        getattr(payload, "defer_reason", ""),
        getattr(payload, "recent_dialogue_summary", None) or "",
    ):
        text = (value or "").strip() if isinstance(value, str) else ""
        if text and text not in lines:
            lines.append(text)
    for text in _queued_message_texts(payload):
        if text not in lines:
            lines.append(text)
    for outcome in run.outcomes:
        text = (outcome.output_text or "").strip()
        if text and text not in lines:
            lines.append(text)
    return tuple(lines)


def _persona_context(character: Character) -> tuple[str, ...]:
    """The two persona facts the register axis needs, and no more.

    Same pair every other adopting surface passes. Deliberately not the
    whole persona: the judge is scoring *register drift*, and a full
    character sheet turns it into a fan of the character."""
    personality = "、".join(character.personality or ())
    lines = []
    if personality:
        lines.append(f"性格：{personality}")
    if (character.speaking_style or "").strip():
        lines.append(f"說話風格：{character.speaking_style.strip()}")
    return tuple(lines)


def _operator_language(payload: Any) -> str:
    """The operator language both composer payloads already carry.

    Read with ``getattr`` like the rest of this module's payload access:
    the loop is generic over the two compose-input dataclasses (and the
    fakes tests hand it), and a payload without the field must fall back
    to the catalog default rather than raise on a fail-soft path."""
    value = getattr(payload, "operator_primary_language", "")
    return value if isinstance(value, str) else ""


__all__ = [
    "DELIVERED_WITHOUT_TEXT_FALLBACK_KEY",
    "HONESTY_CORRECTION_FIELD",
    "MAX_COMPOSE_PASSES",
    "MAX_HONESTY_RETRIES",
    "MAX_QUALITY_RETRIES",
    "MAX_TOOL_CALLS_PER_COMPOSE",
    "SURFACE_FOLLOW_UP",
    "SURFACE_PROMISE",
    "UNDELIVERABLE_ARTIFACT_FALLBACK_KEY",
    "ComposedMessage",
    "ComposerToolLoop",
    "render_quality_correction",
]
