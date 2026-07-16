"""Dream-time relationship-coherence self-healing (first detector).

Runs as a best-effort tail stage of the persona dream pass, once per
``(character_id, operator_id)`` pair. It cleans up address/identity
contamination that slips past the write-side guards or predates them —
the recurring failure where a *direction-B* term (how the player
addresses the character: 兄妹喊「哥哥」、情侶喊「老公」、或直接喊角色名)
leaks into a *direction-A* slot (how the character should address the
player).

Pipeline (LLM-first, no keyword denylists — structural collection only):

1. Collect **authoritative facts**: the relationship seed (direction A /
   B truth + ``confirmed_by_user``), the latest rename-log entry per
   direction, the character's own name, the operator's display name +
   aliases, and a windowed slice of the **raw conversation transcript**
   (first-hand evidence — memory is the derived layer that gets dirty).
2. Collect **suspect data**: confirmed persona ``name`` / ``nickname``
   rows, the observed ``salutation``, and recent memories carrying an
   operator participant reference.
3. Hand both to a high-reasoning detector, which returns a structured
   :class:`CoherenceRepairPlan` (never free text). The transcript lets it
   adjudicate *which derived value is dirty* and tell a legitimate recent
   re-address apart from contamination; it must not mine the transcript
   to invent new names.
4. Apply the plan under strict invariants:
   - persona reversals go through the persona service's supersede path —
     **never** writes the global operator profile;
   - a contaminated salutation is cleared (aligned to the seed);
   - a contaminated memory has its salience lowered and its operator
     participant display name reconciled — content free-text is never
     rewritten, the row is never auto-deleted;
   - a ``confirmed_by_user`` seed is never mutated (seed is truth);
   - repairs are capped per run and every stage is fail-soft.
"""

from __future__ import annotations

import logging

from kokoro_link.contracts.relationship_coherence import (
    CoherenceFacts,
    CoherenceSuspects,
    CoherenceTranscriptTurn,
    MemoryRepair,
    PersonaFieldRepair,
    RelationshipCoherenceDetectorPort,
    SalutationRepair,
    SuspectMemory,
    SuspectPersonaField,
)
from kokoro_link.domain.entities.conversation import MessageRole
from kokoro_link.domain.value_objects.actor import ParticipantRef

_LOGGER = logging.getLogger(__name__)

_IDENTITY_FIELD_KEYS = ("name", "nickname")
_DEFAULT_MAX_REPAIRS = 8
_DEFAULT_TRANSCRIPT_WINDOW = 24
_DEFAULT_MEMORY_WINDOW = 20


