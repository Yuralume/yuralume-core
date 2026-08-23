"""Branching-drama orchestrator.

Coordinates two lifecycles:

**Creation** — background pipeline that generates the initial outline
layers (root + first children), then pre-generates images for those
layers:

    generating_outlines → generating_images → ready

**Gameplay** — synchronous per-request flow with lazy generation:

    start_session → (narrate root) → advance → advance → … → end

Deeper outline layers and images are generated lazily as the player
advances, prefetching 2 layers ahead in the background. Image prefetch
depth is separately configurable (``image_prefetch_depth``) because
outlines are cheap text and scene art is not.

**Action-priced, one price per button** (BD plan D2). The family used to be
``charging_mode=legacy`` on the argument that a drama has no bounded unit of
work — how deep the player walks the tree and how many pictures the prefetch
draws are both open-ended. That argument was answered rather than waived: the
billable unit is not the drama, it is each press. ``branching_drama_create``
covers the tree's creation (root plan, the initial layers, their art);
``branching_drama_advance`` covers one beat *and* the lazy layer plus prefetch
it pulls in behind it — including the *opening* beat, which is the zeroth
press and has the same shape (FX1); ``branching_drama_interact`` covers one
in-beat exchange. Each of those is a constant amount of generation — advance
does not get more expensive at depth six than at depth two — so a fixed price
can honour it.

Two consequences are load-bearing here. A press that delivers nothing — the
409 while another replica owns the next layer, a node with no children —
releases in full: the player pays for beats they got. And an advance's charge
outlives its HTTP response: the prefetch it pays for (D3's "advance ≈ 3
planner ＋ 旁白 ＋ 3 張圖") is spawned, not awaited, so settling at the end of
the request would leave those calls uncovered and the Gateway would bill them
per call *on top of* the fixed price — the double bill the fixed price exists
to remove. Ownership of the handle therefore passes to the prefetch task,
which settles when it is done.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from typing import TYPE_CHECKING

from kokoro_link.application.services.branching_drama_critic import (
    BranchingDramaCritic,
)
from kokoro_link.application.services.branching_drama_director import (
    BranchingDramaDirector,
)
from kokoro_link.application.services.branching_drama_gallery import (
    DramaSceneGallery,
    build_scene_gallery,
)
from kokoro_link.application.services.branching_drama_planner import (
    BranchingDramaPlanner,
    NodeOutline,
)
from kokoro_link.application.services.branching_drama_polisher import (
    BranchingDramaPolisher,
)
from kokoro_link.application.services.character_activity_anchor import (
    CharacterActivityAnchor,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.cloud_action_billing_service import (
    ActionChargeHandle,
    CloudActionBillingService,
    NullActionBillingService,
)
from kokoro_link.application.services.studio_execution_lease import (
    LEASE_ABANDONED,
    StudioExecutionLease,
    StudioLeaseLost,
    StudioLeaseSession,
)

if TYPE_CHECKING:
    from kokoro_link.application.services.event_seed_dispenser import (
        EventSeedDispenser,
    )
from kokoro_link.application.services.studio_failure import (
    failure_error_code,
)
from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
    FusionCharacterBriefBuilder,
)
from kokoro_link.application.services.visual_generation_style import (
    VISUAL_GENERATION_STYLE_DEFAULT,
    VisualGenerationStyleService,
    apply_visual_generation_style,
)
from kokoro_link.contracts.branching_drama import (
    BranchingDramaRepositoryPort,
)
from kokoro_link.contracts.cloud_action_billing import (
    ACTION_BRANCHING_DRAMA_ADVANCE,
    ACTION_BRANCHING_DRAMA_CREATE,
    ACTION_BRANCHING_DRAMA_INTERACT,
    ACTION_BRANCHING_DRAMA_SCENE_REGEN,
    client_quoted_price,
)
from kokoro_link.contracts.interaction_context import (
    confirm_deferred_deliveries,
    interaction_scope,
)
from kokoro_link.contracts.object_storage import (
    ObjectStorageError,
    ObjectStoragePort,
)
from kokoro_link.contracts.scene_image import (
    SceneImageError,
    SceneImagePort,
)
from kokoro_link.contracts.studio_jobs import (
    JOB_KIND_BRANCHING_CREATE,
    JOB_STATUS_FAILED as JOB_FAILED,
    JOB_STATUS_SUCCEEDED as JOB_SUCCEEDED,
    MAX_JOB_ATTEMPTS,
    StudioGenerationJob,
    StudioJobRepositoryPort,
)
from kokoro_link.domain.entities.branching_drama import (
    DEFAULT_OPERATOR_POSITION,
    IMAGE_PREFETCH_DEPTH,
    OUTLINE_PREFETCH_DEPTH,
    SESSION_ENDED,
    STATUS_FAILED,
    STATUS_GENERATING_IMAGES,
    STATUS_GENERATING_OUTLINES,
    STATUS_READY,
    BranchingDrama,
    DramaNode,
    DramaSession,
    _MAX_CHARACTERS,
    _MIN_CHARACTERS,
    normalise_drama_visual_style,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.visual_generation_style import (
    normalise_visual_generation_style,
)
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_visual_identity_lines,
)


_LOGGER = logging.getLogger(__name__)
_OUTLINE_CONCURRENCY = 5

_LOAD_FAILED = object()
"""Sentinel for "the drama could not be read", kept distinct from ``None``
("the drama is gone"): one must not move money, the other is a refund."""

_CHARGE_PARAM_KEYS = ("charge_id", "interaction_id", "action_key")
"""Job-param keys that let a restarted process re-attach the action charge."""

_T = TypeVar("_T")

_UNEXPECTED_KWARG_RE = re.compile(r"unexpected keyword argument '([^']+)'")

_OPTIONAL_COLLABORATOR_KWARGS = frozenset({
    "operator_primary_language",
    "operator_position",
    "operator_note",
})
"""Kwargs the planner / director / critic gained after their first ship.

Each is additive framing: the collaborator produces a usable result
without it. A stand-in wired by a test rig (or a self-host fork pinned to
an older module) may not accept them yet, so
:func:`_call_dropping_unsupported_kwargs` retries without the ones a
callee rejects instead of failing the whole drama over a prompt hint.
Anything a collaborator genuinely needs must NOT be listed here — being
listed is what makes an argument silently droppable.
"""


async def _call_dropping_unsupported_kwargs(
    fn: Callable[..., Awaitable[_T]],
    /,
    **kwargs: Any,
) -> _T:
    """Await ``fn(**kwargs)``, shedding optional kwargs it doesn't accept.

    Retries one name at a time, and only names in
    :data:`_OPTIONAL_COLLABORATOR_KWARGS` that are actually still in the
    call — so a collaborator that understands the language hint but not
    the position keeps the hint, and a ``TypeError`` raised from *inside*
    the collaborator propagates untouched.
    """
    attempt = dict(kwargs)
    while True:
        try:
            return await fn(**attempt)
        except TypeError as exc:
            match = _UNEXPECTED_KWARG_RE.search(str(exc))
            name = match.group(1) if match else ""
            if name not in _OPTIONAL_COLLABORATOR_KWARGS or name not in attempt:
                raise
            del attempt[name]

_LAZY_LAYER_POLL_INTERVAL_SECONDS = 0.5
_LAZY_LAYER_POLL_ATTEMPTS = 8
"""Bounded re-check for the *interactive* lazy-generation path.

