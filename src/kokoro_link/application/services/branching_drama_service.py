"""Branching-drama orchestrator.

Coordinates two lifecycles:

**Creation** — background pipeline that generates the initial outline
layers (root + first children), then pre-generates images for those
layers:

    generating_outlines → generating_images → ready

**Gameplay** — synchronous per-request flow with lazy generation:

    start_session → (narrate root) → advance → advance → … → end

Deeper outline layers and images are generated lazily as the player
advances, prefetching 2 layers ahead in the background.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from typing import TYPE_CHECKING

from kokoro_link.application.services.branching_drama_critic import (
    BranchingDramaCritic,
)
from kokoro_link.application.services.branching_drama_director import (
    BranchingDramaDirector,
)
from kokoro_link.application.services.branching_drama_planner import (
    BranchingDramaPlanner,
    NodeOutline,
)
from kokoro_link.application.services.branching_drama_polisher import (
    BranchingDramaPolisher,
)
from kokoro_link.application.services.character_service import CharacterService

if TYPE_CHECKING:
    from kokoro_link.application.services.event_seed_dispenser import (
        EventSeedDispenser,
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
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.studio_jobs import (
    JOB_KIND_BRANCHING_CREATE,
    JOB_STATUS_FAILED as JOB_FAILED,
    JOB_STATUS_SUCCEEDED as JOB_SUCCEEDED,
    MAX_JOB_ATTEMPTS,
    StudioGenerationJob,
    StudioJobRepositoryPort,
)
from kokoro_link.domain.entities.branching_drama import (
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
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_visual_identity_lines,
)
from kokoro_link.infrastructure.tools.comfyui.scene_generator import (
    ComfySceneGenerator,
    SceneGenerationError,
)


_LOGGER = logging.getLogger(__name__)
_OUTLINE_CONCURRENCY = 5


@dataclass(slots=True)
class _PipelineContext:
    drama_id: str
    prompt: str
    total_segments: int
    characters: list[Character]
    briefs: list[CharacterBrief]
    operator_primary_language: str = "zh-TW"


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
        scene_generator: ComfySceneGenerator | None = None,
        object_storage: ObjectStoragePort | None = None,
        event_seed_dispenser: "EventSeedDispenser | None" = None,
        visual_style_service: VisualGenerationStyleService | None = None,
        jobs: StudioJobRepositoryPort | None = None,
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
        self._scene_generator = scene_generator
        self._object_storage = object_storage
        self._event_seed_dispenser = event_seed_dispenser
        self._visual_style_service = visual_style_service
        # Durable job ledger (C0). ``None`` keeps the pre-C0 fire-and-
        # forget behaviour for rigs that don't care about restarts.
        self._jobs = jobs
        self._tasks: dict[int, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}

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
        operator_primary_language: str = "zh-TW",
    ) -> BranchingDrama:
        characters = await self._resolve_characters(character_ids)
        # Operator left the prompt blank → try to seed direction from
        # the first eligible character's curated event inbox. Keeps
        # drama generation deterministic when the operator *did* write
        # a brief, but offers an LLM-grounded fallback when they didn't.
        prompt = await self._maybe_apply_event_seed(
            prompt=prompt, characters=characters,
        )
        drama = BranchingDrama.create_pending(
            character_ids=[c.id for c in characters],
            prompt=prompt,
            total_segments=total_segments,
        )
        await self._repo.add(drama)

        ctx = _PipelineContext(
            drama_id=drama.id,
            prompt=drama.prompt,
            total_segments=drama.total_segments,
            characters=characters,
            briefs=self._brief_builder.build_persona_only_many(characters),
            operator_primary_language=operator_primary_language,
        )
        await self._track_and_spawn(
            target_id=drama.id,
            params={
                "operator_primary_language": operator_primary_language,
            },
            runner=self._run_generation_pipeline(ctx),
        )
        return drama

    # ── session management ────────────────────────────────────────

    async def start_session(
        self, drama_id: str, *, operator_primary_language: str = "zh-TW",
    ) -> tuple[DramaSession, DramaNode, str]:
        """Start a new playthrough. Returns (session, root_node, narration)."""
        drama = await self._require_ready(drama_id)
        root = await self._repo.get_root_node(drama_id)
        if root is None:
            raise ValueError(f"drama {drama_id} has no root node")

        characters = await self._resolve_characters(drama.character_ids)
        briefs = self._brief_builder.build_persona_only_many(characters)

        narration = await self._narrate_with_language(
            node=root, briefs=briefs, previous_turns=(),
            operator_primary_language=operator_primary_language,
        )
        narration = await self._review_and_polish(
            node=root, narration_text=narration, briefs=briefs,
            previous_turns=(),
            operator_primary_language=operator_primary_language,
        )
        session = DramaSession.start(
            drama_id=drama_id, root_node_id=root.id,
        )
        session = session.with_turn(
            node_id=root.id, narration=narration,
        )
        await self._repo.add_session(session)

        self._maybe_prefetch_ahead(
            root, drama, operator_primary_language=operator_primary_language,
        )
        return session, root, narration

    async def interact_session(
        self,
        session_id: str,
        *,
        player_input: str,
        operator_primary_language: str = "zh-TW",
    ) -> tuple[DramaSession, str, str | None]:
        """Interact within the current beat.

        Returns ``(session, response, advance_hint)``.
        ``advance_hint`` is a short phrase for the advance button when
        the LLM judges the beat is sufficiently explored, else ``None``.
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

        response, advance_hint = await self._respond_with_language(
            node=current_node,
            briefs=briefs,
            previous_turns=session.turns[:-1] if session.turns else (),
            exchanges=existing_exchanges,
            player_input=player_input,
            operator_primary_language=operator_primary_language,
        )

        session = session.with_exchange(
            player_input=player_input, response=response,
        )
        await self._repo.save_session(session)
        return session, response, advance_hint

    async def advance_session(
        self,
        session_id: str,
        *,
        operator_primary_language: str = "zh-TW",
    ) -> tuple[DramaSession, DramaNode, str, bool]:
        """Advance to the next beat based on accumulated exchanges.

        Returns ``(session, next_node, narration, is_ending)``.
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

        children_list = await self._ensure_children_exist(
            current_node, drama,
            operator_primary_language=operator_primary_language,
        )
        if not children_list:
            session = session.end()
            await self._repo.save_session(session)
            raise ValueError("no children — already at ending")

        children_by_tone = {c.tone: c for c in children_list if c.tone}

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

        characters = await self._resolve_characters(drama.character_ids)
        briefs = self._brief_builder.build_persona_only_many(characters)

        narration = await self._narrate_with_language(
            node=next_node,
            briefs=briefs,
            previous_turns=session.turns,
            operator_primary_language=operator_primary_language,
        )
        narration = await self._review_and_polish(
            node=next_node, narration_text=narration, briefs=briefs,
            previous_turns=session.turns,
            operator_primary_language=operator_primary_language,
        )

        is_ending = next_node.depth >= drama.total_segments - 1
        session = session.with_turn(
            node_id=next_node.id,
            narration=narration,
            chosen_tone=tone,
        )
        await self._repo.save_session(session)

        self._maybe_prefetch_ahead(
            next_node, drama,
            operator_primary_language=operator_primary_language,
        )
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
            critique = await self._critic.review(
                node=node,
                narration_text=narration_text,
                briefs=briefs,
                previous_turns=previous_turns,
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
    ) -> tuple[str, NodeOutline]:
        try:
            return await self._planner.plan_root(
                prompt=prompt,
                briefs=briefs,
                total_segments=total_segments,
                operator_primary_language=operator_primary_language,
            )
        except TypeError as exc:
            if "operator_primary_language" not in str(exc):
                raise
            return await self._planner.plan_root(
                prompt=prompt,
                briefs=briefs,
                total_segments=total_segments,
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
    ) -> dict[str, NodeOutline]:
        try:
            return await self._planner.plan_children(
                prompt=prompt,
                briefs=briefs,
                parent_summary=parent_summary,
                path_context=path_context,
                depth=depth,
                total_segments=total_segments,
                operator_primary_language=operator_primary_language,
            )
        except TypeError as exc:
            if "operator_primary_language" not in str(exc):
                raise
            return await self._planner.plan_children(
                prompt=prompt,
                briefs=briefs,
                parent_summary=parent_summary,
                path_context=path_context,
                depth=depth,
                total_segments=total_segments,
            )

    async def _narrate_with_language(
        self,
        *,
        node: DramaNode,
        briefs: Sequence[CharacterBrief],
        previous_turns: Sequence,
        operator_primary_language: str,
    ) -> str:
        try:
            return await self._director.narrate(
                node=node,
                briefs=briefs,
                previous_turns=previous_turns,
                operator_primary_language=operator_primary_language,
            )
        except TypeError as exc:
            if "operator_primary_language" not in str(exc):
                raise
            return await self._director.narrate(
                node=node,
                briefs=briefs,
                previous_turns=previous_turns,
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
    ) -> tuple[str, str | None]:
        try:
            return await self._director.respond_in_scene(
                node=node,
                briefs=briefs,
                previous_turns=previous_turns,
                exchanges=exchanges,
                player_input=player_input,
                operator_primary_language=operator_primary_language,
            )
        except TypeError as exc:
            if "operator_primary_language" not in str(exc):
                raise
            return await self._director.respond_in_scene(
                node=node,
                briefs=briefs,
                previous_turns=previous_turns,
                exchanges=exchanges,
                player_input=player_input,
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
        if self._polisher is None:
            return narration_text
        try:
            return await self._polisher.polish(
                node=node,
                narration_text=narration_text,
                critique=critique,
                briefs=briefs,
                previous_turns=previous_turns,
                operator_primary_language=operator_primary_language,
            )
        except TypeError as exc:
            if "operator_primary_language" not in str(exc):
                raise
            return await self._polisher.polish(
                node=node,
                narration_text=narration_text,
                critique=critique,
                briefs=briefs,
                previous_turns=previous_turns,
            )

    # ── generation pipeline ───────────────────────────────────────

    async def _run_generation_pipeline(
        self, ctx: _PipelineContext,
    ) -> None:
        async with self._lock_for(ctx.drama_id):
            try:
                await self._generate_tree(ctx)
                await self._generate_initial_images(ctx)
                drama = await self._must_load(ctx.drama_id)
                await self._repo.save(
                    drama.with_status(STATUS_READY),
                )
            except _PipelineAbort as abort:
                _LOGGER.warning(
                    "branching drama pipeline aborted id=%s reason=%s",
                    ctx.drama_id, abort.reason,
                )
                await self._mark_failed(ctx.drama_id, abort.reason)
            except Exception:
                _LOGGER.exception(
                    "branching drama pipeline crashed id=%s", ctx.drama_id,
                )
                await self._mark_failed(ctx.drama_id, "pipeline crashed")

    async def _resume_generation_pipeline(
        self, ctx: _PipelineContext,
    ) -> None:
        """Re-drive a creation pipeline interrupted by a restart.

        Persisted nodes are the checkpoint: an existing root is reused
        (``_generate_tree`` would create a duplicate), missing child
        layers are regenerated per-parent, and the image stage already
        skips nodes that have an ``image_path``.
        """
        async with self._lock_for(ctx.drama_id):
            try:
                root = await self._repo.get_root_node(ctx.drama_id)
                if root is None:
                    await self._generate_tree(ctx)
                else:
                    await self._fill_missing_layers(ctx, root)
                await self._generate_initial_images(ctx)
                drama = await self._must_load(ctx.drama_id)
                await self._repo.save(
                    drama.with_status(STATUS_READY),
                )
            except _PipelineAbort as abort:
                _LOGGER.warning(
                    "branching drama resume aborted id=%s reason=%s",
                    ctx.drama_id, abort.reason,
                )
                await self._mark_failed(ctx.drama_id, abort.reason)
            except Exception:
                _LOGGER.exception(
                    "branching drama resume crashed id=%s", ctx.drama_id,
                )
                await self._mark_failed(ctx.drama_id, "pipeline crashed")

    async def _fill_missing_layers(
        self, ctx: _PipelineContext, root: DramaNode,
    ) -> None:
        """Generate only the initial layers that never landed."""
        initial_depth = min(OUTLINE_PREFETCH_DEPTH, ctx.total_segments)
        current_layer = [root]
        for depth in range(1, initial_depth):
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
                    ctx, parent_nodes=missing_parents, depth=depth,
                ))
            current_layer = next_layer

    async def _generate_tree(self, ctx: _PipelineContext) -> None:
        drama_title, root_outline = await self._plan_root_with_language(
            prompt=ctx.prompt,
            briefs=ctx.briefs,
            total_segments=ctx.total_segments,
            operator_primary_language=ctx.operator_primary_language,
        )
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
            next_layer = await self._generate_layer(
                ctx, parent_nodes=current_layer, depth=depth,
            )
            current_layer = next_layer

    async def _generate_layer(
        self,
        ctx: _PipelineContext,
        *,
        parent_nodes: list[DramaNode],
        depth: int,
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
        )

    async def _ensure_children_exist(
        self,
        node: DramaNode,
        drama: BranchingDrama,
        *,
        operator_primary_language: str = "zh-TW",
    ) -> list[DramaNode]:
        """Return children of *node*, generating them lazily if needed."""
        existing = await self._repo.get_children(node.id)
        if existing:
            return existing
        if node.depth >= drama.total_segments - 1:
            return []
        async with self._lock_for(drama.id):
            existing = await self._repo.get_children(node.id)
            if existing:
                return existing
            ctx = await self._make_context(
                drama,
                operator_primary_language=operator_primary_language,
            )
            return await self._generate_layer(
                ctx, parent_nodes=[node], depth=node.depth + 1,
            )

    # ── lazy prefetch (outlines + images) ────────────────────────

    def _maybe_prefetch_ahead(
        self,
        node: DramaNode,
        drama: BranchingDrama,
        *,
        operator_primary_language: str = "zh-TW",
    ) -> None:
        """Queue outline + image generation for upcoming layers."""
        max_depth = min(
            node.depth + OUTLINE_PREFETCH_DEPTH,
            drama.total_segments - 1,
        )
        if node.depth >= max_depth:
            return

        async def prefetch() -> None:
            try:
                await self._prefetch_subtree_outlines(
                    node, drama, max_depth,
                    operator_primary_language=operator_primary_language,
                )
            except Exception:
                _LOGGER.exception(
                    "outline prefetch failed node=%s", node.id,
                )
            if self._can_generate_images:
                try:
                    await self._prefetch_subtree_images(
                        node, drama, max_depth,
                    )
                except Exception:
                    _LOGGER.exception(
                        "image prefetch failed node=%s", node.id,
                    )

        self._spawn(prefetch())

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
        children = await self._ensure_children_exist(
            node, drama,
            operator_primary_language=operator_primary_language,
        )
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
        # Pre-generate images for depth 0 and 1 (4 images total).
        # Actual image generation depends on ComfyUI availability —
        # skip gracefully if not configured.
        if not self._can_generate_images:
            return
        for d in range(min(IMAGE_PREFETCH_DEPTH, ctx.total_segments)):
            nodes = await self._repo.get_nodes_at_depth(ctx.drama_id, d)
            for node in nodes:
                if node.image_path:
                    continue
                path = await self._generate_node_image(node, ctx)
                if path:
                    await self._repo.save_node(node.with_image_path(path))

    async def _generate_node_image(
        self,
        node: DramaNode,
        ctx: _PipelineContext,
    ) -> str | None:
        """Generate a VN-style scene image for a node."""
        if not self._can_generate_images:
            return None
        prompt = await self._build_scene_prompt(node, ctx)
        try:
            image_bytes = await self._scene_generator.generate(
                positive=prompt, aspect="landscape",
            )
        except SceneGenerationError:
            _LOGGER.warning(
                "scene generation failed for node=%s, skipping", node.id,
            )
            return None
        object_key = f"branching-dramas/{node.drama_id}/{node.id}.png"
        if self._object_storage is None:
            return None
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
        return stored.url

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
        )
        for child in children:
            if not child.image_path:
                path = await self._generate_node_image(child, ctx)
                if path:
                    await self._repo.save_node(child.with_image_path(path))
            if child.depth < max_depth:
                await self._prefetch_subtree_images(
                    child, drama, max_depth,
                )

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
        self, drama_id: str, reason: str,
    ) -> None:
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
                drama.with_status(STATUS_FAILED, error_message=reason),
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
    ) -> None:
        """Record a durable job row, then spawn the pipeline.

        Fail-soft: a broken ledger must never cost a live generation —
        it only loses restart durability for this run."""
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
        if job is None:
            self._spawn(runner)
        else:
            self._spawn(self._run_tracked(job, runner))

    async def _run_tracked(
        self, job: StudioGenerationJob, runner,
    ) -> None:
        await runner
        await self._finalize_job(job)

    async def _finalize_job(self, job: StudioGenerationJob) -> None:
        """Flip the job to its terminal status from the drama state."""
        if self._jobs is None:
            return
        try:
            drama = await self._repo.get(job.target_id)
            if drama is None:
                updated = job.with_status(
                    JOB_FAILED, error_message="target deleted",
                )
            elif drama.status == STATUS_READY:
                updated = job.with_status(JOB_SUCCEEDED)
            elif drama.status == STATUS_FAILED:
                updated = job.with_status(
                    JOB_FAILED, error_message=drama.error_message,
                )
            else:
                updated = job.with_status(
                    JOB_FAILED,
                    error_message=(
                        f"pipeline ended non-terminal ({drama.status})"
                    ),
                )
            await self._jobs.save(updated)
        except Exception:
            _LOGGER.exception(
                "branching drama: could not finalize job id=%s", job.id,
            )

    async def resume_job(self, job: StudioGenerationJob) -> str:
        """Re-drive an interrupted creation job found at startup.

        Returns ``"resumed"`` / ``"finalized"`` / ``"failed"``."""
        if self._jobs is None:
            return "failed"
        drama = await self._repo.get(job.target_id)
        if drama is None:
            await self._jobs.save(job.with_status(
                JOB_FAILED, error_message="target missing",
            ))
            return "failed"
        if drama.is_terminal():
            await self._finalize_job(job)
            return "finalized"
        if job.attempts >= MAX_JOB_ATTEMPTS:
            await self._mark_failed(
                drama.id, "generation interrupted repeatedly",
            )
            await self._jobs.save(job.with_status(
                JOB_FAILED, error_message="attempt limit reached",
            ))
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
            return "failed"
        job = job.with_attempts(job.attempts + 1)
        await self._jobs.save(job)
        self._spawn(self._run_tracked(
            job, self._resume_generation_pipeline(ctx),
        ))
        return "resumed"

    @property
    def _can_generate_images(self) -> bool:
        return self._scene_generator is not None and self._object_storage is not None

    def _lock_for(self, drama_id: str) -> asyncio.Lock:
        lock = self._locks.get(drama_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[drama_id] = lock
        return lock


class _PipelineAbort(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
