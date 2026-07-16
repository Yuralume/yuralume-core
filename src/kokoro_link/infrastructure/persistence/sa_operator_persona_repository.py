"""SQLAlchemy-backed ``OperatorPersonaRepositoryPort``.

Per-character: every read / write is keyed on
``(character_id, operator_id)``. A different character's rows live
in the same table but are queried independently — no cross-character
inheritance, so a brand-new character starts at zero observations.

The table mixes staging and confirmed rows; this repo hides that —
``get`` returns an aggregate with confirmed fields sorted into their
layer dicts plus pending candidates as a tuple. Pending fields are
NEVER folded into the layer dicts so callers that only render prompts
can't accidentally inject staging noise.

Idempotency: confirmed fields are upserted by
``(character_id, operator_id, layer, field_key, state)`` because one
confirmed value is injected per key. Pending candidates can carry
several competing values for the same key, so the database uniqueness
also includes ``value`` and the repo merges same-value evidence rows.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.operator_persona import OperatorPersonaRepositoryPort
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    EvidenceRef,
    ProfileField,
)
from kokoro_link.infrastructure.persistence.models import OperatorProfileFieldRow


class SAOperatorPersonaRepository(OperatorPersonaRepositoryPort):
    def __init__(self, session_factory: sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(
        self, character_id: str, operator_id: str,
    ) -> OperatorPersona:
        async with self._session_factory() as session:
            stmt = select(OperatorProfileFieldRow).where(
                OperatorProfileFieldRow.character_id == character_id,
                OperatorProfileFieldRow.operator_id == operator_id,
                OperatorProfileFieldRow.state.in_(
                    ("pending", "confirmed"),
                ),
            )
            result = await session.execute(stmt)
            rows = list(result.scalars())

        layer1: dict[str, ProfileField] = {}
        layer2: dict[str, ProfileField] = {}
        layer3: dict[str, ProfileField] = {}
        layer5: dict[str, ProfileField] = {}
        pending: list[CandidateField] = []
        for row in rows:
            if row.state == "confirmed":
                fld = _row_to_field(row)
                if fld is None:
                    continue
                if fld.layer == 1:
                    layer1[fld.field_key] = fld
                elif fld.layer == 2:
                    layer2[fld.field_key] = fld
                elif fld.layer == 3:
                    layer3[fld.field_key] = fld
                elif fld.layer == 5:
                    layer5[fld.field_key] = fld
            else:  # pending
                cand = _row_to_candidate(row)
                if cand is not None:
                    pending.append(cand)
        pending.sort(key=lambda c: c.extracted_at)
        return OperatorPersona(
            character_id=character_id,
            operator_id=operator_id,
            layer1_identity=layer1,
            layer2_life=layer2,
            layer3_emotional=layer3,
            layer5_trust=layer5,
            layer4_interaction=None,
            pending_candidates=tuple(pending),
        )

    async def upsert_field(
        self,
        character_id: str,
        operator_id: str,
        field: ProfileField,
    ) -> ProfileField:
        now = datetime.now(timezone.utc)
        evidence_payload = json.dumps(
            [ev.to_dict() for ev in field.evidence_refs], ensure_ascii=False,
        )
        async with self._session_factory() as session:
            row = await self._find_row(
                session,
                character_id=character_id,
                operator_id=operator_id,
                layer=field.layer,
                field_key=field.field_key,
                state="confirmed",
            )
            if row is None:
                row_id = field.field_id or _new_id()
                row = OperatorProfileFieldRow(
                    id=row_id,
                    character_id=character_id,
                    operator_id=operator_id,
                    layer=field.layer,
                    field_key=field.field_key,
                    value=field.value,
                    confidence=field.confidence,
                    state="confirmed",
                    source=field.source,
                    content_mode=field.content_mode.value,
                    evidence_json=evidence_payload,
                    update_count=field.update_count,
                    explicit=False,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.value = field.value
                row.confidence = field.confidence
                row.source = field.source
                row.content_mode = _merge_content_mode(
                    row.content_mode,
                    field.content_mode.value,
                )
                row.evidence_json = evidence_payload
                row.update_count = field.update_count
                row.updated_at = now
            await session.commit()
            return _row_to_field(row) or field

    async def upsert_candidate(
        self,
        character_id: str,
        operator_id: str,
        candidate: CandidateField,
    ) -> CandidateField:
        now = datetime.now(timezone.utc)
        evidence_payload = json.dumps(
            [candidate.evidence_ref.to_dict()], ensure_ascii=False,
        )
        async with self._session_factory() as session:
            existing = await self._find_pending_duplicate(
                session,
                character_id=character_id,
                operator_id=operator_id,
                layer=candidate.layer,
                field_key=candidate.field_key,
                value=candidate.proposed_value,
                quote=candidate.evidence_ref.quote,
            )
            if existing is not None:
                existing.update_count = existing.update_count + 1
                existing.confidence = max(
                    existing.confidence, candidate.raw_extractor_confidence,
                )
                existing.content_mode = _merge_content_mode(
                    existing.content_mode,
                    candidate.content_mode.value,
                )
                existing.evidence_json = _append_evidence_json(
                    existing.evidence_json,
                    candidate.evidence_ref,
                )
                existing.updated_at = now
                await session.commit()
                return _row_to_candidate(existing) or candidate
            row_id = candidate.candidate_id or _new_id()
            row = OperatorProfileFieldRow(
                id=row_id,
                character_id=character_id,
                operator_id=operator_id,
                layer=candidate.layer,
                field_key=candidate.field_key,
                value=candidate.proposed_value,
                confidence=candidate.raw_extractor_confidence,
                state=candidate.state,
                source=candidate.source,
                content_mode=candidate.content_mode.value,
                evidence_json=evidence_payload,
                update_count=1,
                explicit=candidate.explicit,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.commit()
            return _row_to_candidate(row) or candidate

    async def list_pending(
        self,
        character_id: str,
        operator_id: str,
        *,
        limit: int = 100,
    ) -> list[CandidateField]:
        async with self._session_factory() as session:
            stmt = (
                select(OperatorProfileFieldRow)
                .where(
                    OperatorProfileFieldRow.character_id == character_id,
                    OperatorProfileFieldRow.operator_id == operator_id,
                    OperatorProfileFieldRow.state == "pending",
                )
                .order_by(OperatorProfileFieldRow.created_at.asc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = list(result.scalars())
        out: list[CandidateField] = []
        for row in rows:
            cand = _row_to_candidate(row)
            if cand is not None:
                out.append(cand)
        return out

    async def count_pending(
        self, character_id: str, operator_id: str,
    ) -> int:
        async with self._session_factory() as session:
            stmt = select(OperatorProfileFieldRow.id).where(
                OperatorProfileFieldRow.character_id == character_id,
                OperatorProfileFieldRow.operator_id == operator_id,
                OperatorProfileFieldRow.state == "pending",
            )
            result = await session.execute(stmt)
            return len(list(result.scalars()))

    async def list_confirmed_for_decay(
        self,
        character_id: str,
        operator_id: str,
        *,
        stale_after_days: int,
    ) -> list[ProfileField]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=stale_after_days)
        async with self._session_factory() as session:
            stmt = select(OperatorProfileFieldRow).where(
                OperatorProfileFieldRow.character_id == character_id,
                OperatorProfileFieldRow.operator_id == operator_id,
                OperatorProfileFieldRow.state == "confirmed",
                OperatorProfileFieldRow.updated_at < cutoff,
            )
            result = await session.execute(stmt)
            rows = list(result.scalars())
        out: list[ProfileField] = []
        for row in rows:
            fld = _row_to_field(row)
            if fld is not None:
                out.append(fld)
        return out

    async def list_characters_with_pending(self) -> list[tuple[str, str]]:
        async with self._session_factory() as session:
            stmt = (
                select(
                    OperatorProfileFieldRow.character_id,
                    OperatorProfileFieldRow.operator_id,
                )
                .where(OperatorProfileFieldRow.state == "pending")
                .distinct()
            )
            result = await session.execute(stmt)
            return [(char_id, op_id) for char_id, op_id in result.all()]

    async def get_row_scope(self, row_id: str) -> tuple[str, str] | None:
        async with self._session_factory() as session:
            row = await session.get(OperatorProfileFieldRow, row_id)
            if row is None:
                return None
            return (row.character_id, row.operator_id)

    async def mark_state(self, candidate_id: str, state: str) -> None:
        await self._mark(candidate_id, state)

    async def mark_field_state(self, field_id: str, state: str) -> None:
        await self._mark(field_id, state)

    async def delete_for_character(self, character_id: str) -> int:
        async with self._session_factory() as session:
            result = await session.execute(
                delete(OperatorProfileFieldRow).where(
                    OperatorProfileFieldRow.character_id == character_id,
                ),
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def reject_evidence_since(
        self,
        *,
        conversation_id: str,
        since,
    ) -> int:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            stmt = select(OperatorProfileFieldRow).where(
                OperatorProfileFieldRow.updated_at >= since,
                OperatorProfileFieldRow.state.in_(("pending", "confirmed")),
            )
            result = await session.execute(stmt)
            changed = 0
            for row in result.scalars():
                if _row_has_evidence_since(
                    row.evidence_json,
                    conversation_id=conversation_id,
                    since=since,
                ):
                    row.state = "rejected"
                    row.updated_at = now
                    changed += 1
            if changed:
                await session.commit()
            return changed

    async def _mark(self, row_id: str, state: str) -> None:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            row = await session.get(OperatorProfileFieldRow, row_id)
            if row is None:
                return
            row.state = state
            row.updated_at = now
            await session.commit()

    async def _find_row(
        self,
        session: AsyncSession,
        *,
        character_id: str,
        operator_id: str,
        layer: int,
        field_key: str,
        state: str,
    ) -> OperatorProfileFieldRow | None:
        stmt = select(OperatorProfileFieldRow).where(
            OperatorProfileFieldRow.character_id == character_id,
            OperatorProfileFieldRow.operator_id == operator_id,
            OperatorProfileFieldRow.layer == layer,
            OperatorProfileFieldRow.field_key == field_key,
            OperatorProfileFieldRow.state == state,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def _find_pending_duplicate(
        self,
        session: AsyncSession,
        *,
        character_id: str,
        operator_id: str,
        layer: int,
        field_key: str,
        value: str,
        quote: str,
    ) -> OperatorProfileFieldRow | None:
        """De-duplicate pending candidates that say the same thing.

        Same key/value remains one staging row even when backed by a
        different quote; evidence is appended so the dream pass can see
        multiple observations. Different values for the same key
        intentionally coexist for conflict / supersede / merge
        reasoning.
        """
        stmt = select(OperatorProfileFieldRow).where(
            OperatorProfileFieldRow.character_id == character_id,
            OperatorProfileFieldRow.operator_id == operator_id,
            OperatorProfileFieldRow.layer == layer,
            OperatorProfileFieldRow.field_key == field_key,
            OperatorProfileFieldRow.state == "pending",
            OperatorProfileFieldRow.value == value,
        )
        result = await session.execute(stmt)
        return result.scalars().first()


def _new_id() -> str:
    return uuid.uuid4().hex


def _row_to_field(row: OperatorProfileFieldRow) -> ProfileField | None:
    evidence = _decode_evidence(row.evidence_json)
    if not evidence:
        return None
    try:
        return ProfileField(
            field_key=row.field_key,
            layer=row.layer,
            value=row.value,
            confidence=row.confidence,
            evidence_refs=tuple(evidence),
            last_updated=row.updated_at,
            update_count=row.update_count,
            source=row.source,
            content_mode=row.content_mode,
            character_id=row.character_id,
            field_id=row.id,
        )
    except ValueError:
        return None


def _row_to_candidate(row: OperatorProfileFieldRow) -> CandidateField | None:
    evidence = _decode_evidence(row.evidence_json)
    if not evidence:
        return None
    try:
        return CandidateField(
            field_key=row.field_key,
            layer=row.layer,
            proposed_value=row.value,
            evidence_ref=evidence[0],
            raw_extractor_confidence=row.confidence,
            state=row.state,
            source=row.source,
            content_mode=row.content_mode,
            candidate_id=row.id,
            extracted_at=row.created_at,
            explicit=row.explicit,
            character_id=row.character_id,
        )
    except ValueError:
        return None


def _decode_evidence(raw: str | None) -> list[EvidenceRef]:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    out: list[EvidenceRef] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        ev = EvidenceRef.from_dict(entry)
        if ev is not None:
            out.append(ev)
    return out


def _append_evidence_json(raw: str | None, evidence: EvidenceRef) -> str:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        payload = []
    if not isinstance(payload, list):
        payload = []
    new_entry = evidence.to_dict()
    for entry in payload:
        if (
            isinstance(entry, dict)
            and entry.get("conversation_id") == new_entry["conversation_id"]
            and entry.get("turn_id") == new_entry["turn_id"]
            and entry.get("quote") == new_entry["quote"]
        ):
            return json.dumps(payload, ensure_ascii=False)
    payload.append(new_entry)
    return json.dumps(payload, ensure_ascii=False)


def _row_has_evidence_since(
    raw: str | None,
    *,
    conversation_id: str,
    since: datetime,
) -> bool:
    for evidence in _decode_evidence(raw):
        if evidence.conversation_id != conversation_id:
            continue
        extracted_at = evidence.extracted_at
        ref = since
        if extracted_at.tzinfo is None and ref.tzinfo is not None:
            extracted_at = extracted_at.replace(tzinfo=timezone.utc)
        if ref.tzinfo is None and extracted_at.tzinfo is not None:
            ref = ref.replace(tzinfo=timezone.utc)
        if extracted_at >= ref:
            return True
    return False


def _merge_content_mode(left: str | None, right: str | None) -> str:
    if (
        _coerce_content_mode(left) is MessageContentMode.NSFW
        or _coerce_content_mode(right) is MessageContentMode.NSFW
    ):
        return MessageContentMode.NSFW.value
    return MessageContentMode.NORMAL.value


def _coerce_content_mode(value: str | None) -> MessageContentMode:
    try:
        return MessageContentMode(str(value or "").strip().lower())
    except ValueError:
        return MessageContentMode.NORMAL
