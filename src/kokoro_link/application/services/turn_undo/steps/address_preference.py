"""TU5 — roll back how the character addresses the player.

The player writes 「叫我森森」, the post-turn extractor emits an
``AddressChangeSignal``, ``ChatService._apply_observed_address_changes``
routes it through ``RelationshipNamesService.update_names`` (source
``observed``), which does **three** things: writes the new salutation
onto the ``character_operator_relationship_seed`` row
(``user_address_name`` / ``character_address_name``), appends one
``operator_address_change_log`` event per changed direction, and — for
the player direction only — reconciles the learned persona ``name``
field to the same value. The player undoes the turn — and the character
keeps calling them 森森, with the ``observed`` log entry still sitting
there to explain why.

That seed + change-log pair, not ``OperatorAddressPreference``, is what
actually moves per turn: ``OperatorAddressPreference`` (the salutation /
formality / length row TU1's journal snapshots as
``prev_address_preference``) is written only by the dream-pass tail stage
on its own multi-hour cooldown (``AddressPreferenceObserverService``,
driven from ``PersonaDreamService``) — never from a single turn's
post-turn pipeline. So this step does two independent things:

1. **The real fix.** Ask ``deps.address_change_log`` for every
   ``observed`` entry at/after ``journal.turn_started_at`` for this
   ``(character_id, operator_id)`` pair — deleting them in the same call
   (``delete_observed_since``) so there is no separate list-then-delete
   race. For each direction that comes back, write the seed field back
   to that event's ``old_value``.
2. **The persona half of the same rename.** The player direction also
   moved the learned persona ``name`` (supersede-then-insert through
   ``OperatorPersonaService.set_explicit_field_for_operator``), and
   nothing else reverses it: ``PersonaEvidenceRejectStep`` matches rows
   by ``conversation_id``, and that write stamps the *literal string*
   ``"persona_player_edit"`` into its ``EvidenceRef``, which no real
   conversation id can equal. Left standing it is not merely a stale
   value — ``resolve_player_address`` reads
   ``seed.user_address_name > persona layer1 name > profile.display_name``,
   so on the commonest shape of this bug (the player naming themselves
   for the *first* time, whose ``old_value`` is the empty string) the
   seed restore writes the seed back to empty and the resolver falls
   straight through to the persona row — the character still says 森森
   after the undo. Reversed here through
   ``operator_persona.revert_field_write_since``, keyed on the very
   value the deleted log entry says was written, so a dream pass that
   touched the same field in the same window is left alone.
3. **The snapshot TU1 already built.** If ``journal.prev_address_preference``
   is present *and the live row has drifted from it*, upsert it back
   verbatim. Almost always a no-op (the dream pass rarely lands inside a
   single turn's window), but it is the contract TU1's skeleton promised
   and costs nothing to honour. The drift check is what keeps the
   ordinary turn from writing — and, worse, from *reporting* — a
   restore of a subsystem it never touched.

``journal.prev_address_preference`` being ``None`` is genuinely ambiguous
(no row / no operator / subsystem unwired) and must never be read as
license to delete a current row — nothing here does that; part 2 only
ever upserts.
"""

from __future__ import annotations

import logging
from dataclasses import replace as dataclass_replace

from kokoro_link.application.services.turn_journal_snapshots import (
    address_preference_from_dict,
)
from kokoro_link.application.services.turn_undo.result import UndoTally
from kokoro_link.application.services.turn_undo.step import (
    UndoContext, UndoStep,
)
from kokoro_link.contracts.operator_persona import ADDRESS_NAME_FIELD_KEY
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.address_change_event import (
    DIRECTION_CHARACTER, DIRECTION_PLAYER, AddressChangeEvent,
)

_LOGGER = logging.getLogger(__name__)