class RelationshipCoherenceService:
    def __init__(
        self,
        *,
        detector: RelationshipCoherenceDetectorPort,
        persona_service,
        seed_repository,
        change_log_repository=None,
        character_repository=None,
        operator_profile_service=None,
        address_preference_repository=None,
        memory_repository=None,
        conversation_repository=None,
        max_repairs_per_run: int = _DEFAULT_MAX_REPAIRS,
        transcript_window: int = _DEFAULT_TRANSCRIPT_WINDOW,
        memory_window: int = _DEFAULT_MEMORY_WINDOW,
    ) -> None:
        self._detector = detector
        self._persona_service = persona_service
        self._seeds = seed_repository
        self._change_log = change_log_repository
        self._characters = character_repository
        self._profiles = operator_profile_service
        self._preferences = address_preference_repository
        self._memories = memory_repository
        self._conversations = conversation_repository
        self._max_repairs = max(1, int(max_repairs_per_run))
        self._transcript_window = max(0, int(transcript_window))
        self._memory_window = max(0, int(memory_window))

    async def heal_pair(self, character_id: str, operator_id: str) -> None:
        """Run one coherence pass. Best-effort and idempotent: any failure
        is swallowed so the dream pass always completes."""
        try:
            facts = await self._collect_facts(character_id, operator_id)
            suspects = await self._collect_suspects(character_id, operator_id)
        except Exception:
            _LOGGER.exception(
                "coherence: fact/suspect collection failed (char=%s op=%s)",
                character_id, operator_id,
            )
            return

        try:
            plan = await self._detector.detect(facts=facts, suspects=suspects)
        except Exception:
            _LOGGER.exception(
                "coherence: detector raised (char=%s op=%s)",
                character_id, operator_id,
            )
            return

        if plan is None or plan.is_empty():
            return

        applied = 0
        applied += await self._apply_persona_repairs(
            character_id, operator_id, facts, suspects,
            plan.persona_field_repairs, budget=self._max_repairs - applied,
        )
        if plan.salutation_repair is not None and applied < self._max_repairs:
            if await self._apply_salutation_repair(
                character_id, operator_id, facts, suspects,
                plan.salutation_repair,
            ):
                applied += 1
        applied += await self._apply_memory_repairs(
            character_id, operator_id, suspects,
            plan.memory_repairs, budget=self._max_repairs - applied,
        )

    # ---- collection ------------------------------------------------------

    async def _collect_facts(
        self, character_id: str, operator_id: str,
    ) -> CoherenceFacts:
        seed = await self._safe(self._seeds.get(character_id, operator_id))
        character = None
        if self._characters is not None:
            character = await self._safe(self._characters.get(character_id))
        profile = None
        if self._profiles is not None:
            profile = await self._safe(self._profiles.get_for_user(operator_id))
        latest_player = await self._latest_rename(
            character_id, operator_id, "player",
        )
        latest_character = await self._latest_rename(
            character_id, operator_id, "character",
        )
        transcript = await self._collect_transcript(character_id)
        return CoherenceFacts(
            seed_user_address_name=_attr(seed, "user_address_name"),
            seed_character_address_name=_attr(seed, "character_address_name"),
            seed_confirmed_by_user=bool(
                getattr(seed, "confirmed_by_user", True),
            ) if seed is not None else True,
            character_name=_attr(character, "name"),
            operator_display_name=_attr(profile, "display_name"),
            operator_aliases=tuple(getattr(profile, "aliases", ()) or ()),
            latest_rename_player_direction=_attr(latest_player, "new_value"),
            latest_rename_character_direction=_attr(
                latest_character, "new_value",
            ),
            recent_transcript=transcript,
        )

    async def _collect_transcript(
        self, character_id: str,
    ) -> tuple[CoherenceTranscriptTurn, ...]:
        if self._conversations is None or self._transcript_window <= 0:
            return ()
        messages = await self._safe(
            self._conversations.recent_messages_for_character(
                character_id, limit=self._transcript_window,
            ),
        )
        if not messages:
            return ()
        turns: list[CoherenceTranscriptTurn] = []
        for msg in messages:
            role = getattr(msg, "role", None)
            if role not in (MessageRole.USER, MessageRole.ASSISTANT):
                continue
            content = (getattr(msg, "content", "") or "").strip()
            if not content:
                continue
            turns.append(
                CoherenceTranscriptTurn(role=role.value, content=content[:500]),
            )
        return tuple(turns[-self._transcript_window:])

    async def _collect_suspects(
        self, character_id: str, operator_id: str,
    ) -> CoherenceSuspects:
        persona_fields = await self._collect_persona_suspects(
            character_id, operator_id,
        )
        salutation = ""
        if self._preferences is not None:
            pref = await self._safe(
                self._preferences.get(
                    character_id=character_id, operator_id=operator_id,
                ),
            )
            salutation = _attr(pref, "salutation")
        memories = await self._collect_memory_suspects(character_id, operator_id)
        return CoherenceSuspects(
            persona_fields=persona_fields,
            observed_salutation=salutation,
            memories=memories,
        )

    async def _collect_persona_suspects(
        self, character_id: str, operator_id: str,
    ) -> tuple[SuspectPersonaField, ...]:
        persona = await self._safe(
            self._persona_service.get_current(character_id, operator_id),
        )
        if persona is None:
            return ()
        out: list[SuspectPersonaField] = []
        layer1 = getattr(persona, "layer1_identity", {}) or {}
        for key in _IDENTITY_FIELD_KEYS:
            fld = layer1.get(key)
            if fld is None or not getattr(fld, "field_id", None):
                continue
            out.append(
                SuspectPersonaField(
                    field_id=fld.field_id,
                    field_key=fld.field_key,
                    value=fld.value,
                    source=fld.source,
                    confidence=fld.confidence,
                ),
            )
        return tuple(out)

    async def _collect_memory_suspects(
        self, character_id: str, operator_id: str,
    ) -> tuple[SuspectMemory, ...]:
        if self._memories is None or self._memory_window <= 0:
            return ()
        items = await self._safe(
            self._memories.list_all_for_character(
                character_id, world_scope=None,
            ),
        )
        if not items:
            return ()
        out: list[SuspectMemory] = []
        for item in items[: self._memory_window]:
            op_names = tuple(
                p.display_name
                for p in getattr(item, "participants", ())
                if getattr(p, "actor_kind", None) == "operator"
                and _matches_operator(p, operator_id)
            )
            if not op_names:
                continue
            out.append(
                SuspectMemory(
                    memory_id=item.id,
                    content=(item.content or "")[:400],
                    salience=float(item.salience),
                    operator_participant_names=op_names,
                ),
            )
        return tuple(out)

    async def _latest_rename(
        self, character_id: str, operator_id: str, direction: str,
    ):
        if self._change_log is None:
            return None
        return await self._safe(
            self._change_log.latest(
                character_id=character_id,
                operator_id=operator_id,
                direction=direction,
            ),
        )

    # ---- appliers --------------------------------------------------------

    async def _apply_persona_repairs(
        self,
        character_id: str,
        operator_id: str,
        facts: CoherenceFacts,
        suspects: CoherenceSuspects,
        repairs: tuple[PersonaFieldRepair, ...],
        *,
        budget: int,
    ) -> int:
        if budget <= 0 or not repairs:
            return 0
        by_id = {f.field_id: f for f in suspects.persona_fields}
        applied = 0
        for repair in repairs:
            if applied >= budget:
                break
            suspect = by_id.get(repair.field_id)
            if suspect is None:
                # The detector referenced a row we didn't surface — do not
                # trust an unverifiable id.
                continue
            # Structural double-check: the value must actually collide with
            # the cited direction-B / character-name authority. This keeps a
            # miscalibrated model from retiring a legitimate direction-A name.
            if not _persona_value_is_contaminated(suspect.value, facts):
                continue
            try:
                await self._persona_service.transition_field_state_for_operator(
                    suspect.field_id, "superseded", operator_id,
                )
                applied += 1
            except Exception:
                _LOGGER.exception(
                    "coherence: persona supersede failed (char=%s field=%s)",
                    character_id, suspect.field_id,
                )
        if applied:
            try:
                self._persona_service.invalidate_cache(character_id, operator_id)
            except Exception:  # pragma: no cover - defensive
                _LOGGER.exception("coherence: persona cache invalidate failed")
        return applied

    async def _apply_salutation_repair(
        self,
        character_id: str,
        operator_id: str,
        facts: CoherenceFacts,
        suspects: CoherenceSuspects,
        repair: SalutationRepair,
    ) -> bool:
        if self._preferences is None:
            return False
        observed = suspects.observed_salutation
        if not observed:
            return False
        # Structural double-check: only clear when the observed salutation
        # actually collides with a direction-A authority (user address name
        # or operator's own name/alias) — a genuine direction-B term stays.
        if not _salutation_is_contaminated(observed, facts):
            return False
        try:
            pref = await self._preferences.get(
                character_id=character_id, operator_id=operator_id,
            )
            if pref is None:
                return False
            # Clear the salutation (align to seed) without touching the
            # register bands or evidence. ``with_updates`` treats ``None``
            # as "leave alone", so pass an explicit empty string via replace.
            from dataclasses import replace

            cleared = replace(pref, salutation="")
            await self._preferences.upsert(cleared)
            return True
        except Exception:
            _LOGGER.exception(
                "coherence: salutation clear failed (char=%s op=%s)",
                character_id, operator_id,
            )
            return False

    async def _apply_memory_repairs(
        self,
        character_id: str,
        operator_id: str,
        suspects: CoherenceSuspects,
        repairs: tuple[MemoryRepair, ...],
        *,
        budget: int,
    ) -> int:
        if budget <= 0 or self._memories is None or not repairs:
            return 0
        known_ids = {m.memory_id for m in suspects.memories}
        applied = 0
        for repair in repairs:
            if applied >= budget:
                break
            if repair.memory_id not in known_ids:
                continue
            if await self._apply_one_memory_repair(
                character_id, operator_id, repair,
            ):
                applied += 1
        return applied

    async def _apply_one_memory_repair(
        self, character_id: str, operator_id: str, repair: MemoryRepair,
    ) -> bool:
        try:
            item = await self._memories.get(repair.memory_id)
        except Exception:
            _LOGGER.exception(
                "coherence: memory fetch failed (id=%s)", repair.memory_id,
            )
            return False
        if item is None:
            return False
        new_salience = _clamp01(repair.lower_salience_to)
        # Only ever lower salience, never raise it.
        if new_salience > item.salience:
            new_salience = item.salience
        participants = _reconcile_participants(
            getattr(item, "participants", ()),
            operator_id=operator_id,
            corrected_name=repair.reconcile_participant_to.strip(),
        )
        try:
            # content stays None — free-text is never rewritten; the row is
            # never deleted.
            await self._memories.update_fields(
                repair.memory_id,
                salience=new_salience,
                participants=participants,
            )
            return True
        except TypeError:
            # Repository predates the ``participants`` kwarg — fall back to
            # salience-only so the down-rank still lands.
            try:
                await self._memories.update_fields(
                    repair.memory_id, salience=new_salience,
                )
                return True
            except Exception:
                _LOGGER.exception(
                    "coherence: memory salience update failed (id=%s)",
                    repair.memory_id,
                )
                return False
        except Exception:
            _LOGGER.exception(
                "coherence: memory update failed (id=%s)", repair.memory_id,
            )
            return False

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    async def _safe(awaitable):
        try:
            return await awaitable
        except Exception:
            _LOGGER.exception("coherence: source read failed")
            return None


