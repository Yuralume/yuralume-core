"""Application service for branching-drama → arc-template draft (BD7).

The exit from a finished playthrough. A player who just walked one line
through a branching tree has, at that moment, the most specific piece of
material this product can produce: a story they made, with the beats they
chose and the things they said inside them. Until this service existed
that line died with the ending overlay — the session stayed in the
database and nothing could ever be made from it again.

Separate from ``FusionToArcDraftService`` on purpose (BD7 拍板): the
source is a transcript rather than authored prose, the ended-session
precondition has no fusion analogue, and the conversion mode arrives
pre-answered by the drama's own ``operator_position``. What is shared is
the *destination* — the same unsaved :class:`TemplateDraft` the fusion
adapt produces, so the創作專區 wizard picks it up with no new surface —
and the same ``arc_adapt`` feature key, so the conversion costs what
converting a finished work has always cost.
"""

from __future__ import annotations

import logging

from kokoro_link.application.services.arc_template_intake_service import (
    TemplateDraft,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.contracts.drama_to_arc import (
    DramaPathBeat,
    DramaToArcAdapterPort,
    DramaToArcContext,
    operator_mode_for_drama_position,
)
from kokoro_link.contracts.fusion_to_arc import (
    FUSION_OPERATOR_MODE_UNCHANGED,
    normalise_fusion_operator_mode,
)
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.domain.entities.branching_drama import (
    DramaSession,
    DramaSessionTurn,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_arc_planner_relationship_lines,
)

_LOGGER = logging.getLogger(__name__)


class DramaToArcDraftService:
    """Creates unsaved arc-template drafts from ended drama playthroughs."""

    def __init__(
        self,
        *,
        drama_service,  # noqa: ANN001 - BranchingDramaService (read surface)
        character_service: CharacterService,
        adapter: DramaToArcAdapterPort,
        relationship_seed_repository: (
            CharacterOperatorRelationshipSeedRepositoryPort | None
        ) = None,
        operator_profile_service=None,  # noqa: ANN001 - resolves operator id
    ) -> None:
        self._drama_service = drama_service
        self._character_service = character_service
        self._adapter = adapter
        # Same optional wiring, same fail-soft, and the same reason as the
        # fusion path: the two modes that put the player in the arc write
        # better beats when they know how this cast already addresses them.
        self._relationship_seed_repository = relationship_seed_repository
        self._operator_profile_service = operator_profile_service

    async def adapt(
        self,
        drama_id: str,
        session_id: str,
        *,
        user_id: str | None = None,
        operator_primary_language: str = "zh-TW",
        instruction: str = "",
        operator_mode: str | None = None,
    ) -> TemplateDraft | None:
        """Adapt one ended playthrough into an unsaved template draft.

        ``operator_mode`` is the player's answer to "how do I enter the arc
        this becomes". ``None`` means they did not re-answer it, and the
        drama's own ``operator_position`` answers for them (BD7); an
        off-vocabulary value raises rather than picking a branch on the
        caller's behalf.
        """
        session = await self._load_ended_session(drama_id, session_id)
        drama = await self._drama_service.get(drama_id)
        if drama is None:
            raise ValueError("Branching drama not found")

        mode = (
            operator_mode_for_drama_position(drama.operator_position)
            if operator_mode is None
            else normalise_fusion_operator_mode(operator_mode)
        )
        path = await self._build_path(session)
        if not path:
            # An ended session with no turns is a playthrough that never
            # started. There is nothing to adapt and a draft built from
            # nothing would be the model inventing a story the player did
            # not play — which is the one thing this exit must not do.
            raise ValueError("Drama session has no played path")

        characters = await self._resolve_characters(
            drama.character_ids, user_id=user_id,
        )
        return await self._adapter.adapt(
            DramaToArcContext(
                drama=drama,
                path=path,
                characters=tuple(characters),
                operator_primary_language=operator_primary_language,
                instruction=instruction.strip(),
                operator_mode=mode,
                operator_relationship_lines=(
                    await self._load_operator_relationship_lines(
                        characters, user_id=user_id, mode=mode,
                    )
                ),
            )
        )

    # ── source material ───────────────────────────────────────────────

    async def _load_ended_session(
        self, drama_id: str, session_id: str,
    ) -> DramaSession:
        """The playthrough, or a refusal naming which precondition failed.

        A session belonging to another drama is "not found" rather than a
        mismatch error: the pair is the address, and answering "wrong
        drama" would confirm the id exists somewhere.
        """
        session = await self._drama_service.get_session(session_id)
        if session is None or session.drama_id != drama_id:
            raise ValueError("Drama session not found")
        if not session.is_ended:
            # Mid-playthrough the line is still being walked. Adapting it
            # now would freeze a story the player has not finished telling
            # — and they would pay for the conversion twice to get the end.
            raise ValueError("Drama session has not ended")
        return session

    async def _build_path(
        self, session: DramaSession,
    ) -> tuple[DramaPathBeat, ...]:
        """The walked line, in play order, outline joined to transcript.

        A turn whose node has vanished (a drama deleted mid-conversion,
        a repository that lost a row) keeps its narration and loses only
        the authored title / summary: the transcript is the part that
        cannot be recovered from anywhere else, so it must never be
        dropped because its label went missing.
        """
        beats: list[DramaPathBeat] = []
        for index, turn in enumerate(session.turns):
            node = await self._safe_get_node(turn)
            beats.append(
                DramaPathBeat(
                    sequence=index,
                    depth=node.depth if node is not None else index,
                    tone=turn.chosen_tone,
                    title=node.title if node is not None else "",
                    summary=node.summary if node is not None else "",
                    narration=turn.narration,
                    exchanges=turn.exchanges,
                ),
            )
        return tuple(beats)

    async def _safe_get_node(self, turn: DramaSessionTurn):  # noqa: ANN201
        try:
            return await self._drama_service.get_node(turn.node_id)
        except Exception:
            _LOGGER.exception(
                "drama-to-arc: node %s unavailable; adapting that beat "
                "from its transcript alone", turn.node_id,
            )
            return None

    async def _resolve_characters(
        self,
        character_ids: tuple[str, ...],
        *,
        user_id: str | None,
    ) -> list[Character]:
        characters: list[Character] = []
        for character_id in character_ids:
            character = await self._character_service.get_character_entity(
                character_id,
                user_id=user_id,
            )
            if character is None:
                raise ValueError("Character not found")
            characters.append(character)
        return characters

    # ── who the player is to this cast ────────────────────────────────

    async def _load_operator_relationship_lines(
        self,
        characters: list[Character],
        *,
        user_id: str | None,
        mode: str,
    ) -> tuple[str, ...]:
        """Pre-render who the player is to each character in the cast.

        Mirrors the fusion path's rule deliberately (紅線 5): not loaded
        at all under ``unchanged``, because the cheapest way to keep a
        prompt from writing the player into a story they declared they are
        not in is to never tell it their name. Fail-soft to ``()`` at
        every step — a missing relationship costs a nuance in one
        adaptation, never the adaptation itself.
        """
        if mode == FUSION_OPERATOR_MODE_UNCHANGED:
            return ()
        repository = self._relationship_seed_repository
        if repository is None or not characters:
            return ()
        operator_id = await self._resolve_operator_id(user_id)
        if not operator_id:
            return ()
        lines: list[str] = []
        for character in characters:
            try:
                seed = await repository.get(character.id, operator_id)
            except Exception:
                _LOGGER.exception(
                    "drama-to-arc relationship material unavailable "
                    "character=%s; adapting without it", character.id,
                )
                continue
            rendered = render_arc_planner_relationship_lines(seed)
            if not rendered:
                continue
            # Grouped per character for the same reason as the fusion
            # twin: a drama has a cast, and an ungrouped pile of address
            # terms leaves the model guessing whose is whose.
            lines.append(f"{character.name}：")
            lines.extend(rendered)
        return tuple(lines)

    async def _resolve_operator_id(self, user_id: str | None) -> str | None:
        service = self._operator_profile_service
        if service is None or not user_id:
            return None
        try:
            profile = await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception(
                "drama-to-arc operator profile unavailable; adapting "
                "without relationship material",
            )
            return None
        return getattr(profile, "id", None) if profile else None