class AddressPreferenceRestoreStep(UndoStep):
    name = "address-preference"

    async def apply(self, context: UndoContext, tally: UndoTally) -> None:
        journal = context.journal
        deps = context.deps
        character = await deps.characters.get(journal.character_id)
        if character is None:
            return
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID

        reverted_rename = await self._revert_observed_rename(
            context, tally, character_id=journal.character_id,
            operator_id=operator_id,
        )
        restored_pref = await self._restore_address_preference_snapshot(
            context, character_id=journal.character_id,
        )
        if reverted_rename or restored_pref:
            tally.restored_address_preference = True

    async def _revert_observed_rename(
        self, context: UndoContext, tally: UndoTally,
        *, character_id: str, operator_id: str,
    ) -> bool:
        """Undo the per-turn ``RelationshipNamesService`` rename this
        turn's post-turn extractor may have made."""
        deps = context.deps
        # Only the log is load-bearing here: it is both the record of
        # what the turn renamed and the ``old_value`` every restore is
        # written from. The seed store and the persona store each guard
        # themselves below, so a deployment missing one of them still
        # gets the other half reversed.
        if deps.address_change_log is None:
            return False
        try:
            deleted = await deps.address_change_log.delete_observed_since(
                character_id=character_id, operator_id=operator_id,
                since=context.journal.turn_started_at,
            )
        except Exception:
            _LOGGER.exception(
                "undo: address change log revert failed character=%s",
                character_id,
            )
            return False
        if not deleted:
            return False
        tally.reverted_address_log_entries = len(deleted)

        # Multiple events for the same direction inside one turn's window
        # would be unusual (one post-turn pass emits at most one signal
        # per direction), but the oldest one's ``old_value`` is the true
        # pre-turn value if it somehow happens; a later one would only be
        # restating an intra-turn value.
        oldest_by_direction: dict[str, AddressChangeEvent] = {}
        for event in sorted(
            deleted,
            key=lambda e: e.effective_at or context.journal.turn_started_at,
        ):
            oldest_by_direction.setdefault(event.direction, event)

        player_event = oldest_by_direction.get(DIRECTION_PLAYER)
        character_event = oldest_by_direction.get(DIRECTION_CHARACTER)

        await self._restore_seed_names(
            context, character_id=character_id, operator_id=operator_id,
            player_event=player_event, character_event=character_event,
        )
        # Independent of the seed write above, and deliberately not
        # nested under it: the persona row was written by a different
        # subsystem in the same logical operation, so a seed store that
        # is unwired or raising must not decide whether the persona half
        # gets reversed.
        if player_event is not None:
            await self._revert_persona_name(
                context, character_id=character_id, operator_id=operator_id,
                written_value=player_event.new_value,
            )
        return True

    async def _restore_seed_names(
        self, context: UndoContext, *, character_id: str, operator_id: str,
        player_event: AddressChangeEvent | None,
        character_event: AddressChangeEvent | None,
    ) -> None:
        """Write each renamed direction back to its event's ``old_value``."""
        seeds = context.deps.relationship_seeds
        if seeds is None:
            return
        updates: dict[str, str] = {}
        if player_event is not None:
            updates["user_address_name"] = player_event.old_value
        if character_event is not None:
            updates["character_address_name"] = character_event.old_value
        if not updates:
            return
        try:
            seed = await seeds.get(character_id, operator_id)
        except Exception:
            _LOGGER.exception(
                "undo: relationship seed lookup failed character=%s",
                character_id,
            )
            return
        if seed is None:
            return
        try:
            await seeds.save(
                dataclass_replace(seed, updated_at=context.now, **updates),
            )
        except Exception:
            _LOGGER.exception(
                "undo: relationship seed restore failed character=%s",
                character_id,
            )

    async def _revert_persona_name(
        self, context: UndoContext, *, character_id: str, operator_id: str,
        written_value: str,
    ) -> None:
        """Reverse ``_reconcile_persona_name``'s supersede-then-insert.

        ``written_value`` is the deleted log entry's ``new_value`` — the
        exact string the reconcile pushed into the persona row. Matching
        on it rather than on the time window alone is what stops an
        unrelated dream-pass write to the same key inside the same
        window from being rolled back with the rename.

        ``getattr`` rather than a direct call because the persona
        repository is one of the optional dependencies: a deployment can
        run with it absent, and a store that predates this method must
        report "nothing reversed" the same way an absent one does rather
        than raising through a step whose contract is to stay quiet.
        """
        repository = context.deps.operator_persona
        if repository is None or not written_value:
            return
        revert = getattr(repository, "revert_field_write_since", None)
        if revert is None:
            return
        try:
            await revert(
                character_id=character_id,
                operator_id=operator_id,
                field_key=ADDRESS_NAME_FIELD_KEY,
                value=written_value,
                since=context.journal.turn_started_at,
            )
        except Exception:
            _LOGGER.exception(
                "undo: persona name revert failed character=%s",
                character_id,
            )

    async def _restore_address_preference_snapshot(
        self, context: UndoContext, *, character_id: str,
    ) -> bool:
        """TU1's own contract: upsert ``prev_address_preference`` back
        verbatim when the journal captured one. Almost always a no-op in
        production (see module docstring), kept for the cases where the
        dream pass really did land inside this turn's window.

        Gated on the row having actually moved, the same way the scene
        step gates on ``current.is_open``. On an ordinary turn the
        snapshot and the live row are identical, and writing it back
        would be a redundant write that the result then reports to the
        player as "the address preference was restored" — a claim about
        a subsystem this turn never touched. A read that raises, or a
        store that hands the row back in a shape that cannot compare
        equal, degrades to the unconditional upsert."""
        deps = context.deps
        if deps.address_preferences is None:
            return False
        snapshot = context.journal.prev_address_preference
        if snapshot is None:
            return False
        try:
            restored = address_preference_from_dict(snapshot)
        except Exception:
            _LOGGER.exception(
                "undo: address preference snapshot decode failed "
                "character=%s",
                character_id,
            )
            return False
        try:
            current = await deps.address_preferences.get(
                character_id=restored.character_id,
                operator_id=restored.operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "undo: address preference lookup failed character=%s",
                character_id,
            )
            current = None
        if current == restored:
            return False
        try:
            await deps.address_preferences.upsert(restored)
        except Exception:
            _LOGGER.exception(
                "undo: address preference snapshot restore failed "
                "character=%s",
                character_id,
            )
            return False
        return True