def _attr(obj, name: str) -> str:
    value = getattr(obj, name, "") if obj is not None else ""
    return value if isinstance(value, str) else ""


def _norm(value: str) -> str:
    return (value or "").strip().casefold()


def _persona_value_is_contaminated(value: str, facts: CoherenceFacts) -> bool:
    """A persona name/nickname is contaminated when it structurally
    matches a direction-B authority (how the player addresses the
    character) or the character's own name — i.e. it describes the wrong
    direction. Exact strip+casefold, no fuzzy matching."""
    target = _norm(value)
    if not target:
        return False
    authorities = (
        facts.seed_character_address_name,
        facts.character_name,
        facts.latest_rename_character_direction,
    )
    return any(target == _norm(a) for a in authorities if a)


def _salutation_is_contaminated(value: str, facts: CoherenceFacts) -> bool:
    """An observed salutation (direction B) is contaminated when it
    matches a direction-A authority: the seed user-address-name, the
    operator's display name, or an operator alias."""
    target = _norm(value)
    if not target:
        return False
    authorities = [
        facts.seed_user_address_name,
        facts.operator_display_name,
        facts.latest_rename_player_direction,
    ]
    authorities.extend(facts.operator_aliases)
    return any(target == _norm(a) for a in authorities if a)


def _matches_operator(participant, operator_id: str) -> bool:
    actor_id = getattr(participant, "actor_id", None)
    # When the participant carries no id we still treat an operator-kind
    # ref as belonging to the current operator (single-operator installs).
    return actor_id is None or actor_id == operator_id


def _reconcile_participants(
    participants, *, operator_id: str, corrected_name: str,
) -> tuple[ParticipantRef, ...]:
    """Return participants with the operator ref's display name rewritten
    to ``corrected_name``. Structured field only — never touches content.
    When there's no corrected name, participants are returned unchanged so
    the repair degrades to a pure salience down-rank."""
    if not corrected_name:
        return tuple(participants)
    from dataclasses import replace

    out: list[ParticipantRef] = []
    for p in participants:
        if (
            getattr(p, "actor_kind", None) == "operator"
            and _matches_operator(p, operator_id)
            and _norm(p.display_name) != _norm(corrected_name)
        ):
            out.append(replace(p, display_name=corrected_name))
        else:
            out.append(p)
    return tuple(out)


def _clamp01(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v