When another replica holds the drama's lease we must neither generate a
duplicate layer nor block the player indefinitely: poll the repository a few
times over a couple of seconds (0.5s × 8 ≈ 4s, well inside a normal advance
round-trip) and surface an explicit in-progress signal if the owner's layer
still has not landed."""


class BranchingGenerationInProgress(Exception):
    """The next outline layer is being generated by another replica.

    Raised by :meth:`BranchingDramaService._ensure_children_exist` when the
    drama's cross-replica lease is held elsewhere and the owner's children did
    not land inside the bounded re-check window. Deliberately NOT a
    ``ValueError``: the gameplay routes map ``ValueError`` to a terminal 400,
    and an "empty children" verdict on this path would also end the player's
    session — both are wrong for a transient, retryable condition.
    """

    def __init__(self, drama_id: str, node_id: str) -> None:
        super().__init__(
            f"branching drama {drama_id}: the next layer under node {node_id} "
            "is being generated by another replica — retry shortly",
        )
        self.drama_id = drama_id
        self.node_id = node_id


class SceneImageUnavailable(Exception):
    """This deployment has no way to draw a scene at all (BD6).

    Self-host without ComfyUI and without the cloud renderer: there is no
    port to call, so a manual redraw cannot even be attempted. Kept apart
    from :class:`SceneRegenerationFailed` because the two are different
    answers — "this deployment cannot do this" (503) versus "the attempt
    was made and it broke" (502) — and because nothing is ever charged
    here: the check runs above the charge.
    """


class SceneRegenerationInProgress(Exception):
    """The same node is already being redrawn *in this process*.

    A redraw is slow and costs the player credits, so the second press of a
    double-click must not become a second charge. Deliberately fail-fast
    rather than "wait for the first one": the player gets an immediate,
    retryable answer and the first redraw's own response still carries the
    picture. Process-local by design — the cross-replica story is the
    versioned object key, which makes two concurrent redraws land on two
    different keys instead of corrupting one.
    """


class SceneRegenerationFailed(Exception):
    """The renderer or the object store refused a manual redraw (BD6).

    Never silent: the prefetch paths fail soft because nobody asked for
    those pictures, but a player who pressed 「重繪」 is owed an answer.
    The charge is released before this is raised — and the billing
    service's own rule settles it instead when the Gateway had already
    served a covered call, so a provider that was paid is not refunded.
    """


@dataclass(slots=True)
class _PipelineContext:
    drama_id: str
    prompt: str
    total_segments: int
    characters: list[Character]
    briefs: list[CharacterBrief]
    operator_primary_language: str = "zh-TW"
    operator_position: str = DEFAULT_OPERATOR_POSITION
    """BD2 framing the whole tree is planned against. Carried on the
    context rather than re-read per layer because a resumed pipeline and
    a lazily generated layer must plan under the same framing as the root
    did — a tree half-written for a protagonist and half for a spectator
    is worse than either."""
    operator_note: str | None = None
    visual_style: str | None = None
    """BD10 art direction every scene image of this tree is drawn in.
    Carried here for the same reason ``operator_position`` is: a lazily
    generated layer and a manual redraw must draw in the same look the
    root did. ``None`` is a pre-BD10 drama and routes
    :meth:`_build_scene_prompt` back to the legacy ``characters[0]``
    resolution — a fallback, not a default."""


class BranchingDramaService:
    def __init__(
        self,
        *,
        repository: BranchingDramaRepositoryPort,
        character_service: CharacterService,
        brief_builder: FusionCharacterBriefBuilder,
        planner: BranchingDramaPlanner,
        director: BranchingDramaDirector,
        critic: BranchingDramaCritic | None = None,
        polisher: BranchingDramaPolisher | None = None,
        uploads_dir: Path | None = None,
        scene_image: SceneImagePort | None = None,
        object_storage: ObjectStoragePort | None = None,
        event_seed_dispenser: "EventSeedDispenser | None" = None,
        visual_style_service: VisualGenerationStyleService | None = None,
        jobs: StudioJobRepositoryPort | None = None,
        execution_lease: StudioExecutionLease | None = None,
        lease_heartbeat_interval_seconds: float | None = None,
        lazy_layer_poll_interval_seconds: float = (
            _LAZY_LAYER_POLL_INTERVAL_SECONDS
        ),
        lazy_layer_poll_attempts: int = _LAZY_LAYER_POLL_ATTEMPTS,
        image_prefetch_depth: int = IMAGE_PREFETCH_DEPTH,
        action_billing: (
            CloudActionBillingService | NullActionBillingService | None
        ) = None,
        # NF4 — 分歧劇場 is a paid foreground way to play with these
        # characters, so every press has to move their foreground-interaction
        # anchor. Without it a player who lives in the drama surface reads as
        # "never interacted" to dormancy, idle down-shift and the freeze
        # reaper alike. Optional (``None`` → not tracked) so the many rigs
        # that construct this service without a character repository keep
        # working unchanged.
        activity_anchor: CharacterActivityAnchor | None = None,
    ) -> None:
        self._repo = repository
        self._character_service = character_service
        self._brief_builder = brief_builder
        self._planner = planner
        self._director = director
        # critic + polisher are optional so test rigs and fake-LLM
        # boots without the review pipeline still behave; production
        # container wires both in.
        self._critic = critic
        self._polisher = polisher
        _ = uploads_dir
        self._scene_image = scene_image
        self._object_storage = object_storage
        # How many tree layers get a picture drawn ahead of the player
        # (BD1). Deployments that pay per image — hosted — dial this down
        # so the prefetch stops drawing branches nobody walks into; a
        # ``0`` turns scene art off entirely without unwiring the port.
        # The default is the historical constant, so self-host is unchanged.
        self._image_prefetch_depth = max(0, int(image_prefetch_depth))
        self._event_seed_dispenser = event_seed_dispenser
        self._visual_style_service = visual_style_service
        # Durable job ledger (C0). ``None`` keeps the pre-C0 fire-and-
        # forget behaviour for rigs that don't care about restarts.
        self._jobs = jobs
        # Per-target cross-replica execution lease (Phase 4 前置). ``None`` →
        # the historical lock-only path (self-host / lease-less rigs).
        self._execution_lease = execution_lease
        self._lease_heartbeat_interval = lease_heartbeat_interval_seconds
        self._lazy_poll_interval = max(0.0, lazy_layer_poll_interval_seconds)
        self._lazy_poll_attempts = max(1, lazy_layer_poll_attempts)
        # BD3: one press, one price on a hosted ``action_fixed`` tier.
        # ``None`` (self-host, every token-billed tier, every test rig that
        # does not care) resolves to the null object, so the three entry
        # points below stay branch-free and byte-identical to the pre-BD3
        # behaviour.
        self._action_billing: (
            CloudActionBillingService | NullActionBillingService
        ) = action_billing or NullActionBillingService()
        self._activity_anchor = activity_anchor
        self._tasks: dict[int, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Nodes this process is currently redrawing by hand (BD6). Not a
        # lock: a second press must be *told* it is a duplicate, not queued
        # behind a redraw it would then pay for twice.
        self._scene_regen_in_flight: set[str] = set()

    # ── read surface ──────────────────────────────────────────────

    async def get(self, drama_id: str) -> BranchingDrama | None:
        return await self._repo.get(drama_id)

    async def list_recent(
        self, *, limit: int = 50,
    ) -> list[BranchingDrama]:
        return await self._repo.list_recent(limit=limit)

    async def get_node(self, node_id: str) -> DramaNode | None:
        return await self._repo.get_node(node_id)

    async def get_children(
        self, parent_node_id: str,
    ) -> list[DramaNode]:
        return await self._repo.get_children(parent_node_id)

    async def get_root_node(self, drama_id: str) -> DramaNode | None:
        return await self._repo.get_root_node(drama_id)

    async def count_nodes(self, drama_id: str) -> int:
        return await self._repo.count_nodes(drama_id)

    async def scene_gallery(
        self, drama: BranchingDrama,
    ) -> DramaSceneGallery:
        """劇場圖集 (BD9) — walked pictures in full, the rest as a count.

        Takes the already-loaded drama rather than an id because every
        caller has just fetched it to prove ownership, and re-reading it
        here would re-open the aggregate for nothing. The assembly itself
        lives in :mod:`branching_drama_gallery`, which owns the anti-spoiler
        boundary and is unit-testable without this service's whole rig.
        """
        return await build_scene_gallery(self._repo, drama)

    async def delete(self, drama_id: str) -> None:
        await self._repo.delete(drama_id)
        self._locks.pop(drama_id, None)

    # ── create ────────────────────────────────────────────────────

    async def create(
        self,
        *,
        character_ids: Sequence[str],
        prompt: str,
        total_segments: int = 6,
        operator_position: str | None = None,
        operator_note: str | None = None,
        visual_style: str | None = None,
        operator_primary_language: str = "zh-TW",
        user_id: str | None = None,
    ) -> BranchingDrama:
        """Persist the pending row and kick off the creation pipeline.

        The ``branching_drama_create`` charge is raised before anything is
        written, so a refusal (402 out of credits, 409 moved price) reaches
        the caller ahead of the route's ``202`` instead of surfacing minutes
        later as a job that quietly turned ``failed``.
        """
        characters = await self._resolve_characters(character_ids)
        # Operator left the prompt blank → try to seed direction from
        # the first eligible character's curated event inbox. Keeps
        # drama generation deterministic when the operator *did* write
        # a brief, but offers an LLM-grounded fallback when they didn't.
        #
        # Above the charge on purpose: the seed dispenser is a background
        # feature key, and a drama that cannot resolve its cast or its seed
        # never starts — charging then refunding it would print a spend and
        # a refund for a press that produced nothing.
        prompt = await self._maybe_apply_event_seed(
            prompt=prompt, characters=characters,
        )
        # BD10: a client that stated no look gets the one it would have
        # been drawn in anyway — resolved *here*, once, and written to the
        # row. Above the charge with the seed and the cast for the same
        # reason: it is a read, and a create that cannot get this far never
        # starts, so it must not print a spend/refund pair.
        resolved_visual_style = await self._resolve_visual_style(
            visual_style, characters=characters, user_id=user_id,
        )
        charge = await self._begin_action_charge(
            ACTION_BRANCHING_DRAMA_CREATE, user_id=user_id,
        )
        try:
            drama = BranchingDrama.create_pending(
                character_ids=[c.id for c in characters],
                prompt=prompt,
                total_segments=total_segments,
                operator_position=operator_position,
                operator_note=operator_note,
                visual_style=resolved_visual_style,
            )
            await self._repo.add(drama)

            ctx = _PipelineContext(
                drama_id=drama.id,
                prompt=drama.prompt,
                total_segments=drama.total_segments,
                characters=characters,
                briefs=self._brief_builder.build_persona_only_many(characters),
                operator_primary_language=operator_primary_language,
                # Read back off the entity, not the argument: the entity is
                # what normalised them, and it is what a resumed pipeline will
                # later read out of the row.
                operator_position=drama.operator_position,
                operator_note=drama.operator_note,
                visual_style=drama.visual_style,
            )
            await self._track_and_spawn(
                target_id=drama.id,
                params={
                    "operator_primary_language": operator_primary_language,
                },
                runner=self._run_generation_pipeline(ctx),
                charge=charge,
            )
        except BaseException:
            # Nothing was generated, so nothing was covered — a full refund.
            await self._action_billing.release(charge)
            raise
        await self._mark_cast_engaged(characters)
        return drama

    async def _resolve_visual_style(
        self,
        requested: str | None,
        *,
        characters: list[Character],
        user_id: str | None,
    ) -> str:
        """The look this new drama will be drawn in — always an answer (BD10).

        A stated look wins outright. When nothing was stated this reproduces
        the *old* implicit rule exactly — the first character's style, with
        the owner's preference and then the product default behind it — and
        the caller stores the result. That is the whole shape of the change:
        the resolution that used to happen invisibly at every image, off
        whatever character happened to be first *at that moment*, now
        happens once, at creation, and becomes a fact on the row.

        Never returns ``None``. ``None`` on the entity means "written before
        this column existed", and a drama created today has no business
        claiming that.
        """
        stated = normalise_drama_visual_style(requested)
        if stated is not None:
            return stated
        first = characters[0] if characters else None
        if first is None:
            return VISUAL_GENERATION_STYLE_DEFAULT
        if self._visual_style_service is None:
            # No preference store wired (self-host rigs, tests): the
            # character's own override or the product default, which is
            # what ``styled_prompt`` would have produced here anyway.
            return normalise_visual_generation_style(
                first.visual_generation_style,
            )
        return await self._visual_style_service.get_style_for_character(
            first, user_id=user_id,
        )

    async def _mark_cast_engaged(self, characters: Sequence[Character]) -> None:
        """NF4: this press was a foreground interaction with the whole cast.

        Called after the work landed — a press that raised delivered nothing
        and was refunded, so it is not evidence of anything. Fail-soft inside
        the anchor, so bookkeeping can never turn a delivered beat into an
        error the player sees (and a refund for work they received)."""
        if self._activity_anchor is None:
            return
        await self._activity_anchor.touch_all(characters)

    async def _begin_action_charge(
        self, action_key: str, *, user_id: str | None,
    ) -> ActionChargeHandle | None:
        """Charge one drama action before any of its work is scheduled.

        ``None`` means "not billed here" — self-host, a token-billed tier, a
        wallet outage — and the caller proceeds unchanged.
        """
        return await self._action_billing.begin(
            action_key,
            operator_id=(user_id or ""),
            # Bind to the number this player's own screen quoted; the route
            # puts it in scope from the request body.
            quoted_price_cr=client_quoted_price(action_key),
        )

    # ── session management ────────────────────────────────────────

    async def start_session(
        self,
        drama_id: str,
        *,
        operator_primary_language: str = "zh-TW",
        user_id: str | None = None,
    ) -> tuple[DramaSession, DramaNode, str]:
        """Start a new playthrough. Returns (session, root_node, narration).

        Charged as one ``branching_drama_advance`` (FX1): 開場 is the zeroth
        press and its cost has an advance's exact shape — one narration, its
        critic/polish pass, and the prefetch spawned behind it. Leaving it
        free was not a discount but a hole: BD7's 「換條路再走」 makes opening a
        session repeatable, so every press ran up to three planner calls and
        nine pictures billed *per call* by the Gateway with no covering
        charge at all.

        Two boundaries, both inherited from :meth:`advance_session`:

        * **every refusal this service can answer alone stays above the
          charge** — a drama that is not ready, a tree with no root, a cast
          that cannot be resolved — so a press that never ran leaves no
          spend/refund pair on the player's ledger.
        * **the settle belongs to the prefetch task, not to this request.**
          The layers and pictures drawn ahead of the player are part of what
          the opening bought, and closing the charge when the narration
          returns would leave every one of those calls uncovered and billed
          on top of the fixed price.
        """
        drama = await self._require_ready(drama_id)
        root = await self._repo.get_root_node(drama_id)
        if root is None:
            raise ValueError(f"drama {drama_id} has no root node")

        characters = await self._resolve_characters(drama.character_ids)
        briefs = self._brief_builder.build_persona_only_many(characters)

        charge = await self._begin_action_charge(
            ACTION_BRANCHING_DRAMA_ADVANCE, user_id=user_id,
        )
        try:
            # Entered even without a charge — as an explicit *clear*, so an
            # uncharged opening cannot inherit somebody else's reservation
            # and have its calls waived against it.
            with interaction_scope(charge.context if charge else None):
                narration = await self._narrate_with_language(
                    node=root, briefs=briefs, previous_turns=(),
                    operator_primary_language=operator_primary_language,
                    operator_position=drama.operator_position,
                    operator_note=drama.operator_note,
                )
                narration = await self._review_and_polish(
                    node=root, narration_text=narration, briefs=briefs,
                    previous_turns=(),
                    operator_primary_language=operator_primary_language,
                    operator_position=drama.operator_position,
                    operator_note=drama.operator_note,
                )
                session = DramaSession.start(
                    drama_id=drama_id, root_node_id=root.id,
                )
                session = session.with_turn(
                    node_id=root.id, narration=narration,
                )
                await self._repo.add_session(session)
        except BaseException:
            # The opening never landed — nothing the player can read, so a
            # whole refund (the billing service still settles instead when the
            # Gateway had already served a covered call).
            await self._action_billing.release(charge)
            raise

        # Hands the open charge to the prefetch, which settles it — exactly as
        # an advance does.
        self._maybe_prefetch_ahead(
            root, drama,
            operator_primary_language=operator_primary_language,
            charge=charge,
        )
        await self._mark_cast_engaged(characters)
        return session, root, narration

    async def interact_session(
        self,
        session_id: str,
        *,
        player_input: str,
        operator_primary_language: str = "zh-TW",
        user_id: str | None = None,
    ) -> tuple[DramaSession, str, str | None]:
        """Interact within the current beat.

        Returns ``(session, response, advance_hint)``.
        ``advance_hint`` is a short phrase for the advance button when
        the LLM judges the beat is sufficiently explored, else ``None``.

        One ``branching_drama_interact`` charge per exchange (BD plan D2):
        an in-beat loop is a real LLM call every time, so a free one would be
        chat with the meter switched off. Every refusal this service can
        answer on its own — no session, an ended session, a missing drama or
        node — happens above the charge, so a press that never ran leaves no
        spend/refund pair on the player's ledger.
        """
        session = await self._repo.get_session(session_id)
        if session is None:
            raise ValueError(f"session {session_id} not found")
        if session.is_ended:
            raise ValueError("session already ended")

        drama = await self._repo.get(session.drama_id)
        if drama is None:
            raise ValueError("drama not found")

        current_node = await self._repo.get_node(session.current_node_id)
        if current_node is None:
            raise ValueError("current node not found")

        characters = await self._resolve_characters(drama.character_ids)
        briefs = self._brief_builder.build_persona_only_many(characters)

        last_turn = session.turns[-1] if session.turns else None
        existing_exchanges = last_turn.exchanges if last_turn else ()

        async with self._action_billing.action(
            ACTION_BRANCHING_DRAMA_INTERACT,
            operator_id=(user_id or ""),
            quoted_price_cr=client_quoted_price(
                ACTION_BRANCHING_DRAMA_INTERACT,
            ),
        ):
            response, advance_hint = await self._respond_with_language(
                node=current_node,
                briefs=briefs,
                previous_turns=session.turns[:-1] if session.turns else (),
                exchanges=existing_exchanges,
                player_input=player_input,
                operator_primary_language=operator_primary_language,
                operator_position=drama.operator_position,
                operator_note=drama.operator_note,
            )

            session = session.with_exchange(
                player_input=player_input, response=response,
            )
            await self._repo.save_session(session)
        await self._mark_cast_engaged(characters)
        return session, response, advance_hint

    async def advance_session(
        self,
        session_id: str,
        *,
        operator_primary_language: str = "zh-TW",
        user_id: str | None = None,
    ) -> tuple[DramaSession, DramaNode, str, bool]:
        """Advance to the next beat based on accumulated exchanges.

        Returns ``(session, next_node, narration, is_ending)``.

        One ``branching_drama_advance`` charge per press (BD plan D2), raised
        around everything the press has to deliver: the lazily planned next
        layer, the tone classification, the narration and its polish pass.
        Two boundaries carry the plan's promises:

        * **the 409 refunds.** ``BranchingGenerationInProgress`` means another
          replica owns the next layer and this player got no beat at all, so
          the charge is released in full rather than billed for a retry they
          have to make themselves. So does "no children" — a session that
          walks into an ending delivered nothing new either.
        * **the settle is handed to the prefetch.** The layers and pictures
          drawn ahead of the player are part of what this press bought (D3),
          but they are spawned rather than awaited — so closing the charge at
          the end of the request would leave every one of those calls
          uncovered and billed per call on top of the fixed price. The handle
          travels into :meth:`_maybe_prefetch_ahead`, which runs the prefetch
          inside its scope and settles afterwards. A process that dies in
          between leaves the reservation open on purpose: the User-side
          sweeper is the only party that can still see what was served.
        """
        session = await self._repo.get_session(session_id)
        if session is None:
            raise ValueError(f"session {session_id} not found")
        if session.is_ended:
            raise ValueError("session already ended")

        drama = await self._repo.get(session.drama_id)
        if drama is None:
            raise ValueError("drama not found")

        current_node = await self._repo.get_node(session.current_node_id)
        if current_node is None:
            raise ValueError("current node not found")

        charge = await self._begin_action_charge(
            ACTION_BRANCHING_DRAMA_ADVANCE, user_id=user_id,
        )
        try:
            # The scope is entered even without a charge — as an explicit
            # *clear*, so an uncharged advance cannot inherit somebody else's
            # reservation and have its calls waived against it.
            with interaction_scope(charge.context if charge else None):
                children_list = await self._ensure_children_exist(
                    current_node, drama,
                    operator_primary_language=operator_primary_language,
                )
                if not children_list:
                    session = session.end()
                    await self._repo.save_session(session)
                    raise ValueError("no children — already at ending")

                children_by_tone = {
                    c.tone: c for c in children_list if c.tone
                }

                last_turn = session.turns[-1] if session.turns else None
                exchanges = last_turn.exchanges if last_turn else ()

                tone = await self._director.classify_tone(
                    exchanges=exchanges,
                    children=children_by_tone,
                )
                next_node = children_by_tone.get(tone)
                if next_node is None:
                    next_node = children_list[0]
                    tone = next_node.tone or tone

                characters = await self._resolve_characters(
                    drama.character_ids,
                )
                briefs = self._brief_builder.build_persona_only_many(
                    characters,
                )

                narration = await self._narrate_with_language(
                    node=next_node,
                    briefs=briefs,
                    previous_turns=session.turns,
                    operator_primary_language=operator_primary_language,
                    operator_position=drama.operator_position,
                    operator_note=drama.operator_note,
                )
                narration = await self._review_and_polish(
                    node=next_node, narration_text=narration, briefs=briefs,
                    previous_turns=session.turns,
                    operator_primary_language=operator_primary_language,
                    operator_position=drama.operator_position,
                    operator_note=drama.operator_note,
                )

                is_ending = next_node.depth >= drama.total_segments - 1
                session = session.with_turn(
                    node_id=next_node.id,
                    narration=narration,
                    chosen_tone=tone,
                )
                await self._repo.save_session(session)
        except BaseException:
            # The beat never landed — the 409, an ended branch, a collaborator
            # that raised. Nothing the player can read, so a whole refund.
            await self._action_billing.release(charge)
            raise

        # Hands the open charge to the prefetch, which settles it (see the
        # docstring): the layers drawn ahead of the player are part of what
        # this press bought.
        self._maybe_prefetch_ahead(
            next_node, drama,
            operator_primary_language=operator_primary_language,
            charge=charge,
        )
        await self._mark_cast_engaged(characters)
        return session, next_node, narration, is_ending

    async def end_session(self, session_id: str) -> DramaSession:
        """Explicitly end a session (used after the final beat)."""
        session = await self._repo.get_session(session_id)
        if session is None:
            raise ValueError(f"session {session_id} not found")
        if session.is_ended:
            raise ValueError("session already ended")
        session = session.end()
        await self._repo.save_session(session)
        return session

    async def get_session(
        self, session_id: str,
    ) -> DramaSession | None:
        return await self._repo.get_session(session_id)

    async def list_sessions(
        self, drama_id: str,
    ) -> list[DramaSession]:
        return await self._repo.list_sessions(drama_id)

    # ── narration review pass ─────────────────────────────────────

    async def _review_and_polish(
        self,
        *,
        node: DramaNode,
        narration_text: str,
        briefs: Sequence[CharacterBrief],
        previous_turns: Sequence,
        operator_primary_language: str = "zh-TW",
        operator_position: str = DEFAULT_OPERATOR_POSITION,
        operator_note: str | None = None,
    ) -> str:
        """Single-round critic→polish on a freshly narrated turn.

        No-op when critic / polisher aren't wired (test rigs). Failure
        in either pass is swallowed and the original draft returns —
        gameplay path must never break because of a review hiccup.
        Mirrors fusion's review loop but with one round only because
        per-turn latency matters in gameplay.
        """
        narration_text = (narration_text or "").strip()
        if not narration_text:
            return narration_text
        if self._critic is None or self._polisher is None:
            return narration_text
        try:
            critique = await _call_dropping_unsupported_kwargs(
                self._critic.review,
                node=node,
                narration_text=narration_text,
                briefs=briefs,
                previous_turns=previous_turns,
                operator_position=operator_position,
                operator_note=operator_note,
            )
        except Exception:
            _LOGGER.exception(
                "drama: critic crashed node=%s — keeping draft", node.id,
            )
            return narration_text
        if not critique.has_issues():
            return narration_text
        try:
            polished = await self._polish_with_language(
                node=node,
                narration_text=narration_text,
                critique=critique,
                briefs=briefs,
                previous_turns=previous_turns,
                operator_primary_language=operator_primary_language,
            )
        except Exception:
            _LOGGER.exception(
                "drama: polisher crashed node=%s — keeping draft", node.id,
            )
            return narration_text
        return polished or narration_text

    async def _plan_root_with_language(
        self,
        *,
        prompt: str,
        briefs: Sequence[CharacterBrief],
        total_segments: int,
        operator_primary_language: str,
        operator_position: str = DEFAULT_OPERATOR_POSITION,
        operator_note: str | None = None,
    ) -> tuple[str, NodeOutline]:
        return await _call_dropping_unsupported_kwargs(
            self._planner.plan_root,
            prompt=prompt,
            briefs=briefs,
            total_segments=total_segments,
            operator_primary_language=operator_primary_language,
            operator_position=operator_position,
            operator_note=operator_note,
        )

    async def _plan_children_with_language(
        self,
        *,
        prompt: str,
        briefs: Sequence[CharacterBrief],
        parent_summary: str,
        path_context: str,
        depth: int,
        total_segments: int,
        operator_primary_language: str,
        operator_position: str = DEFAULT_OPERATOR_POSITION,
        operator_note: str | None = None,
    ) -> dict[str, NodeOutline]:
        return await _call_dropping_unsupported_kwargs(
            self._planner.plan_children,
            prompt=prompt,
            briefs=briefs,
            parent_summary=parent_summary,
            path_context=path_context,
            depth=depth,
            total_segments=total_segments,
            operator_primary_language=operator_primary_language,
            operator_position=operator_position,
            operator_note=operator_note,
        )

    async def _narrate_with_language(
        self,
        *,
        node: DramaNode,
        briefs: Sequence[CharacterBrief],
        previous_turns: Sequence,
        operator_primary_language: str,
        operator_position: str = DEFAULT_OPERATOR_POSITION,
        operator_note: str | None = None,
    ) -> str:
        return await _call_dropping_unsupported_kwargs(
            self._director.narrate,
            node=node,
            briefs=briefs,
            previous_turns=previous_turns,
            operator_primary_language=operator_primary_language,
            operator_position=operator_position,
            operator_note=operator_note,
        )

    async def _respond_with_language(
        self,
        *,
        node: DramaNode,
        briefs: Sequence[CharacterBrief],
        previous_turns: Sequence,
        exchanges: Sequence,
        player_input: str,
        operator_primary_language: str,
        operator_position: str = DEFAULT_OPERATOR_POSITION,
        operator_note: str | None = None,
    ) -> tuple[str, str | None]:
        return await _call_dropping_unsupported_kwargs(
            self._director.respond_in_scene,
            node=node,
            briefs=briefs,
            previous_turns=previous_turns,
            exchanges=exchanges,
            player_input=player_input,
            operator_primary_language=operator_primary_language,
            operator_position=operator_position,
            operator_note=operator_note,
        )

    async def _polish_with_language(
        self,
        *,
        node: DramaNode,
        narration_text: str,
        critique,
        briefs: Sequence[CharacterBrief],
        previous_turns: Sequence,
        operator_primary_language: str,
    ) -> str:
        # No position kwarg here on purpose: the polisher rewrites against
        # the critique, and the critique was already produced under the
        # drama's framing (see ``_review_and_polish``).
        if self._polisher is None:
            return narration_text
        return await _call_dropping_unsupported_kwargs(
            self._polisher.polish,
            node=node,
            narration_text=narration_text,
            critique=critique,
            briefs=briefs,
            previous_turns=previous_turns,
            operator_primary_language=operator_primary_language,
        )

    # ── generation pipeline ───────────────────────────────────────

    async def _run_generation_pipeline(
        self, ctx: _PipelineContext,
    ) -> str | None:
        async with self._lock_for(ctx.drama_id):
            async with self._lease_session(ctx.drama_id) as lease:
                if not lease.acquired:
                    self._log_lease_skip(ctx.drama_id)
                    return LEASE_ABANDONED
                try:
                    await self._generate_tree(ctx, lease=lease)
                    lease.raise_if_lost()
                    await self._generate_initial_images(ctx)
                    drama = await self._must_load(ctx.drama_id)
                    await self._repo.save(
                        drama.with_status(STATUS_READY),
                    )
                except StudioLeaseLost:
                    self._log_lease_lost(ctx.drama_id)
                    return LEASE_ABANDONED
                except _PipelineAbort as abort:
                    _LOGGER.warning(
                        "branching drama pipeline aborted id=%s reason=%s",
                        ctx.drama_id, abort.reason,
                    )
                    await self._mark_failed(
                        ctx.drama_id,
                        abort.reason,
                        error_code=failure_error_code(abort),
                    )
                except Exception as exc:
                    _LOGGER.exception(
                        "branching drama pipeline crashed id=%s", ctx.drama_id,
                    )
                    await self._mark_failed(
                        ctx.drama_id,
                        "pipeline crashed",
                        error_code=failure_error_code(exc),
                    )

    async def _resume_generation_pipeline(
        self, ctx: _PipelineContext,
    ) -> str | None:
        """Re-drive a creation pipeline interrupted by a restart.

        Persisted nodes are the checkpoint: an existing root is reused
        (``_generate_tree`` would create a duplicate), missing child
        layers are regenerated per-parent, and the image stage already
        skips nodes that have an ``image_path``.
        """
        async with self._lock_for(ctx.drama_id):
            async with self._lease_session(ctx.drama_id) as lease:
                if not lease.acquired:
                    self._log_lease_skip(ctx.drama_id)
                    return LEASE_ABANDONED
                try:
                    root = await self._repo.get_root_node(ctx.drama_id)
                    if root is None:
                        await self._generate_tree(ctx, lease=lease)
                    else:
                        await self._fill_missing_layers(ctx, root, lease=lease)
                    lease.raise_if_lost()
                    await self._generate_initial_images(ctx)
                    drama = await self._must_load(ctx.drama_id)
                    await self._repo.save(
                        drama.with_status(STATUS_READY),
                    )
                except StudioLeaseLost:
                    self._log_lease_lost(ctx.drama_id)
                    return LEASE_ABANDONED
                except _PipelineAbort as abort:
                    _LOGGER.warning(
                        "branching drama resume aborted id=%s reason=%s",
                        ctx.drama_id, abort.reason,
                    )
                    await self._mark_failed(
                        ctx.drama_id,
                        abort.reason,
                        error_code=failure_error_code(abort),
                    )
                except Exception as exc:
                    _LOGGER.exception(
                        "branching drama resume crashed id=%s", ctx.drama_id,
                    )
                    await self._mark_failed(
                        ctx.drama_id,
                        "pipeline crashed",
                        error_code=failure_error_code(exc),
                    )

    async def _fill_missing_layers(
        self,
        ctx: _PipelineContext,
        root: DramaNode,
        *,
        lease: StudioLeaseSession | None = None,
    ) -> None:
        """Generate only the initial layers that never landed."""
        initial_depth = min(OUTLINE_PREFETCH_DEPTH, ctx.total_segments)
        current_layer = [root]
        for depth in range(1, initial_depth):
            # Bounded cross-replica abort between outline layers.
            if lease is not None:
                lease.raise_if_lost()
            next_layer: list[DramaNode] = []
            missing_parents: list[DramaNode] = []
            for parent in current_layer:
                children = await self._repo.get_children(parent.id)
                if children:
                    next_layer.extend(children)
                else:
                    missing_parents.append(parent)
            if missing_parents:
                next_layer.extend(await self._generate_layer(
                    ctx, parent_nodes=missing_parents, depth=depth, lease=lease,
                ))
            current_layer = next_layer

    async def _generate_tree(
        self,
        ctx: _PipelineContext,
        *,
        lease: StudioLeaseSession | None = None,
    ) -> None:
        drama_title, root_outline = await self._plan_root_with_language(
            prompt=ctx.prompt,
            briefs=ctx.briefs,
            total_segments=ctx.total_segments,
            operator_primary_language=ctx.operator_primary_language,
            operator_position=ctx.operator_position,
            operator_note=ctx.operator_note,
        )
        # Lease re-verify after the root-plan await, before persisting the title /
        # root node — a stolen lease must not clobber the reclaiming owner's tree.
        if lease is not None:
            lease.raise_if_lost()
        drama = await self._must_load(ctx.drama_id)
        await self._repo.save(drama.with_title(drama_title))

        root = DramaNode.create_root(
            drama_id=ctx.drama_id,
            title=root_outline.title,
            summary=root_outline.summary,
            appearing_character_ids=root_outline.appearing_character_ids,
        )
        await self._repo.add_nodes([root])

        initial_depth = min(OUTLINE_PREFETCH_DEPTH, ctx.total_segments)
        current_layer = [root]
        for depth in range(1, initial_depth):
            # Bounded cross-replica abort between outline layers.
            if lease is not None:
                lease.raise_if_lost()
            next_layer = await self._generate_layer(
                ctx, parent_nodes=current_layer, depth=depth, lease=lease,
            )
            current_layer = next_layer

    async def _generate_layer(
        self,
        ctx: _PipelineContext,
        *,
        parent_nodes: list[DramaNode],
        depth: int,
        lease: StudioLeaseSession | None = None,
    ) -> list[DramaNode]:
        semaphore = asyncio.Semaphore(_OUTLINE_CONCURRENCY)
        results: list[list[DramaNode]] = [[] for _ in parent_nodes]

        async def generate_for_parent(
            idx: int, parent: DramaNode,
        ) -> None:
            async with semaphore:
                path_context = await self._build_path_context(parent)
                children_outlines = await self._plan_children_with_language(
                    prompt=ctx.prompt,
                    briefs=ctx.briefs,
                    parent_summary=parent.summary,
                    path_context=path_context,
                    depth=depth,
                    total_segments=ctx.total_segments,
                    operator_primary_language=ctx.operator_primary_language,
                    operator_position=ctx.operator_position,
                    operator_note=ctx.operator_note,
                )
                nodes: list[DramaNode] = []
                for tone, outline in children_outlines.items():
                    node = DramaNode.create_child(
                        drama_id=ctx.drama_id,
                        parent_node_id=parent.id,
                        depth=depth,
                        tone=tone,
                        title=outline.title,
                        summary=outline.summary,
                        appearing_character_ids=outline.appearing_character_ids,
                    )
                    nodes.append(node)
                results[idx] = nodes

        tasks = [
            generate_for_parent(i, p)
            for i, p in enumerate(parent_nodes)
        ]
        await asyncio.gather(*tasks)

        # Lease re-verify after the layer's provider awaits, before persisting the
        # generated nodes — a stolen lease must not add nodes onto the reclaiming
        # owner's tree.
        if lease is not None:
            lease.raise_if_lost()
        all_nodes = [n for group in results for n in group]
        if all_nodes:
            await self._repo.add_nodes(all_nodes)
        return all_nodes

    async def _build_path_context(self, node: DramaNode) -> str:
        """Walk up from node to root, building a summary of the path."""
        parts: list[str] = []
        current: DramaNode | None = node
        while current is not None:
            tone_label = (
                f"[{current.tone}] " if current.tone else "[開場] "
            )
            parts.append(f"{tone_label}{current.title}：{current.summary}")
            if current.parent_node_id:
                current = await self._repo.get_node(current.parent_node_id)
            else:
                break
        parts.reverse()
        return "\n".join(parts)

    async def _make_context(
        self,
        drama: BranchingDrama,
        *,
        operator_primary_language: str = "zh-TW",
    ) -> _PipelineContext:
        characters = await self._resolve_characters(drama.character_ids)
        return _PipelineContext(
            drama_id=drama.id,
            prompt=drama.prompt,
            total_segments=drama.total_segments,
            characters=characters,
            briefs=self._brief_builder.build_persona_only_many(characters),
            operator_primary_language=operator_primary_language,
            operator_position=drama.operator_position,
            operator_note=drama.operator_note,
            visual_style=drama.visual_style,
        )

    async def _ensure_children_exist(
        self,
        node: DramaNode,
        drama: BranchingDrama,
        *,
        operator_primary_language: str = "zh-TW",
    ) -> list[DramaNode]:
        """Return children of *node*, generating them lazily if needed.

        This is the interactive path — a player is waiting on it — so it is
        guarded by the same two-layer mutex the creation pipelines use: the
        process-local lock keeps same-replica callers in line, and the
        per-drama cross-replica lease keeps a *second* replica from
        generating a duplicate layer under the same parent.

        Losing the lease race never means "generate anyway" and never means
        "block until the owner finishes": we re-check the repository over a
        short bounded window and, if the owner's layer has not landed, raise
        :class:`BranchingGenerationInProgress` for the caller to retry.
        """
        existing = await self._repo.get_children(node.id)
        if existing:
            return existing
        if node.depth >= drama.total_segments - 1:
            return []
        async with self._lock_for(drama.id):
            existing = await self._repo.get_children(node.id)
            if existing:
                return existing
            async with self._lease_session(drama.id) as lease:
                if not lease.acquired:
                    self._log_lease_skip(drama.id)
                    return await self._await_foreign_layer(node, drama)
                # Third check under the lease: the replica that just released
                # it may have generated exactly this layer between our read
                # above and our claim.
                existing = await self._repo.get_children(node.id)
                if existing:
                    return existing
                ctx = await self._make_context(
                    drama,
                    operator_primary_language=operator_primary_language,
                )
                try:
                    return await self._generate_layer(
                        ctx,
                        parent_nodes=[node],
                        depth=node.depth + 1,
                        lease=lease,
                    )
                except StudioLeaseLost:
                    # Our lease was stolen mid-generation — the reclaiming
                    # replica owns this tree now and ``_generate_layer``
                    # abandoned before persisting. Never race its writes.
                    self._log_lease_lost(drama.id)
                    return await self._await_foreign_layer(node, drama)

    async def _await_foreign_layer(
        self, node: DramaNode, drama: BranchingDrama,
    ) -> list[DramaNode]:
        """Bounded re-check for a layer another replica is generating.

        Returns the owner's children as soon as they land; raises
        :class:`BranchingGenerationInProgress` when the window elapses. Never
        generates — a duplicate layer is exactly the bug this guards.
        """
        for _ in range(self._lazy_poll_attempts):
            if self._lazy_poll_interval > 0:
                await asyncio.sleep(self._lazy_poll_interval)
            landed = await self._repo.get_children(node.id)
            if landed:
                return landed
        _LOGGER.info(
            "branching drama: next layer still generating elsewhere "
            "drama=%s node=%s", drama.id, node.id,
        )
        raise BranchingGenerationInProgress(drama.id, node.id)

    # ── lazy prefetch (outlines + images) ────────────────────────

    def _maybe_prefetch_ahead(
        self,
        node: DramaNode,
        drama: BranchingDrama,
        *,
        operator_primary_language: str = "zh-TW",
        charge: ActionChargeHandle | None = None,
    ) -> None:
        """Queue outline + image generation for upcoming layers.

        ``charge`` (BD3) is the advance press that paid for this prefetch, and
        passing it here *transfers ownership of it*: the spawned task runs the
        prefetch inside the charge's interaction scope so the Gateway waives
        those calls, and settles once — however the prefetch ended. A failed
        prefetch still settles, because the beat the player pressed for was
        already delivered; refunding it would make "advance, then let the
        prefetch fail" a free beat.

        ``None`` is every uncharged caller (self-host, a token-billed tier, a
        wallet outage) and keeps the pre-BD3 fire-and-forget shape.
        """
        max_depth = min(
            node.depth + OUTLINE_PREFETCH_DEPTH,
            drama.total_segments - 1,
        )
        prefetchable = node.depth < max_depth
        if not prefetchable and charge is None:
            return

        async def prefetch() -> None:
            try:
                if not prefetchable:
                    return
                with interaction_scope(charge.context if charge else None):
                    await self._run_prefetch(
                        node, drama, max_depth,
                        operator_primary_language=operator_primary_language,
                    )
            finally:
                await self._action_billing.settle(charge)

        self._spawn(prefetch())

    async def _run_prefetch(
        self,
        node: DramaNode,
        drama: BranchingDrama,
        max_depth: int,
        *,
        operator_primary_language: str,
    ) -> None:
        """Outlines then pictures, each failing on its own without the other."""
        try:
            await self._prefetch_subtree_outlines(
                node, drama, max_depth,
                operator_primary_language=operator_primary_language,
            )
        except Exception:
            _LOGGER.exception(
                "outline prefetch failed node=%s", node.id,
            )
        # Images walk their own (configurable) depth ahead of the
        # player, which the default keeps equal to the outline depth.
        # A deployment that drew fewer layers than it outlined would
        # otherwise have no way to say so.
        image_max_depth = min(
            node.depth + self._image_prefetch_depth,
            drama.total_segments - 1,
        )
        if self._can_generate_images and node.depth < image_max_depth:
            try:
                await self._prefetch_subtree_images(
                    node, drama, image_max_depth,
                )
            except Exception:
                _LOGGER.exception(
                    "image prefetch failed node=%s", node.id,
                )

    async def _prefetch_subtree_outlines(
        self,
        node: DramaNode,
        drama: BranchingDrama,
        max_depth: int,
        *,
        operator_primary_language: str = "zh-TW",
    ) -> None:
        if node.depth >= max_depth:
            return
        try:
            children = await self._ensure_children_exist(
                node, drama,
                operator_primary_language=operator_primary_language,
            )
        except BranchingGenerationInProgress:
            # Speculative prefetch — another replica is already producing this
            # subtree. Nothing to do and nothing to warn about.
            return
        for child in children:
            if child.depth < max_depth:
                await self._prefetch_subtree_outlines(
                    child, drama, max_depth,
                    operator_primary_language=operator_primary_language,
                )

    async def _generate_initial_images(
        self, ctx: _PipelineContext,
    ) -> None:
        drama = await self._must_load(ctx.drama_id)
        await self._repo.save(
            drama.with_status(STATUS_GENERATING_IMAGES),
        )
        # Pre-generate images for the first ``image_prefetch_depth``
        # layers (default: depth 0 and 1 → 4 images). Actual image
        # generation depends on a scene-image backend being wired —
        # skip gracefully if not configured.
        if not self._can_generate_images:
            return
        for d in range(min(self._image_prefetch_depth, ctx.total_segments)):
            nodes = await self._repo.get_nodes_at_depth(ctx.drama_id, d)
            for node in nodes:
                if node.image_path or self._is_being_redrawn(node):
                    continue
                path = await self._generate_node_image(node, ctx)
                if path:
                    await self._store_prefetched_image(node, path)

    async def _generate_node_image(
        self,
        node: DramaNode,
        ctx: _PipelineContext,
    ) -> str | None:
        """Generate a VN-style scene image for a node — fail-soft.

        This is the *prefetch* path: nobody asked for these pictures, so a
        renderer that refuses leaves the node picture-less and the drama
        carries on. The manual redraw
        (:meth:`regenerate_node_image`) shares the drawing itself but not
        this policy — a player who pressed the button is owed an error.
        """
        if not self._can_generate_images:
            return None
        try:
            return await self._render_scene_image(
                node, ctx, object_key=_scene_object_key(node),
            )
        except SceneImageError:
            _LOGGER.warning(
                "scene generation failed for node=%s, skipping", node.id,
            )
            return None

    async def _render_scene_image(
        self,
        node: DramaNode,
        ctx: _PipelineContext,
        *,
        object_key: str,
    ) -> str:
        """Draw one scene and store it at ``object_key``; return its URL.

        Raises whatever the renderer or the store raises — every fail-soft
        policy lives in the caller, so the two callers can differ.

        The one thing both callers share is *when the picture became real*,
        and that is this method's own business: the hosted image adapter
        defers its covered-call receipt because bytes in memory are not yet
        what the player bought, so the object-storage write is the step that
        confirms it. Without that confirmation the enclosing action charge
        looks unconsumed and settles as a full refund — a hosted redraw that
        works perfectly and costs nothing (FX1). Confirming here rather than
        in either caller is what keeps the prefetch and the manual redraw on
        the same fact while they keep their different failure policies.
        """
        prompt = await self._build_scene_prompt(node, ctx)
        image_bytes = await self._scene_image.generate(
            positive=prompt,
            aspect="landscape",
            # Routing / attribution anchor only — the drawing itself
            # is described entirely by ``prompt``. Same character the
            # visual-style service styles against, so a hosted call is
            # billed and profile-routed consistently with it.
            character=ctx.characters[0] if ctx.characters else None,
        )
        stored = await self._object_storage.put_bytes(
            object_key=object_key,
            content=image_bytes,
            content_type="image/png",
            metadata={
                "drama_id": node.drama_id,
                "node_id": node.id,
                "kind": "branching_drama_scene",
            },
        )
        # The picture now exists where the player can see it, so the covered
        # Gateway call has finally delivered. A storage failure never reaches
        # here, which is what keeps it refundable.
        confirm_deferred_deliveries()
        return stored.url

    # ── manual redraw (BD6) ───────────────────────────────────────

    async def regenerate_node_image(
        self,
        node_id: str,
        *,
        drama_id: str,
        user_id: str | None = None,
    ) -> DramaNode:
        """Redraw one scene on the player's own press, and charge for it.

        The prefetch draws every picture fail-soft, which leaves two states
        no automatic retry ever repairs: a node whose renderer was down at
        creation time (no picture at all), and a node whose picture is
        simply not what the player wanted. Both are fixed by the same
        press, so this is one action — ``branching_drama_scene_regen`` —
        rather than a free repair and a paid re-roll.

        Three boundaries carry the promises:

        * **the port check is above the charge.** A deployment with no
          renderer cannot redraw, and refusing before the charge means no
          spend/refund pair for a press that never ran.
        * **the in-flight guard is above the charge too.** A double-click
          would otherwise be two reservations for one picture.
        * **the new picture takes a new object key.** Overwriting the old
          key would leave every browser, proxy and CDN showing the picture
          the player just rejected; a fresh key cannot be stale. The
          superseded object is deleted afterwards, best-effort — a leaked
          object is cheap, a lost current picture is not.
        """
        if not self._can_render_scene_image:
            raise SceneImageUnavailable(
                "this deployment has no scene renderer configured",
            )
        node = await self._repo.get_node(node_id)
        if node is None or node.drama_id != drama_id:
            raise ValueError(f"node {node_id} not found")
        drama = await self._repo.get(drama_id)
        if drama is None:
            raise ValueError(f"drama {drama_id} not found")
        if node_id in self._scene_regen_in_flight:
            raise SceneRegenerationInProgress(
                f"node {node_id} is already being redrawn — retry shortly",
            )
        self._scene_regen_in_flight.add(node_id)
        try:
            return await self._redraw_node_image(
                node, drama, user_id=user_id,
            )
        finally:
            self._scene_regen_in_flight.discard(node_id)

    async def _redraw_node_image(
        self,
        node: DramaNode,
        drama: BranchingDrama,
        *,
        user_id: str | None,
    ) -> DramaNode:
        """Charge, draw, store, save — and close the charge either way."""
        ctx = await self._make_context(drama)
        superseded = node.image_path
        object_key = _versioned_scene_object_key(node)
        charge = await self._begin_action_charge(
            ACTION_BRANCHING_DRAMA_SCENE_REGEN, user_id=user_id,
        )
        try:
            # Entered even without a charge — as an explicit *clear*, so an
            # unbilled redraw cannot inherit somebody else's reservation.
            with interaction_scope(charge.context if charge else None):
                url = await self._render_scene_image(
                    node, ctx, object_key=object_key,
                )
                redrawn = node.with_image_path(url)
                await self._repo.save_node(redrawn)
        except BaseException as exc:
            # ``release`` settles instead when the Gateway already served a
            # covered call: a picture that was drawn and then lost on the
            # way to storage was still paid for upstream.
            await self._action_billing.release(charge)
            if isinstance(exc, (SceneImageError, ObjectStorageError)):
                raise SceneRegenerationFailed(str(exc)) from exc
            raise
        await self._action_billing.settle(charge)
        await self._discard_superseded_scene_image(superseded, object_key)
        await self._mark_cast_engaged(ctx.characters)
        return redrawn

    async def _discard_superseded_scene_image(
        self, previous_path: str | None, current_key: str,
    ) -> None:
        """Best-effort delete of the picture this redraw replaced.

        Never raises and never runs before the new picture is saved: the
        node already points at the new key, so failing here leaks one
        object, while doing it earlier could delete the only picture the
        player has if the redraw then fails.
        """
        if not previous_path or self._object_storage is None:
            return
        try:
            previous_key = self._object_storage.object_key_from_url(
                previous_path,
            )
        except Exception:  # noqa: BLE001 — cleanup never fails a redraw
            _LOGGER.debug(
                "could not resolve the superseded scene key from %s",
                previous_path, exc_info=True,
            )
            return
        if not previous_key or previous_key == current_key:
            return
        try:
            await self._object_storage.delete(object_key=previous_key)
        except Exception:  # noqa: BLE001 — a leaked object is cheap
            _LOGGER.info(
                "could not delete the superseded scene image %s",
                previous_key, exc_info=True,
            )

    async def _build_scene_prompt(
        self, node: DramaNode, ctx: _PipelineContext,
    ) -> str:
        char_map = {c.id: c for c in ctx.characters}
        appearances: list[str] = []
        for cid in node.appearing_character_ids:
            char = char_map.get(cid)
            if char is None:
                continue
            if char.appearance:
                appearances.append(char.appearance)
            appearances.extend(render_character_visual_identity_lines(char))
        parts = [node.summary]
        if appearances:
            parts.append(", ".join(appearances))
        parts.append("visual novel scene, cinematic lighting")
        prompt = ", ".join(parts)
        if ctx.visual_style is not None:
            # BD10: the drama's own art direction, decided once at creation
            # and identical for every node, every lazily drawn layer and
            # every manual redraw. No per-character lookup: a mixed cast
            # used to draw its second character in its first one's look,
            # and reordering the picker repainted the whole production.
            return apply_visual_generation_style(prompt, ctx.visual_style)
        # Pre-BD10 drama (``visual_style IS NULL``) — reproduce exactly what
        # it has always been drawn in, so an old tree does not change look
        # halfway through because the feature landed.
        character = ctx.characters[0] if ctx.characters else None
        if self._visual_style_service is None:
            return apply_visual_generation_style(
                prompt, VISUAL_GENERATION_STYLE_DEFAULT,
            )
        return await self._visual_style_service.styled_prompt(
            prompt, character=character,
        )

    async def _prefetch_subtree_images(
        self,
        node: DramaNode,
        drama: BranchingDrama,
        max_depth: int,
    ) -> None:
        children = await self._repo.get_children(node.id)
        characters = await self._resolve_characters(drama.character_ids)
        briefs = self._brief_builder.build_persona_only_many(characters)
        ctx = _PipelineContext(
            drama_id=drama.id,
            prompt=drama.prompt,
            total_segments=drama.total_segments,
            characters=characters,
            briefs=briefs,
            visual_style=drama.visual_style,
        )
        for child in children:
            if not child.image_path and not self._is_being_redrawn(child):
                path = await self._generate_node_image(child, ctx)
                if path:
                    await self._store_prefetched_image(child, path)
            if child.depth < max_depth:
                await self._prefetch_subtree_images(
                    child, drama, max_depth,
                )

    # ── prefetch write barrier (FX3) ──────────────────────────────

    def _is_being_redrawn(self, node: DramaNode) -> bool:
        """Is the player already paying to redraw this exact node?

        Only sees this process, which is all it needs to: the guard exists
        so an automatic prefetch does not spend a Gateway call drawing a
        picture a manual redraw is about to replace anyway. Cross-replica
        races are not this check's job — :meth:`_store_prefetched_image`
        is what makes losing one harmless.
        """
        return node.id in self._scene_regen_in_flight

    async def _store_prefetched_image(
        self, node: DramaNode, path: str,
    ) -> None:
        """Write a prefetched picture only if the node is still picture-less.

        The prefetch walks the subtree depth-first from a snapshot taken
        when it started, and drawing every layer takes long enough for the
        player to press 重繪 (BD6) on a node further up — which charges
        them, writes a fresh object key, and returns. A blind
        ``save_node(node.with_image_path(path))`` from the stale snapshot
        then puts the old picture back, and what the player paid for is
        gone with no error anywhere: last write wins, and the last write is
        the one nobody asked for.

        So the node is re-read at write time and the *speculative* picture
        yields. Anything already there was either bought by the player or
        drawn by another replica; either beats a picture generated on a
        guess. The orphaned object is left behind on purpose — a leaked
        object is cheap, exactly as in
        :meth:`_discard_superseded_scene_image`.
        """
        current = await self._repo.get_node(node.id)
        if current is None:
            # The drama was deleted while this subtree was being drawn.
            return
        if current.image_path:
            _LOGGER.debug(
                "prefetched scene for node=%s dropped — a picture landed "
                "first", node.id,
            )
            return
        await self._repo.save_node(current.with_image_path(path))

    # ── internal helpers ──────────────────────────────────────────

    async def _maybe_apply_event_seed(
        self,
        *,
        prompt: str,
        characters: list[Character],
    ) -> str:
        """When the operator left ``prompt`` blank, try to claim one
        curated event seed from the first eligible character's inbox
        and append it as inspiration for the planner.

        Non-blank prompts are returned unchanged — the operator's
        intent always wins. Failures (no dispenser, character opted
        out, empty inbox) leave the prompt untouched."""
        if (prompt or "").strip():
            return prompt
        if self._event_seed_dispenser is None:
            return prompt
        for character in characters:
            if not character.world_awareness_enabled:
                continue
            try:
                claimed = await self._event_seed_dispenser.claim(
                    character_id=character.id,
                    surface="branching_drama",
                )
            except Exception:
                _LOGGER.exception(
                    "drama: event seed claim crashed character=%s",
                    character.id,
                )
                continue
            if claimed is None:
                continue
            event = claimed.event
            title = (event.title or "").strip()
            if not title:
                continue
            summary = (event.summary or "").strip()
            seed_block = "（外界靈感：" + title
            if summary:
                seed_block += "；" + summary[:160]
            seed_block += "）"
            return (
                f"以下面這條外界消息作為背景靈感（不要照搬，只當引子，"
                f"故事可自由發展）：{seed_block}"
            )
        return prompt

    async def _resolve_characters(
        self, character_ids: Sequence[str],
    ) -> list[Character]:
        seen: set[str] = set()
        ordered: list[str] = []
        for cid in character_ids:
            cleaned = (cid or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            ordered.append(cleaned)
        out: list[Character] = []
        missing: list[str] = []
        for cid in ordered:
            entity = await self._character_service.get_character_entity(cid)
            if entity is None:
                missing.append(cid)
                continue
            out.append(entity)
        if missing:
            raise ValueError(
                "branching drama: unknown character ids: "
                + ", ".join(missing),
            )
        if len(out) < _MIN_CHARACTERS:
            raise ValueError(
                f"branching drama needs at least {_MIN_CHARACTERS} "
                f"characters",
            )
        if len(out) > _MAX_CHARACTERS:
            raise ValueError(
                f"branching drama accepts at most {_MAX_CHARACTERS} "
                f"characters",
            )
        return out

    async def _require_ready(self, drama_id: str) -> BranchingDrama:
        drama = await self._repo.get(drama_id)
        if drama is None:
            raise ValueError(f"branching drama {drama_id} not found")
        if drama.status != STATUS_READY:
            raise ValueError(
                f"branching drama {drama_id} not ready "
                f"(status={drama.status})",
            )
        return drama

    async def _must_load(self, drama_id: str) -> BranchingDrama:
        drama = await self._repo.get(drama_id)
        if drama is None:
            raise _PipelineAbort(
                f"drama {drama_id} disappeared mid-pipeline",
            )
        return drama

    async def _mark_failed(
        self, drama_id: str, reason: str, *, error_code: str | None = None,
    ) -> None:
        """Persist the terminal ``failed`` state for ``drama_id``.

        ``error_code`` carries the cloud gateway's machine-readable refusal
        (see :mod:`~kokoro_link.application.services.studio_failure`) so the
        polling client can distinguish "you're out of 螢火" from a genuine
        fault; it stays ``None`` for ordinary crashes.
        """
        try:
            drama = await self._repo.get(drama_id)
        except Exception:
            _LOGGER.exception(
                "branching drama: could not load to mark failed id=%s",
                drama_id,
            )
            return
        if drama is None:
            return
        try:
            await self._repo.save(
                drama.with_status(
                    STATUS_FAILED,
                    error_message=reason,
                    error_code=error_code,
                ),
            )
        except Exception:
            _LOGGER.exception(
                "branching drama: could not persist failed id=%s",
                drama_id,
            )

    def _spawn(self, coro) -> None:  # noqa: ANN001
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return
        task = loop.create_task(coro)
        self._tasks[id(task)] = task
        task.add_done_callback(lambda t: self._tasks.pop(id(t), None))

    # ── durable job ledger (C0) ───────────────────────────────────

    async def _track_and_spawn(
        self,
        *,
        target_id: str,
        params: dict,
        runner,
        charge: ActionChargeHandle | None = None,
    ) -> None:
        """Record a durable job row, then spawn the pipeline.

        Fail-soft: a broken ledger must never cost a live generation —
        it only loses restart durability for this run.

        ``charge`` rides in the params rather than only in memory: whichever
        replica ends up finalizing this job is the one that must close the
        reservation, and after a restart the params are all it has."""
        params = {**params, **_charge_params(charge)}
        job: StudioGenerationJob | None = None
        if self._jobs is not None:
            try:
                job = StudioGenerationJob.create(
                    kind=JOB_KIND_BRANCHING_CREATE,
                    target_id=target_id,
                    params=params,
                )
                await self._jobs.add(job)
            except Exception:
                _LOGGER.exception(
                    "branching drama: could not record studio job id=%s",
                    target_id,
                )
                job = None
        if job is not None:
            self._spawn(self._run_tracked(job, runner, charge=charge))
        elif charge is not None:
            # No job row to hang the charge off, but the player has still
            # been billed — the pipeline runs under its own charge lifecycle.
            self._spawn(
                self._run_billed(runner, target_id=target_id, charge=charge),
            )
        else:
            self._spawn(runner)

    async def _run_tracked(
        self,
        job: StudioGenerationJob,
        runner,
        *,
        charge: ActionChargeHandle | None = None,
        fresh_handle: bool = False,
    ) -> None:
        result = await self._run_scoped(runner, charge)
        if result == LEASE_ABANDONED:
            # The target's lease is held by / was reclaimed by another replica —
            # that owner finalizes the job. Do NOT touch it here or we would race
            # the legitimate writer. The charge is left open for the same
            # reason: it travels in the job params, so whoever finalizes THIS
            # row closes it, and deciding it here would either refund work that
            # replica is still producing or bill for work it abandoned.
            return
        await self._finalize_job(job, charge=charge, fresh_handle=fresh_handle)

    async def _run_billed(
        self, runner, *, target_id: str, charge: ActionChargeHandle,
    ) -> None:
        """Run a charged pipeline whose job row could not be recorded.

        The one place a lost lease still closes the charge here: with no job
        row there are no params to hand it over with, so "leave it to whoever
        finalizes the row" has no row to leave it to and the reservation would
        simply never be closed. The handle is live and this process's own, so
        its usage record is the honest answer.
        """
        result = await self._run_scoped(runner, charge)
        if result == LEASE_ABANDONED:
            await self._action_billing.release(charge)
            return
        drama = await self._load_for_finalize(target_id)
        if drama is _LOAD_FAILED:
            return
        await self._close_charge_for(charge, drama)  # type: ignore[arg-type]

    async def _run_scoped(self, runner, charge: ActionChargeHandle | None):
        """Await ``runner`` inside the charge's interaction scope.

        Entered even when there is no charge — as an explicit *clear*, so a
        pipeline that could not be billed cannot inherit an unrelated scope
        left behind by whatever spawned it and have its calls waived for free.
        """
        with interaction_scope(charge.context if charge else None):
            return await runner

    async def _finalize_job(
        self,
        job: StudioGenerationJob,
        *,
        charge: ActionChargeHandle | None = None,
        fresh_handle: bool = False,
    ) -> None:
        """Flip the job to its terminal status, then close the charge on it.

        The order is load-bearing: the job row is the only durable record that
        this run ended, so the charge is closed *after* it lands. A failed row
        write leaves a ``running`` row **and** an open reservation — one
        consistent state that recovery re-drives — rather than money already
        moved against a run recovery still believes is in flight.
        """
        drama = await self._load_for_finalize(job.target_id)
        if drama is _LOAD_FAILED:
            # Without the drama neither settle nor release can be justified;
            # the charge stays open and the User-side sweeper decides.
            return
        if self._jobs is not None:
            try:
                await self._jobs.save(_terminal_job_row(job, drama))
            except Exception:
                _LOGGER.exception(
                    "branching drama: could not finalize job id=%s — leaving "
                    "the action charge open for recovery", job.id,
                )
                return
        await self._close_charge_for(
            charge, drama, fresh_handle=fresh_handle,  # type: ignore[arg-type]
        )

    async def _load_for_finalize(self, target_id: str):
        """The drama, ``None`` if deleted, or ``_LOAD_FAILED`` if unreadable.

        The three cases stay distinct: a deleted drama is a definite refund,
        an unreadable one is "we do not know" and must not move money.
        """
        try:
            return await self._repo.get(target_id)
        except Exception:
            _LOGGER.exception(
                "branching drama: could not read drama to finalize id=%s",
                target_id,
            )
            return _LOAD_FAILED

    async def _close_charge_for(
        self,
        charge: ActionChargeHandle | None,
        drama: BranchingDrama | None,
        *,
        fresh_handle: bool = False,
    ) -> None:
        """Settle a delivered tree, refund anything else — unless the handle
        was rebuilt, in which case the *server* decides.

        ``fresh_handle=True`` marks a handle rebuilt from the job params rather
        than held live by this process (recovery, resume). Such a handle's
        usage record is empty by construction — the covered calls were served
        by the process that died — so the billing service's local "nothing was
        served ⇒ full refund" check would give away work the provider was
        already paid for. It therefore asserts no outcome at all and lets the
        User service answer from its own covered-call probe.
        """
        if charge is None:
            return
        if fresh_handle:
            await self._close_fresh_charge(charge)
            return
        if drama is not None and drama.status == STATUS_READY:
            await self._action_billing.settle(charge)
            return
        await self._action_billing.release(charge)

    async def _close_fresh_charge(
        self, charge: ActionChargeHandle | None,
    ) -> None:
        """Close a params-rebuilt charge — by asking the User service, always.

        Neither a plain refund nor a plain settle can be justified from here:
        the handle's usage record is empty because this process is not the one
        that made the calls. ``settle_if_probed`` hands the verdict to the
        covered-call probe, the only record that names *this* charge id.
        """
        if charge is None:
            return
        await self._action_billing.release(charge, settle_if_probed=True)

    async def supersede_job(self, job: StudioGenerationJob) -> None:
        """Fail a duplicate running row — and close the charge it still holds.

        Two ``running`` rows for one drama mean two ``branching_drama_create``
        charges: a second entry-point call that loses the target's lease leaves
        its row (and its own reservation, ids in the params) for recovery,
        while the replica holding the lease finalizes a *different* row under a
        *different* charge. Recovery re-drives only the newest row per target,
        so this is the single place the loser's money is decided — failing the
        row without it would strand the player's credits until the User-side
        sweeper expired them.

        Same order and same CAS as the fusion twin: the row is failed **first**
        because it is the durable record that this duplicate is done with, and
        the write is a compare-and-swap because the row may have been finalized
        meanwhile by the task that still owns it in process — a finalize that
        already closed this very charge. Losing the swap means somebody else
        owns the verdict, so the money is left alone.

        The loser produced no tree, but "produced nothing at all" is not
        something this process can assert: the handle is rebuilt from the
        params, so its usage record is empty whatever the losing replica
        actually served. ``settle_if_probed`` therefore hands the verdict to
        the User service's covered-call probe rather than guessing a refund.
        """
        if self._jobs is None:
            return
        claimed = await self._jobs.save_terminal_if_running(job.with_status(
            JOB_FAILED, error_message="superseded by a newer job",
        ))
        if not claimed:
            _LOGGER.info(
                "branching drama: duplicate job id=%s was already finalized "
                "elsewhere — leaving its action charge to that finalizer",
                job.id,
            )
            return
        await self._close_fresh_charge(_charge_from_params(job.params))

    async def resume_job(self, job: StudioGenerationJob) -> str:
        """Re-drive an interrupted creation job found at startup.

        Returns ``"resumed"`` / ``"finalized"`` / ``"failed"``.

        The charge that paid for this job survived the restart in the params;
        rebuilding the handle is what stops an interrupted creation from
        stranding the player's credits until the upstream sweeper expires
        them."""
        if self._jobs is None:
            return "failed"
        charge = _charge_from_params(job.params)
        drama = await self._repo.get(job.target_id)
        if drama is None:
            await self._jobs.save(job.with_status(
                JOB_FAILED, error_message="target missing",
            ))
            await self._close_fresh_charge(charge)
            return "failed"
        if drama.is_terminal():
            # Terminal, but not necessarily *this* charge's pipeline — a tree
            # finished by the replica that won the lease looks identical from
            # here. So the outcome is never read off the drama; the
            # fresh-handle path hands every case to the ledger's probe.
            await self._finalize_job(job, charge=charge, fresh_handle=True)
            return "finalized"
        if job.attempts >= MAX_JOB_ATTEMPTS:
            await self._mark_failed(
                drama.id, "generation interrupted repeatedly",
            )
            await self._jobs.save(job.with_status(
                JOB_FAILED, error_message="attempt limit reached",
            ))
            await self._close_fresh_charge(charge)
            return "failed"
        params = dict(job.params)
        language = str(
            params.get("operator_primary_language") or "zh-TW",
        )
        try:
            ctx = await self._make_context(
                drama, operator_primary_language=language,
            )
        except ValueError as exc:
            await self._mark_failed(drama.id, str(exc))
            await self._jobs.save(job.with_status(
                JOB_FAILED, error_message=str(exc),
            ))
            await self._close_fresh_charge(charge)
            return "failed"
        job = job.with_attempts(job.attempts + 1)
        await self._jobs.save(job)
        # The resumed run keeps generating under the *original* charge and its
        # scope, but the handle is still a rebuilt one: the pre-crash covered
        # calls are a fact only the ledger remembers, so its finalize stays on
        # the fresh-handle path.
        self._spawn(self._run_tracked(
            job, self._resume_generation_pipeline(ctx),
            charge=charge, fresh_handle=True,
        ))
        return "resumed"

    @property
    def _can_render_scene_image(self) -> bool:
        """Is there a renderer and somewhere to put what it draws?

        Purely a capability question, deliberately free of the prefetch
        depth: BD6's manual redraw is not a prefetch, so a deployment that
        turned the automatic drawing off to stop paying for branches
        nobody walks into can still honour a picture the player asked and
        paid for by hand.
        """
        return (
            self._scene_image is not None
            and self._object_storage is not None
        )

    @property
    def _can_generate_images(self) -> bool:
        """Is drawing scene art possible *and* wanted ahead of the player?

        Every image on this path is a prefetch — the initial layers at
        creation and the subtree ahead of the player — so a configured
        depth of ``0`` is indistinguishable from having no renderer at
        all, and answering "no" here keeps that single fact in one place.
        """
        return self._can_render_scene_image and self._image_prefetch_depth > 0

    def _lock_for(self, drama_id: str) -> asyncio.Lock:
        lock = self._locks.get(drama_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[drama_id] = lock
        return lock

    def _lease_session(self, drama_id: str) -> StudioLeaseSession:
        return StudioLeaseSession(
            self._execution_lease,
            drama_id,
            heartbeat_interval_seconds=self._lease_heartbeat_interval,
        )

    def _log_lease_skip(self, drama_id: str) -> None:
        # Another replica holds the target's lease → do NOT drive or clobber it;
        # the owner finalizes both drama and job. (Distributed-only; the
        # in-memory lease in a single process always acquires.)
        _LOGGER.info(
            "branching drama: target leased by another replica, skipping id=%s",
            drama_id,
        )

    def _log_lease_lost(self, drama_id: str) -> None:
        _LOGGER.warning(
            "branching drama: lease lost mid-run id=%s — abandoning to the "
            "reclaiming replica (no drama/job write)",
            drama_id,
        )


class _PipelineAbort(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_SCENE_OBJECT_PREFIX = "branching-dramas"
_SCENE_VERSION_BYTES = 4


def _scene_object_key(node: DramaNode) -> str:
    """Where a node's *first* picture lands. Stable, so the prefetch's
    "already has an image_path" skip and this key mean the same thing."""
    return f"{_SCENE_OBJECT_PREFIX}/{node.drama_id}/{node.id}.png"


def _versioned_scene_object_key(node: DramaNode) -> str:
    """A key no browser, proxy or CDN has ever seen (BD6).

    The redraw's whole point is that the player stops looking at the old
    picture, and reusing the key cannot promise that: the URL in
    ``image_path`` is the same string, so every cache in the path is
    entitled to keep serving what it already has. A query-string version
    would work only as long as every consumer forwards it — the object
    store's own variant fan-out appends ``?``-free suffixes to keys, and a
    `<img>` rebuilt from the key alone would drop it. A distinct key has
    no such dependency: it is a different object.
    """
    return (
        f"{_SCENE_OBJECT_PREFIX}/{node.drama_id}/"
        f"{node.id}-{secrets.token_hex(_SCENE_VERSION_BYTES)}.png"
    )


def _terminal_job_row(
    job: StudioGenerationJob, drama: BranchingDrama | None,
) -> StudioGenerationJob:
    """The job row this run ends on, read off the drama's own status."""
    if drama is None:
        return job.with_status(JOB_FAILED, error_message="target deleted")
    if drama.status == STATUS_READY:
        return job.with_status(JOB_SUCCEEDED)
    if drama.status == STATUS_FAILED:
        return job.with_status(
            JOB_FAILED, error_message=drama.error_message,
        )
    return job.with_status(
        JOB_FAILED,
        error_message=f"pipeline ended non-terminal ({drama.status})",
    )


def _charge_params(charge: ActionChargeHandle | None) -> dict[str, str]:
    """The action charge, flattened into JSON-safe job params."""
    if charge is None:
        return {}
    return {
        "charge_id": charge.charge_id,
        "interaction_id": charge.interaction_id,
        "action_key": charge.action_key,
    }


def _charge_from_params(params: Mapping) -> ActionChargeHandle | None:
    """Rebuild the handle a previous process opened, if the params carry one.

    All three fields are required — a handle missing any of them could close
    the wrong reservation or stamp calls with an id no charge answers to, both
    of which are worse than leaving the charge to the upstream sweeper.

    The rebuilt handle starts with an empty usage record, and that emptiness
    is a *gap in knowledge*, not evidence: every close driven from one of these
    handles travels the fresh-handle path
    (:meth:`BranchingDramaService._close_fresh_charge`).
    """
    values: list[str] = []
    for key in _CHARGE_PARAM_KEYS:
        raw = params.get(key)
        if not isinstance(raw, str) or not raw.strip():
            return None
        values.append(raw.strip())
    charge_id, interaction_id, action_key = values
    return ActionChargeHandle(
        action_key=action_key,
        charge_id=charge_id,
        interaction_id=interaction_id,
    )
