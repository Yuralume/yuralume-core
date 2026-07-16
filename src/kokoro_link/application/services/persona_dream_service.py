"""The "dream job" — periodic consolidation of operator persona staging.

Triggered from ``ProactiveScheduler._tick_all`` during quiet hours
when there's enough pending material. Runs **per (character_id,
operator_id) pair** — each character dreams about its own
accumulated observations independently. Sharing one dream pass
across characters would leak fact promotions across the perceptual
boundary the per-character pivot exists to enforce.

Why quiet hours: the dream pass is expensive (one LLM call per
character with the entire staging buffer in-context); running it
during active chat windows competes with the chat path for tokens.
Quiet hours also fit the metaphor — the character is "thinking
about you while you sleep".

Apply semantics:

- Each action is best-effort. A single failure (e.g. promote tries
  to write a field whose unique key is already taken) is logged and
  skipped; the rest of the batch still applies.
- After every successful promote / merge / supersede / decay we
  invalidate the persona service cache so the next prompt sees the
  new state immediately.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from kokoro_link.bootstrap.settings import PersonaSettings
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.operator_persona import OperatorPersonaRepositoryPort
from kokoro_link.contracts.persona_consolidator import (
    ConsolidationResult,
    DecayAction,
    InferAction,
    MergeAction,
    PersonaConsolidatorPort,
    PromoteAction,
    SupersedeAction,
)
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    EvidenceRef,
    ProfileField,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.value_objects.timezone import timezone_for_id

from kokoro_link.application.services.operator_persona_service import (
    OperatorPersonaService,
)
from kokoro_link.application.services.operator_profile_service import (
    OperatorProfileService,
)
from kokoro_link.application.services.behavioral_pattern_service import (
    BehavioralPatternObserverService,
)
from kokoro_link.application.services.cloud_identity_context import (
    cloud_actor_scope,
)
from kokoro_link.application.services.disposition_drift_service import (
    DispositionDriftService,
)
from kokoro_link.application.services.relationship_milestone_service import (
    RelationshipMilestoneService,
)
from kokoro_link.application.services.self_reflection_service import (
    SelfReflectionService,
)

_LOGGER = logging.getLogger(__name__)


class PersonaDreamService:
    def __init__(
        self,
        *,
        consolidator: PersonaConsolidatorPort,
        repository: OperatorPersonaRepositoryPort,
        persona_service: OperatorPersonaService,
        settings: PersonaSettings,
        operator_profile_service: OperatorProfileService | None = None,
        relationship_milestone_service: "RelationshipMilestoneService | None" = None,
        behavioral_pattern_service: "BehavioralPatternObserverService | None" = None,
        character_repository=None,  # noqa: ANN001 - optional, only for name lookup
        clock: ClockPort | None = None,
    ) -> None:
        self._consolidator = consolidator
        self._repository = repository
        self._persona_service = persona_service
        self._settings = settings
        # Optional dependency retained for backwards-compatible
        # construction, but per-character persona facts no longer sync
        # into the global OperatorProfile. A name learned by one
        # character may be a roleplay alias or local nickname; copying
        # it to every character collapses the per-character social
        # boundary this feature exists to protect.
        self._operator_profile_service = operator_profile_service
        # Optional tail-stage observer (HUMANIZATION_ROADMAP §3.5).
        # Runs after consolidation so the Familiarity-band lookup sees
        # the most up-to-date persona snapshot.
        self._relationship_milestone_service = relationship_milestone_service
        # Behavioural pattern observer (HUMANIZATION_ROADMAP §3.3) —
        # statistics-only schedule recurrences + LLM-based phrase habits.
        # Per-character, runs in the same tail stage as relationship
        # milestones; operator id is irrelevant for behavioural patterns
        # (they describe the character, not the pair).
        self._behavioral_pattern_service = behavioral_pattern_service
        self._character_repository = character_repository
        self._clock = clock
        # Self-reflection service (HUMANIZATION_ROADMAP §3.2) — dream-
        # time first-person narrative across 7 / 30 day windows. Wired
        # via setter for the same forward-reference reason as the
        # behavioural observer above.
        self._self_reflection_service: SelfReflectionService | None = None
        # Disposition drift service (HUMANIZATION_ROADMAP §3.1).
        self._disposition_drift_service: DispositionDriftService | None = None
        # Optional quiet-hours service (HUMANIZATION_ROADMAP §4.5) —
        # when set, ``should_run_now`` reads the DB-stored window so
        # the operator can edit quiet hours without redeploy.
        self._quiet_hours_service = None
        # Address preference observer (HUMANIZATION_ROADMAP §4.2) —
        # observed register / address style refresh in the dream tail.
        self._address_preference_service = None
        # Relationship coherence self-heal — repairs address/identity
        # contamination (direction inversions) in the derived stores.
        # Runs after the address preference refresh so it sees the freshest
        # observed salutation. Best-effort, never blocks the dream pass.
        self._relationship_coherence_service = None
        # Optional priority gate (HUMANIZATION_ROADMAP §4.5) — when set,
        # the dream pass acquires a DREAM-priority slot before the LLM
        # call so chat / proactive paths can preempt during quiet hours
        # if they happen to fire on the same tick.
        self._priority_gate = None
        # Per (character_id, operator_id) — each character's dream
        # cycle is independent.
        self._last_run_at: dict[tuple[str, str], datetime] = {}

    def set_behavioral_pattern_service(
        self, service: "BehavioralPatternObserverService | None",
    ) -> None:
        """Late-bind the behavioural pattern observer.

        Wiring order in :mod:`bootstrap.container` builds the dream
        service early (it depends on the persona repo which is on the
        primary engine) while the observer depends on later-built
        repositories (turn-record / behavioural-pattern, on the
        observability engine). The setter avoids forward-reference
        gymnastics — the container instantiates the dream service
        first, then calls this once the observer is ready.
        """
        self._behavioral_pattern_service = service

    def set_character_repository(self, repo) -> None:  # noqa: ANN001 - duck-typed
        """Late-bind the character repository (see ``set_behavioral_pattern_service``)."""
        self._character_repository = repo

    def set_self_reflection_service(
        self, service: "SelfReflectionService | None",
    ) -> None:
        """Late-bind the §3.2 self-reflection service."""
        self._self_reflection_service = service

    def set_disposition_drift_service(
        self, service: "DispositionDriftService | None",
    ) -> None:
        """Late-bind the §3.1 disposition drift service."""
        self._disposition_drift_service = service

    def set_quiet_hours_service(self, service) -> None:  # noqa: ANN001
        """Late-bind the §4.5 ``QuietHoursService``.

        When set, the dream pass reads the quiet window from the
        runtime settings DB (admin-editable) instead of the env-only
        settings. When unset (legacy tests / dev fakes) the original
        ``settings.dream_quiet_hours_*`` env path is used.
        """
        self._quiet_hours_service = service

    def set_address_preference_service(self, service) -> None:  # noqa: ANN001
        """Late-bind the §4.2 ``AddressPreferenceObserverService``.

        Same wiring shape as the §3.x observers — set by the container
        after both the dream service and the address-preference repo
        are built. The dream-pass tail calls
        :meth:`observe_recent_for_pair` so the observed register
        accumulates without an extra per-turn LLM call.
        """
        self._address_preference_service = service

    def set_relationship_coherence_service(self, service) -> None:  # noqa: ANN001
        """Late-bind the relationship-coherence self-heal service.

        Same wiring shape as the other dream tail observers — set by the
        container once the coherence service and its sources (seed repo,
        memory repo, address-preference repo, conversation repo) are built.
        The dream tail calls :meth:`heal_pair` after the address-preference
        refresh so the coherence detector sees the freshest observed
        salutation. Best-effort — a failure never rolls back the dream pass.
        """
        self._relationship_coherence_service = service

    def set_priority_gate(self, gate) -> None:  # noqa: ANN001
        """Late-bind the §4.5 :class:`LLMSerialisationGate`.

        Setter pattern matches the §3.x observers — when the gate is
        wired, the dream pass acquires DREAM priority around the
        consolidator call so a concurrent chat / proactive request
        preempts cleanly. When unset (legacy tests), the pass runs
        without a priority handshake.
        """
        self._priority_gate = gate

    async def should_run_now(
        self,
        character_id: str,
        operator_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """All three conditions must hold for the given pair:

        - currently in operator-local quiet hours
        - at least ``dream_min_pending`` pending candidates queued
          *for this character*
        - at least ``dream_min_interval_hours`` since the last run
          *of this character*

        Cheap by design — only the pending count touches the DB.

        ``operator_id`` is the character owner's user id under the
        multi-user rename (see MULTI_USER_AUTH_PLAN), so passing it
        through to the quiet-hours service lets each user keep their
        own dream-quiet window instead of sharing one installation
        default.
        """
        if not await self._is_quiet_now(user_id=operator_id, now=now):
            return False
        key = (character_id, operator_id)
        last = self._last_run_at.get(key)
        ref = self._resolve_now(now)
        if last is not None:
            elapsed = ref - last
            if elapsed.total_seconds() < self._settings.dream_min_interval_hours * 3600:
                return False
        try:
            pending_count = await self._repository.count_pending(
                character_id, operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "count_pending failed; skipping dream tick for %s/%s",
                character_id, operator_id,
            )
            return False
        return pending_count >= self._settings.dream_min_pending

    async def run_consolidation(
        self,
        character_id: str,
        operator_id: str,
        *,
        now: datetime | None = None,
    ) -> ConsolidationResult:
        """Execute one dream pass for ``(character_id, operator_id)``.

        Returns the (potentially empty) :class:`ConsolidationResult`
        the LLM produced, AFTER attempting to apply each action so
        callers / tests can inspect what happened.
        """
        # Background tick — no HTTP request scope to inherit. Bind the
        # ambient cloud actor so the leaf extractors this fans out to
        # (disposition drift, phrase-habit, address observer) resolve cloud
        # identity without each taking an operator_id parameter.
        with cloud_actor_scope(operator_id=operator_id):
            return await self._run_consolidation_inner(
                character_id, operator_id, now=now,
            )

    async def _run_consolidation_inner(
        self,
        character_id: str,
        operator_id: str,
        *,
        now: datetime | None = None,
    ) -> ConsolidationResult:
        ref = self._resolve_now(now)
        try:
            persona = await self._persona_service.get_current(
                character_id, operator_id,
            )
            pending = await self._repository.list_pending(
                character_id, operator_id, limit=64,
            )
            decay = await self._repository.list_confirmed_for_decay(
                character_id, operator_id,
                stale_after_days=self._settings.decay_after_days,
            )
            pending = _without_sensitive_candidates(pending)
            decay = _without_sensitive_fields(decay)
            plan = await self._consolidate_with_priority(
                persona=persona,
                pending=pending,
                decay=decay,
            )
        except Exception:
            _LOGGER.exception(
                "Persona consolidation prep failed for %s/%s",
                character_id, operator_id,
            )
            return ConsolidationResult()

        pending_index = {c.candidate_id: c for c in pending if c.candidate_id}
        decay_index = {f.field_id: f for f in decay if f.field_id}

        applied_any = False
        for promo in plan.promotions:
            applied_any = (
                await self._apply_promote(
                    character_id, operator_id, promo, pending_index, ref,
                )
                or applied_any
            )
        for merge in plan.merges:
            applied_any = (
                await self._apply_merge(
                    character_id, operator_id, merge, pending_index, ref,
                )
                or applied_any
            )
        for supersede in plan.supersedes:
            applied_any = (
                await self._apply_supersede(
                    character_id, operator_id, supersede, pending_index, ref,
                )
                or applied_any
            )
        for reject in plan.rejections:
            await self._safe_mark(reject.candidate_id, "rejected")
        for decay_action in plan.decays:
            applied_any = (
                await self._apply_decay(
                    character_id, operator_id, decay_action, decay_index, ref,
                )
                or applied_any
            )
        for infer in plan.inferences:
            applied_any = (
                await self._apply_infer(
                    character_id, operator_id, infer, ref,
                )
                or applied_any
            )

        self._last_run_at[(character_id, operator_id)] = ref
        if applied_any:
            self._persona_service.invalidate_cache(character_id, operator_id)

        # HUMANIZATION_ROADMAP §3.5 — observe Familiarity band crossings
        # *after* consolidation so the band lookup reflects any layer-4
        # changes this pass produced. Best-effort: a failure here must
        # not roll back the consolidation plan we just applied.
        if self._relationship_milestone_service is not None:
            try:
                await self._relationship_milestone_service.check_and_emit(
                    character_id, operator_id, now=ref,
                )
            except Exception:
                _LOGGER.exception(
                    "relationship_milestone tail stage failed (char=%s op=%s)",
                    character_id, operator_id,
                )

        # HUMANIZATION_ROADMAP §3.2 — self-reflection over 7/30 day
        # windows. Persona snippets feed the LLM the relationship
        # context; we use the rendered prompt lines the chat path also
        # sees so reflection language stays consistent with chat voice.
        if self._self_reflection_service is not None:
            try:
                # Use the persona service's render so the reflection
                # generator sees the same thresholded snippets the chat
                # prompt sees. ``render_for_prompt`` is sync.
                persona_lines = tuple(
                    self._persona_service.render_for_prompt(persona)
                    if persona is not None else ()
                )
                character_name = ""
                if self._character_repository is not None:
                    try:
                        char_obj = await self._character_repository.get(
                            character_id,
                        )
                        if char_obj is not None:
                            character_name = char_obj.name
                    except Exception:
                        _LOGGER.exception(
                            "self_reflection: character name lookup failed",
                        )
                await self._self_reflection_service.run_for_pair(
                    character_id,
                    operator_id,
                    character_name=character_name,
                    persona_summary_lines=persona_lines,
                    now=ref,
                )
            except Exception:
                _LOGGER.exception(
                    "self_reflection tail stage failed (char=%s op=%s)",
                    character_id, operator_id,
                )

        # HUMANIZATION_ROADMAP §3.1 — disposition drift. Runs after
        # reflection so the judge sees fresh state. Persona snippets
        # already computed above.
        if self._disposition_drift_service is not None:
            try:
                persona_lines = tuple(
                    self._persona_service.render_for_prompt(persona)
                    if persona is not None else ()
                )
                await self._disposition_drift_service.run_for_character(
                    character_id,
                    operator_id=operator_id,
                    persona_summary_lines=persona_lines,
                    now=ref,
                )
            except Exception:
                _LOGGER.exception(
                    "disposition_drift tail stage failed (char=%s)",
                    character_id,
                )

        # HUMANIZATION_ROADMAP §3.3 — behavioural patterns are per-
        # character (not per-pair); run the observer once per dream
        # tick. Best-effort, errors absorbed.
        if self._behavioral_pattern_service is not None:
            try:
                character_name = ""
                local_tz = await self._operator_timezone(operator_id)
                if self._character_repository is not None:
                    try:
                        char_obj = await self._character_repository.get(
                            character_id,
                        )
                        character_name = (
                            char_obj.name if char_obj is not None else ""
                        )
                        if char_obj is not None:
                            local_tz = await self._operator_timezone(
                                getattr(char_obj, "user_id", None),
                            )
                    except Exception:
                        _LOGGER.exception(
                            "behavioral_pattern: character name lookup failed",
                        )
                await self._behavioral_pattern_service.observe_for_character(
                    character_id,
                    character_name=character_name,
                    now=ref,
                    local_tz=local_tz,
                )
            except Exception:
                _LOGGER.exception(
                    "behavioral_pattern tail stage failed (char=%s)",
                    character_id,
                )

        # HUMANIZATION_ROADMAP §4.2 — observed register / address
        # preference refresh. Best-effort, never blocks the dream pass.
        if self._address_preference_service is not None:
            try:
                await self._address_preference_service.observe_recent_for_pair(
                    character_id=character_id,
                    operator_id=operator_id,
                )
            except Exception:
                _LOGGER.exception(
                    "address_preference tail stage failed (char=%s op=%s)",
                    character_id,
                    operator_id,
                )

        # Relationship coherence self-heal — runs last so it sees the
        # freshest observed salutation the address-preference refresh may
        # have just written. Best-effort; the service is itself fail-soft,
        # but wrap defensively so a bug there can never break the dream pass.
        if self._relationship_coherence_service is not None:
            try:
                await self._relationship_coherence_service.heal_pair(
                    character_id, operator_id,
                )
            except Exception:
                _LOGGER.exception(
                    "relationship_coherence tail stage failed (char=%s op=%s)",
                    character_id,
                    operator_id,
                )

        return plan

    # ---- action appliers -------------------------------------------------

    async def _apply_promote(
        self,
        character_id: str,
        operator_id: str,
        action: PromoteAction,
        pending_index: dict,
        now: datetime,
    ) -> bool:
        cand = pending_index.get(action.candidate_id)
        if cand is None:
            return False
        evidence = (cand.evidence_ref,)
        field = self._safe_field(
            character_id=character_id,
            operator_id=operator_id,
            field_key=action.field_key,
            layer=action.layer,
            value=action.value,
            confidence=action.new_confidence,
            evidence=evidence,
            now=now,
            source=cand.source if cand.source else "extraction",
            update_count=max(1, cand.evidence_ref is not None),
            content_mode=cand.content_mode,
        )
        if field is None:
            return False
        try:
            await self._repository.upsert_field(
                character_id, operator_id, field,
            )
            await self._repository.mark_state(action.candidate_id, "promoted")
            await self._maybe_sync_operator_display_name(
                field_key=action.field_key,
                layer=action.layer,
                value=action.value,
                new_confidence=action.new_confidence,
            )
            return True
        except Exception:
            _LOGGER.exception(
                "promote failed (char=%s op=%s field_key=%s)",
                character_id, operator_id, action.field_key,
            )
            return False

    async def _apply_merge(
        self,
        character_id: str,
        operator_id: str,
        action: MergeAction,
        pending_index: dict,
        now: datetime,
    ) -> bool:
        evidences: list[EvidenceRef] = []
        sources: list[str] = []
        content_modes: list[MessageContentMode] = []
        for cid in action.candidate_ids:
            cand = pending_index.get(cid)
            if cand is None:
                continue
            evidences.append(cand.evidence_ref)
            sources.append(cand.source or "extraction")
            content_modes.append(cand.content_mode)
        if not evidences:
            return False
        source = "user_explicit" if "user_explicit" in sources else "extraction"
        field = self._safe_field(
            character_id=character_id,
            operator_id=operator_id,
            field_key=action.field_key,
            layer=action.layer,
            value=action.value,
            confidence=action.new_confidence,
            evidence=tuple(evidences),
            now=now,
            source=source,
            update_count=len(evidences),
            content_mode=_merge_content_modes(content_modes),
        )
        if field is None:
            return False
        try:
            await self._repository.upsert_field(
                character_id, operator_id, field,
            )
            for cid in action.candidate_ids:
                await self._safe_mark(cid, "promoted")
            await self._maybe_sync_operator_display_name(
                field_key=action.field_key,
                layer=action.layer,
                value=action.value,
                new_confidence=action.new_confidence,
            )
            return True
        except Exception:
            _LOGGER.exception(
                "merge failed (char=%s op=%s field_key=%s)",
                character_id, operator_id, action.field_key,
            )
            return False

    async def _apply_supersede(
        self,
        character_id: str,
        operator_id: str,
        action: SupersedeAction,
        pending_index: dict,
        now: datetime,
    ) -> bool:
        evidences = []
        sources = []
        content_modes: list[MessageContentMode] = []
        for cid in action.candidate_ids:
            cand = pending_index.get(cid)
            if cand is None:
                continue
            evidences.append(cand.evidence_ref)
            sources.append(cand.source or "extraction")
            content_modes.append(cand.content_mode)
        if not evidences:
            return False
        source = "user_explicit" if "user_explicit" in sources else "extraction"
        new_field = self._safe_field(
            character_id=character_id,
            operator_id=operator_id,
            field_key=action.field_key,
            layer=action.layer,
            value=action.new_value,
            confidence=action.new_confidence,
            evidence=tuple(evidences),
            now=now,
            source=source,
            update_count=len(evidences),
            content_mode=_merge_content_modes(content_modes),
        )
        if new_field is None:
            return False
        try:
            # Order matters: stamp the old row first so the unique
            # (character_id, operator_id, layer, field_key,
            # state='confirmed') constraint doesn't collide with the
            # new write.
            await self._repository.mark_field_state(
                action.superseded_field_id, "superseded",
            )
            await self._repository.upsert_field(
                character_id, operator_id, new_field,
            )
            for cid in action.candidate_ids:
                await self._safe_mark(cid, "promoted")
            await self._maybe_sync_operator_display_name(
                field_key=action.field_key,
                layer=action.layer,
                value=action.new_value,
                new_confidence=action.new_confidence,
            )
            return True
        except Exception:
            _LOGGER.exception(
                "supersede failed (char=%s op=%s field_key=%s)",
                character_id, operator_id, action.field_key,
            )
            return False

    async def _apply_decay(
        self,
        character_id: str,
        operator_id: str,
        action: DecayAction,
        decay_index: dict,
        now: datetime,
    ) -> bool:
        existing = decay_index.get(action.field_id)
        if existing is None:
            return False
        age = (now - existing.last_updated).days
        if age >= self._settings.stale_after_days:
            await self._safe_mark_field(action.field_id, "stale")
            return True
        try:
            updated = ProfileField(
                field_key=existing.field_key,
                layer=existing.layer,
                value=existing.value,
                confidence=action.new_confidence,
                evidence_refs=existing.evidence_refs,
                last_updated=now,
                update_count=existing.update_count,
                source=existing.source,
                content_mode=existing.content_mode,
                character_id=existing.character_id,
                field_id=existing.field_id,
            )
        except ValueError:
            return False
        try:
            await self._repository.upsert_field(
                character_id, operator_id, updated,
            )
            return True
        except Exception:
            _LOGGER.exception(
                "decay failed (char=%s op=%s field_id=%s)",
                character_id, operator_id, action.field_id,
            )
            return False

    async def _apply_infer(
        self,
        character_id: str,
        operator_id: str,
        action: InferAction,
        now: datetime,
    ) -> bool:
        # Inference has no per-quote evidence — synthesise a single
        # housekeeping evidence row that points at the reasoning string
        # so the audit trail still has *something* tying the field
        # back to its source.
        try:
            evidence = EvidenceRef(
                turn_id=f"dream:{uuid.uuid4().hex}",
                conversation_id="dream",
                quote=action.reason[:240] if action.reason else "dream inference",
                extracted_at=now,
            )
        except ValueError:
            return False
        field = self._safe_field(
            character_id=character_id,
            operator_id=operator_id,
            field_key=action.field_key,
            layer=action.layer,
            value=action.value,
            confidence=action.new_confidence,
            evidence=(evidence,),
            now=now,
            source="dream_inference",
            update_count=1,
            content_mode=MessageContentMode.NORMAL,
        )
        if field is None:
            return False
        try:
            await self._repository.upsert_field(
                character_id, operator_id, field,
            )
            return True
        except Exception:
            _LOGGER.exception(
                "infer failed (char=%s op=%s field_key=%s)",
                character_id, operator_id, action.field_key,
            )
            return False

    def _safe_field(
        self,
        *,
        character_id: str,
        operator_id: str,
        field_key: str,
        layer: int,
        value: str,
        confidence: float,
        evidence: tuple[EvidenceRef, ...],
        now: datetime,
        source: str,
        update_count: int,
        content_mode: MessageContentMode | str = MessageContentMode.NORMAL,
    ) -> ProfileField | None:
        try:
            return ProfileField(
                field_key=field_key,
                layer=layer,
                value=value,
                confidence=confidence,
                evidence_refs=evidence,
                last_updated=now,
                update_count=max(1, update_count),
                source=source,
                content_mode=content_mode,
                character_id=character_id,
            )
        except ValueError:
            _LOGGER.warning(
                "constructed invalid ProfileField (char=%s op=%s field_key=%s)",
                character_id, operator_id, field_key,
            )
            return None

    async def _maybe_sync_operator_display_name(
        self,
        *,
        field_key: str,
        layer: int,
        value: str,
        new_confidence: float,
    ) -> None:
        """No-op by design.

        Per-character learned names stay in the persona table. The
        global OperatorProfile is reserved for operator-declared data
        through the profile UI/API, otherwise one character's local
        nickname can leak into every other character's prompt.
        """
        return

    async def _safe_mark(self, candidate_id: str, state: str) -> None:
        try:
            await self._repository.mark_state(candidate_id, state)
        except Exception:
            _LOGGER.exception(
                "mark_state failed (candidate=%s state=%s)", candidate_id, state,
            )

    async def _safe_mark_field(self, field_id: str, state: str) -> None:
        try:
            await self._repository.mark_field_state(field_id, state)
        except Exception:
            _LOGGER.exception(
                "mark_field_state failed (field=%s state=%s)", field_id, state,
            )

    async def _consolidate_with_priority(self, *, persona, pending, decay):
        """Wrap the consolidator call in a DREAM-priority gate when wired.

        Mirrors the §4.5 "infrastructure ordering, not feature toggle"
        rule: the gate doesn't change *what* the LLM sees, only *when*
        the request runs relative to higher-priority chat traffic.
        """
        if self._priority_gate is None:
            return await self._consolidator.consolidate(
                persona=persona,
                pending=pending,
                decay_candidates=decay,
            )
        from kokoro_link.infrastructure.llm.priority_gate import (
            LLMRequestPriority,
        )
        async with self._priority_gate.acquire(LLMRequestPriority.DREAM):
            return await self._consolidator.consolidate(
                persona=persona,
                pending=pending,
                decay_candidates=decay,
            )

    async def _is_quiet_now(
        self,
        *,
        user_id: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Quiet-hours predicate with runtime-mutable bounds (§4.5).

        Prefers ``QuietHoursService`` when wired so the per-user window
        (user override → global default → env) decides each tick. Falls
        back to the env-driven ``settings.dream_quiet_hours_*`` only
        when the service was never injected (legacy / unit-test paths).
        """
        if self._quiet_hours_service is not None:
            local_tz = await self._operator_timezone(user_id)
            return await self._quiet_hours_service.in_quiet_hours(
                user_id=user_id, now=now, local_tz=local_tz,
            )
        return self._in_quiet_hours(now=now)

    async def _operator_timezone(self, user_id: str | None):
        if not user_id or self._operator_profile_service is None:
            return timezone.utc
        try:
            operator = await self._operator_profile_service.get_for_user(user_id)
            return timezone_for_id(getattr(operator, "timezone_id", None))
        except Exception:  # pragma: no cover - defensive
            return timezone.utc

    def _in_quiet_hours(self, *, now: datetime | None = None) -> bool:
        start = self._settings.dream_quiet_hours_start
        end = self._settings.dream_quiet_hours_end
        hour = self._resolve_hour(now)
        if start <= end:
            return start <= hour < end
        # Window wraps midnight (e.g. 23..7).
        return hour >= start or hour < end

    def _resolve_hour(self, now: datetime | None) -> int:
        """Legacy env fallback used only when QuietHoursService is unwired.

        Production quiet-hours checks pass through ``_is_quiet_now`` with
        the owner timezone. This fallback keeps old tests/dev fakes
        deterministic by using UTC instead of the host's local timezone.
        """
        ref = self._resolve_now(now)
        return ref.hour

    def _resolve_now(self, now: datetime | None) -> datetime:
        return ensure_utc(
            now if now is not None else (
                self._clock.now()
                if self._clock is not None
                else datetime.now(timezone.utc)
            ),
        )


def _without_sensitive_candidates(
    candidates: list[CandidateField],
) -> list[CandidateField]:
    return [
        candidate
        for candidate in candidates
        if candidate.content_mode is not MessageContentMode.NSFW
    ]


def _without_sensitive_fields(fields: list[ProfileField]) -> list[ProfileField]:
    return [
        field
        for field in fields
        if field.content_mode is not MessageContentMode.NSFW
    ]


def _merge_content_modes(
    content_modes: list[MessageContentMode],
) -> MessageContentMode:
    if any(mode is MessageContentMode.NSFW for mode in content_modes):
        return MessageContentMode.NSFW
    return MessageContentMode.NORMAL
