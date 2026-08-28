"""Per-turn auxiliary prompt context, loaded in two parallel waves.

Both reply paths (the streaming branch of ``_start_message_stream`` and
``_generate_reply_with_tools``, which also backs the non-streaming
``send_message``) need the same thirteen enrichment inputs before they can
build a prompt: an image-recognition blurb, seven DB reads, two aux-LLM
calls, an embedding pass and two pure renders.

They used to be awaited one after another. Most of them are network calls
that do not depend on each other, so the turn paid the *sum* of their
latencies — 1.5–3s of avoidable wall clock in front of the first token.

This module keeps the same call set and the same inputs but runs them in
two waves:

* **Wave 1** — everything whose inputs are ready when the turn starts: all
  DB reads, the vision recognition call and the diversity embedding pass.
* **Wave 2** — the two remaining aux-LLM calls, each of which consumes a
  wave-1 result (curiosity wants the relationship seed lines, the register
  profile wants the persona and seed lines).

The two synchronous steps (persona line rendering, script-mix evidence)
sit between the waves, where their inputs land.

The material digest used to be wave 2's third call. DIGEST_OFFPATH moved
it off the turn entirely: it is budgeted by the previous turn's post-turn
and read here as one primary-key SELECT in **wave 1** — see
:mod:`material_digest_precompute`. Wave 1 is where it belongs: its inputs
(character, operator, tolerance) are all ready when the turn starts, and
neither wave-2 call consumes it, so parking it in wave 2 would have made
it wait for a dependency it does not have. A miss yields ``None``, the
digest's long-supported "render the source blocks" path, and is **never**
filled in inline; computing it here is precisely the latency that ticket
removed.

Error semantics are deliberately identical to the serial version:

* ``asyncio.gather`` is used **without** ``return_exceptions``. A loader
  that raises still kills the turn, exactly as it did when the awaits were
  written out in a line, and with the same exception type — no
  ``ExceptionGroup`` wrapping, which is why this is a ``gather`` and not a
  ``TaskGroup``. Its siblings are *not* cancelled (that is ``gather``'s
  documented behaviour): they run to completion and their results are
  dropped. Deliberate — cancelling them would tear down an in-flight DB
  write such as the curiosity attempt ledger row, trading a dead turn for
  a half-written one.
* In production this propagation path is theoretical: every wired loader
  — the seven chat-service DB loads, the aux-LLM adapters, and the
  vision/embedding paths — swallows its own failures and returns a
  fallback (``[]`` / ``None`` / ``""`` / degraded evidence), same as it
  did under the serial code. The kill-the-turn contract exists so a
  *future* loader without such a net keeps the serial failure semantics.
* Both gathers are awaited inline, never spun off as fire-and-forget
  tasks: a client disconnect cancels the awaiting coroutine and the
  cancellation propagates into the children, leaving no orphaned task.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from kokoro_link.contracts.embedder import EmbedderPort
from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.contracts.prompt_material_digest import PromptMaterialDigest
from kokoro_link.contracts.register_profile import RegisterProfile
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Message
from kokoro_link.domain.entities.emotion_event import EmotionEvent
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.diversity.reply_evidence import (
    build_reply_diversity_evidence,
)

__all__ = [
    "ScriptMixDecorator",
    "TurnAuxContext",
    "TurnAuxLoaders",
    "load_turn_aux_context",
]


@dataclass(frozen=True)
class TurnAuxContext:
    """Everything the two reply paths load before building a prompt.

    Field names mirror the local variables the call sites used to bind, so
    the downstream prompt build / address resolution / gate risk scoring
    read exactly the same values under exactly the same names.
    """

    image_recognition_context: str
    operator_persona: Any
    operator_persona_lines: list[str]
    player_persona_note: str
    peer_roster_lines: list[str]
    initial_relationship_lines: list[str]
    persona_curiosity_plan: PersonaCuriosityPlan | None
    emotion_events: list[EmotionEvent]
    self_reflections: list
    material_digest: PromptMaterialDigest | None
    phrase_habit_lines: list[str]
    register_profile: RegisterProfile | None
    diversity_evidence: ReplyDiversityEvidence


class TurnAuxLoaders(Protocol):
    """The slice of ``ChatService`` this module drives.

    The loaders stay bound methods on the service — they read its wired
    repositories and ports, and a pile of existing tests reach for them
    there. This protocol only names the collaboration so the helper can be
    exercised against a stub.
    """

    async def _build_image_recognition_context(
        self,
        *,
        character: Character,
        main_model: Any,
        attachment_urls: Sequence[str],
        content_tolerance: str,
    ) -> str: ...

    async def _load_operator_persona(
        self,
        character_id: str,
        operator: OperatorProfile | None,
    ) -> Any: ...

    def _render_operator_persona_lines(self, persona: Any) -> list[str]: ...

    async def _load_player_persona_note(
        self,
        character_id: str,
        operator: OperatorProfile | None,
        *,
        enabled: bool,
    ) -> str: ...

    async def _load_peer_roster_lines(self, character_id: str) -> list[str]: ...

    async def _load_initial_relationship_lines(
        self,
        character_id: str,
        operator: OperatorProfile | None,
    ) -> list[str]: ...

    async def _load_persona_curiosity_plan(
        self,
        *,
        character: Character,
        operator: OperatorProfile | None,
        enabled: bool,
        conversation_id: str | None,
        recent_dialogue_summary: str,
        initial_relationship_lines: list[str] | tuple[str, ...],
        now: datetime | None,
    ) -> PersonaCuriosityPlan | None: ...

    async def _load_recent_emotion_events(
        self,
        *,
        character_id: str,
        operator: OperatorProfile | None,
        now: datetime,
    ) -> list[EmotionEvent]: ...

    async def _load_self_reflections(
        self,
        *,
        character_id: str,
        operator: OperatorProfile | None,
    ) -> list: ...

    async def _load_cached_prompt_material_digest(
        self,
        *,
        character: Character,
        operator: OperatorProfile | None,
        content_tolerance: str,
        now: datetime,
    ) -> PromptMaterialDigest | None: ...

    async def _load_phrase_habit_lines(self, character_id: str) -> list[str]: ...

    async def _load_register_profile(
        self,
        *,
        character: Character,
        operator: OperatorProfile | None,
        latest_user_message: str,
        recent_dialogue_summary: str,
        relationship_context: tuple[str, ...],
        content_tolerance: str,
    ) -> RegisterProfile | None: ...


class ScriptMixDecorator(Protocol):
    """``chat_service._with_script_mix_evidence``, injected.

    It is a module-level function in ``chat_service``, which imports this
    module — taking it as a parameter keeps the dependency one-way.
    """

    def __call__(
        self,
        evidence: ReplyDiversityEvidence,
        *,
        recent_messages: list[Message],
        primary_language: str = "",
    ) -> ReplyDiversityEvidence: ...


async def _operator_persona_or_none(
    loaders: TurnAuxLoaders,
    *,
    character_id: str,
    operator: OperatorProfile | None,
    load: bool,
) -> Any:
    """Wave-1 slot for the persona aggregate.

    The two call sites disagree on whether the persona is gated: the
    streaming path loads it unconditionally (it also feeds the address
    resolver there), the tool path only loads it when persona prompt lines
    are enabled. Expressed as a flag rather than branching at the call
    sites so both keep one wave-1 shape.
    """
    if not load:
        return None
    return await loaders._load_operator_persona(character_id, operator)


async def load_turn_aux_context(
    loaders: TurnAuxLoaders,
    *,
    character: Character,
    operator: OperatorProfile | None,
    conversation_id: str | None,
    now: datetime,
    main_model: Any,
    vision_urls: Sequence[str],
    recognition_content_tolerance: str,
    content_tolerance: str,
    persona_enabled: bool,
    load_operator_persona: bool,
    latest_user_message: str,
    recent_dialogue_summary: str,
    diversity_messages: list[Message],
    self_repetition_hint: str | None,
    embedder: EmbedderPort | None,
    script_mix_decorator: ScriptMixDecorator,
) -> TurnAuxContext:
    """Load one turn's auxiliary prompt context in two parallel waves.

    *recognition_content_tolerance* is the CONTENT-driven tolerance the
    image recognition call routes on; *content_tolerance* is the
    main-model-derived one the aux-LLM calls use. They differ on purpose —
    see the comments at both call sites.

    The story material (events / arc / beats) and the feed posts are no
    longer parameters: the only step that read them was the material
    digest, which now re-reads its own inputs on the post-turn side
    (DIGEST_OFFPATH). The caller still loads them for the prompt builder.
    """
    (
        image_recognition_context,
        operator_persona,
        player_persona_note,
        peer_roster_lines,
        initial_relationship_lines,
        emotion_events,
        self_reflections,
        phrase_habit_lines,
        material_digest,
        raw_diversity_evidence,
    ) = await asyncio.gather(
        loaders._build_image_recognition_context(
            character=character,
            main_model=main_model,
            attachment_urls=vision_urls,
            content_tolerance=recognition_content_tolerance,
        ),
        _operator_persona_or_none(
            loaders,
            character_id=character.id,
            operator=operator,
            load=load_operator_persona,
        ),
        loaders._load_player_persona_note(
            character.id,
            operator,
            enabled=persona_enabled,
        ),
        loaders._load_peer_roster_lines(character.id),
        loaders._load_initial_relationship_lines(character.id, operator),
        loaders._load_recent_emotion_events(
            character_id=character.id, operator=operator, now=now,
        ),
        loaders._load_self_reflections(
            character_id=character.id, operator=operator,
        ),
        loaders._load_phrase_habit_lines(character.id),
        # Budgeted by the previous turn's post-turn; a miss is a supported
        # render, never a reason to call upstream from here.
        loaders._load_cached_prompt_material_digest(
            character=character,
            operator=operator,
            content_tolerance=content_tolerance,
            now=now,
        ),
        build_reply_diversity_evidence(
            recent_messages=diversity_messages,
            self_repetition_hint=self_repetition_hint,
            embedder=embedder,
        ),
    )

    operator_persona_lines = loaders._render_operator_persona_lines(operator_persona)
    diversity_evidence = script_mix_decorator(
        raw_diversity_evidence,
        recent_messages=diversity_messages,
        primary_language=getattr(operator, "primary_language", "") or "",
    )

    persona_curiosity_plan, register_profile = await asyncio.gather(
        loaders._load_persona_curiosity_plan(
            character=character,
            operator=operator,
            enabled=persona_enabled,
            conversation_id=conversation_id,
            recent_dialogue_summary=recent_dialogue_summary,
            initial_relationship_lines=initial_relationship_lines,
            now=now,
        ),
        loaders._load_register_profile(
            character=character,
            operator=operator,
            latest_user_message=latest_user_message,
            recent_dialogue_summary=recent_dialogue_summary,
            relationship_context=tuple(
                [
                    *(operator_persona_lines or []),
                    *(initial_relationship_lines or []),
                ],
            ),
            content_tolerance=content_tolerance,
        ),
    )

    return TurnAuxContext(
        image_recognition_context=image_recognition_context,
        operator_persona=operator_persona,
        operator_persona_lines=operator_persona_lines,
        player_persona_note=player_persona_note,
        peer_roster_lines=peer_roster_lines,
        initial_relationship_lines=initial_relationship_lines,
        persona_curiosity_plan=persona_curiosity_plan,
        emotion_events=emotion_events,
        self_reflections=self_reflections,
        material_digest=material_digest,
        phrase_habit_lines=phrase_habit_lines,
        register_profile=register_profile,
        diversity_evidence=diversity_evidence,
    )
