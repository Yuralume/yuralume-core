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
from typing import Any, Protocol, TypeVar

from kokoro_link.application.services.tool_attachment_delivery import (
    to_outbound_attachments,
)
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)
from kokoro_link.contracts.messaging import OutboundAttachment
from kokoro_link.contracts.prompt import PromptToolDescriptor, ToolOutcomeMessage
from kokoro_link.contracts.tool import (
    TOOL_CAPABILITY_NONE,
    ToolRegistryPort,
    tool_capability,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.tool_call import ToolCall

_LOGGER = logging.getLogger(__name__)

MAX_TOOL_CALLS_PER_COMPOSE = 1
"""One tool per fulfilment, mirroring the chat / proactive contract.
A promise that genuinely needs two tools degrades to doing the first
one and saying so — better than an unbounded background tool budget."""

MAX_COMPOSE_PASSES = 2
"""compose → tools → compose. There is no third pass: the second one
is told to write the message with what it got, not to ask again."""

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
        surface: str = "promise",
        capability_caps: Mapping[str, int] | None = None,
    ) -> None:
        self._registry = tool_registry
        self._orchestrator = tool_orchestrator
        self._public_base_url = (public_base_url or "").strip().rstrip("/")
        self._public_base_url_provider = public_base_url_provider
        self._surface = surface
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
        tools = self._describe_tools(
            character,
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
            first = await compose(payload)
            return ComposedMessage(content_text=_content_text(first))

        first = await compose(replace(payload, available_tools=tools))
        calls = _tool_calls(first)
        if not calls:
            return ComposedMessage(content_text=_content_text(first))
        if len(calls) > MAX_TOOL_CALLS_PER_COMPOSE:
            _LOGGER.info(
                "%s tool loop: composer asked for %d calls — running the "
                "first only (cap=%d)",
                self._surface, len(calls), MAX_TOOL_CALLS_PER_COMPOSE,
            )
            calls = calls[:MAX_TOOL_CALLS_PER_COMPOSE]

        deferred = await self._maybe_defer(calls, schedule_capability)
        if deferred:
            return ComposedMessage(content_text="", deferred_capability=deferred)

        run = await self._execute(
            character=character,
            calls=calls,
            conversation_id=conversation_id,
            recent_dialogue=recent_dialogue,
        )
        second = await compose(
            replace(payload, available_tools=(), tool_results=run.outcomes),
        )
        body = _content_text(second)
        if not body:
            return self._no_final_text(payload=payload, run=run)
        return ComposedMessage(content_text=body, attachments=run.attachments)

    # -- internals --------------------------------------------------------

    def _no_final_text(
        self, *, payload: PayloadT, run: _ToolRun,
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
          :data:`UNDELIVERABLE_ARTIFACT_FALLBACK_KEY`."""
        tools_ran = ", ".join(o.tool_name for o in run.outcomes) or "no tool"
        if not run.produced_artifacts:
            _LOGGER.info(
                "%s tool loop: second pass produced no text after %s",
                self._surface, tools_ran,
            )
            return ComposedMessage(content_text="")
        if run.attachments:
            _LOGGER.warning(
                "%s tool loop: second pass produced no text after %s but %d "
                "attachment(s) were already produced — shipping them with the "
                "localized fallback instead of re-running the tool",
                self._surface, tools_ran, len(run.attachments),
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
            self._surface, tools_ran, run.produced_artifacts,
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
            capability = self._capability_of(call.name)
            if not capability:
                continue
            try:
                taken = await schedule_capability(capability)
            except Exception:  # noqa: BLE001 - isolation is the point
                _LOGGER.exception(
                    "%s tool loop: capability scheduler crashed tool=%s "
                    "capability=%s — running inline",
                    self._surface, call.name, capability,
                )
                return TOOL_CAPABILITY_NONE
            if taken:
                _LOGGER.info(
                    "%s tool loop: %s deferred to the %s queue — this pass "
                    "sends nothing", self._surface, call.name, capability,
                )
                return capability
        return TOOL_CAPABILITY_NONE

    def _capability_of(self, tool_name: str) -> str:
        if self._registry is None:
            return TOOL_CAPABILITY_NONE
        try:
            tool = self._registry.get(tool_name)
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception(
                "%s tool loop: registry get failed tool=%s",
                self._surface, tool_name,
            )
            return TOOL_CAPABILITY_NONE
        return tool_capability(tool) if tool is not None else TOOL_CAPABILITY_NONE

    def _describe_tools(
        self,
        character: Character,
        *,
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
                self._surface, character.id,
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
                    self._surface, t.name, character.id, capability,
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
        conversation_id: str | None,
        recent_dialogue: str,
    ) -> _ToolRun:
        assert self._orchestrator is not None  # guarded by caller
        public_base_url = await self._resolve_public_base_url()
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
                    self._surface, call.name,
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
                    self._surface, call.name, result.error,
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
                surface=self._surface,
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

    async def _resolve_public_base_url(self) -> str:
        if self._public_base_url_provider is None:
            return self._public_base_url
        try:
            resolved = await self._public_base_url_provider()
        except Exception:
            _LOGGER.exception(
                "%s tool loop: public base URL provider failed; using env "
                "fallback", self._surface,
            )
            return self._public_base_url
        if not isinstance(resolved, str):
            return self._public_base_url
        resolved = resolved.strip().rstrip("/")
        return resolved or self._public_base_url


def _content_text(output: Any) -> str:
    return (getattr(output, "content_text", "") or "").strip()


def _tool_calls(output: Any) -> tuple[ToolCall, ...]:
    return tuple(getattr(output, "tool_calls", ()) or ())


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
    "MAX_COMPOSE_PASSES",
    "MAX_TOOL_CALLS_PER_COMPOSE",
    "UNDELIVERABLE_ARTIFACT_FALLBACK_KEY",
    "ComposedMessage",
    "ComposerToolLoop",
]
