"""Player edit of the per-(character, operator) relationship address names.

Backs ``PATCH /characters/{id}/relationship-names``. Updating the seed's
``user_address_name`` (how the character addresses the player) or
``character_address_name`` (how the player addresses the character) does
three things, in one logical operation:

1. Persist the new value on the relationship seed.
2. Record the change in the per-pair address-change log (one event per
   changed direction, carrying ``character_id``) so the chat prompt can
   surface the rename as a relationship event and link old memories.
3. For the *player* direction, reconcile the learned persona ``name`` to
   the new value (supersede-then-insert via the persona service) so the
   prompt never shows the fresh seed line beside a stale learned name.

History is never rewritten: old memories keep the old name; the alias
bridge + rename-log acknowledgement carry the continuity.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone

from kokoro_link.contracts.address_change_log import (
    AddressChangeLogRepositoryPort,
)
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.value_objects.address_change_event import (
    DIRECTION_CHARACTER,
    DIRECTION_PLAYER,
    SOURCE_OBSERVED,
    SOURCE_PLAYER_EDIT,
    VALID_SOURCES,
    AddressChangeEvent,
)

_LOGGER = logging.getLogger(__name__)


class RelationshipNamesService:
    def __init__(
        self,
        *,
        seed_repository: CharacterOperatorRelationshipSeedRepositoryPort,
        change_log_repository: AddressChangeLogRepositoryPort,
        persona_service=None,
    ) -> None:
        self._seeds = seed_repository
        self._change_log = change_log_repository
        self._persona_service = persona_service

    async def get_names(
        self, *, character_id: str, operator_id: str,
    ) -> tuple[str, str]:
        """Return ``(user_address_name, character_address_name)`` for the
        pair, or empty strings when no seed exists yet."""
        seed = await self._seeds.get(character_id, operator_id)
        if seed is None:
            return "", ""
        return seed.user_address_name, seed.character_address_name

    async def update_names(
        self,
        *,
        character_id: str,
        operator_id: str,
        user_address_name: str | None = None,
        character_address_name: str | None = None,
        source: str = SOURCE_PLAYER_EDIT,
        now: datetime | None = None,
    ) -> CharacterOperatorRelationshipSeed:
        """Apply the address-name edit and return the updated seed.

        Tri-state per field: ``None`` leaves the existing value untouched;
        a provided string sets it (an empty string clears the name). A
        rename-log event is written only when the new value is non-empty
        and actually changed — clearing a name reverts to the
        lower-precedence source and needs no acknowledgement.

        ``source`` stamps the rename-log event: ``player_edit`` for the
        settings-UI edit (default), ``observed`` when the change was
        captured from natural conversation by the post-turn extractor.
        """
        if source not in VALID_SOURCES:
            source = SOURCE_PLAYER_EDIT
        when = now or datetime.now(timezone.utc)
        existing = await self._seeds.get(character_id, operator_id)
        base = existing or CharacterOperatorRelationshipSeed(
            character_id=character_id,
            operator_id=operator_id,
        )
        old_user = base.user_address_name
        old_char = base.character_address_name
        new_user = old_user if user_address_name is None else user_address_name.strip()
        new_char = (
            old_char
            if character_address_name is None
            else character_address_name.strip()
        )

        # Direction-inversion guard for chat-observed writes only. The
        # post-turn mini model sometimes mis-reads the player addressing the
        # *character* (兄妹設定喊「哥哥」、情侶喊「老公」、或直接喊角色名)
        # as a ``player``-direction change, whose value is really how the
        # player addresses the character — the opposite direction. Applying
        # it would overwrite ``user_address_name`` (how the character
        # addresses the player) with the other direction's term, flipping the
        # two. When an observed new value collides with the *opposite*
        # direction's current seed value, drop that field back to its old
        # value so it is treated as "no change" (the other field still
        # updates). A ``player_edit`` is the player's own deliberate action
        # and is never blocked — the player may set any value they like.
        if source == SOURCE_OBSERVED:
            if new_user and new_user == old_char:
                _LOGGER.warning(
                    "observed address change dropped: player-direction value "
                    "%r matches how the player addresses the character "
                    "(direction inversion suspected) char=%s op=%s",
                    new_user, character_id, operator_id,
                )
                new_user = old_user
            if new_char and new_char == old_user:
                _LOGGER.warning(
                    "observed address change dropped: character-direction "
                    "value %r matches how the character addresses the player "
                    "(direction inversion suspected) char=%s op=%s",
                    new_char, character_id, operator_id,
                )
                new_char = old_char

        updated = replace(
            base,
            user_address_name=new_user,
            character_address_name=new_char,
            created_at=base.created_at or when,
            updated_at=when,
        )
        await self._seeds.save(updated)

        if new_user != old_user and new_user:
            await self._record_change(
                character_id, operator_id, DIRECTION_PLAYER,
                old_user, new_user, when, source,
            )
            await self._reconcile_persona_name(
                character_id, operator_id, new_user, when, source,
            )
        if new_char != old_char and new_char:
            await self._record_change(
                character_id, operator_id, DIRECTION_CHARACTER,
                old_char, new_char, when, source,
            )
        return updated

    async def _record_change(
        self,
        character_id: str,
        operator_id: str,
        direction: str,
        old_value: str,
        new_value: str,
        when: datetime,
        source: str = SOURCE_PLAYER_EDIT,
    ) -> None:
        try:
            await self._change_log.record(
                AddressChangeEvent(
                    character_id=character_id,
                    operator_id=operator_id,
                    direction=direction,
                    old_value=old_value,
                    new_value=new_value,
                    source=source,
                    effective_at=when,
                ),
            )
        except Exception:
            # The seed is already saved; a failed audit row must not undo
            # the player's edit or fail the request.
            _LOGGER.exception(
                "address change log write failed (char=%s op=%s dir=%s)",
                character_id, operator_id, direction,
            )

    async def _reconcile_persona_name(
        self,
        character_id: str,
        operator_id: str,
        new_value: str,
        when: datetime,
        source: str = SOURCE_PLAYER_EDIT,
    ) -> None:
        """Align the learned persona ``name`` with the new address name so
        the chat prompt doesn't render the new seed line beside a stale
        learned name. Reuses the persona service's supersede-then-insert;
        fail-soft so a persona hiccup never blocks the seed edit.

        A chat-``observed`` change writes the persona name at a lower
        confidence and never retires a deliberate settings edit; a
        ``player_edit`` is authoritative."""
        if self._persona_service is None:
            return
        try:
            await self._persona_service.set_explicit_field_for_operator(
                character_id=character_id,
                operator_id=operator_id,
                field_key="name",
                value=new_value,
                observed=(source == SOURCE_OBSERVED),
                now=when,
            )
        except Exception:
            _LOGGER.exception(
                "persona name reconcile failed (char=%s op=%s)",
                character_id, operator_id,
            )
