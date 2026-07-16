"""Step 7 — player correction of a learned persona identity field.

The correction must supersede-then-insert (stamp the old confirmed row
``superseded`` *before* writing the new value, mirroring the dream
service) so the unique confirmed constraint never collides and the prior
value survives as history. It must never touch the global profile, and
must reject non-editable fields / empty values.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from kokoro_link.application.services.operator_persona_service import (
    OperatorPersonaService,
)
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.value_objects.profile_field import (
    EvidenceRef,
    ProfileField,
)


def _field(
    field_key: str, value: str, *, field_id: str, source: str = "extraction",
) -> ProfileField:
    return ProfileField(
        character_id="char-A",
        field_key=field_key,
        layer=1,
        value=value,
        confidence=0.8,
        evidence_refs=(
            EvidenceRef(
                turn_id="t1",
                conversation_id="c1",
                quote=value,
                extracted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            ),
        ),
        last_updated=datetime(2026, 6, 1, tzinfo=timezone.utc),
        update_count=2,
        source=source,
        field_id=field_id,
    )


class _FakeRepo:
    def __init__(self, persona: OperatorPersona) -> None:
        self._persona = persona
        self.field_marks: list[tuple[str, str]] = []
        self.upserts: list[ProfileField] = []
        self.order: list[str] = []

    async def get(self, character_id: str, operator_id: str) -> OperatorPersona:
        return self._persona

    async def mark_field_state(self, field_id: str, state: str) -> None:
        self.field_marks.append((field_id, state))
        self.order.append("mark")

    async def upsert_field(self, character_id, operator_id, field):
        self.upserts.append(field)
        self.order.append("upsert")
        return field


def _service(repo: _FakeRepo) -> OperatorPersonaService:
    return OperatorPersonaService(
        repository=repo,
        strength_calculator=None,  # type: ignore[arg-type]
        settings=SimpleNamespace(),  # type: ignore[arg-type]
    )


def test_correction_supersedes_existing_before_insert() -> None:
    persona = OperatorPersona(
        character_id="char-A",
        operator_id="alice",
        layer1_identity={"name": _field("name", "丹尼", field_id="fld-old")},
    )
    repo = _FakeRepo(persona)
    svc = _service(repo)

    result = asyncio.run(
        svc.set_explicit_field_for_operator(
            character_id="char-A",
            operator_id="alice",
            field_key="name",
            value="阿丹",
        )
    )

    # Old confirmed row retired BEFORE the new write (constraint safety +
    # history preserved).
    assert repo.order == ["mark", "upsert"]
    assert repo.field_marks == [("fld-old", "superseded")]
    assert result.value == "阿丹"
    assert result.source == "user_explicit"
    assert result.confidence == 0.95
    assert result.field_id is None  # fresh confirmed row, not the old id


def test_correction_without_existing_just_inserts() -> None:
    persona = OperatorPersona(character_id="char-A", operator_id="alice")
    repo = _FakeRepo(persona)
    svc = _service(repo)

    asyncio.run(
        svc.set_explicit_field_for_operator(
            character_id="char-A",
            operator_id="alice",
            field_key="nickname",
            value="小丹",
        )
    )

    assert repo.field_marks == []  # nothing to supersede
    assert repo.order == ["upsert"]
    assert repo.upserts[0].field_key == "nickname"
    assert repo.upserts[0].value == "小丹"


def test_observed_does_not_retire_user_explicit_row() -> None:
    # A chat-observed rename must NOT overwrite a deliberate settings edit.
    persona = OperatorPersona(
        character_id="char-A",
        operator_id="alice",
        layer1_identity={
            "name": _field("name", "丹尼", field_id="fld-ux", source="user_explicit"),
        },
    )
    repo = _FakeRepo(persona)
    svc = _service(repo)

    result = asyncio.run(
        svc.set_explicit_field_for_operator(
            character_id="char-A",
            operator_id="alice",
            field_key="name",
            value="森森",
            observed=True,
        )
    )

    assert repo.upserts == []  # nothing written
    assert repo.field_marks == []  # the deliberate row is not retired
    assert result.value == "丹尼"  # existing user_explicit preserved


def test_observed_supersedes_a_learned_row_at_lower_confidence() -> None:
    persona = OperatorPersona(
        character_id="char-A",
        operator_id="alice",
        layer1_identity={
            "name": _field("name", "丹尼", field_id="fld-ex", source="extraction"),
        },
    )
    repo = _FakeRepo(persona)
    svc = _service(repo)

    result = asyncio.run(
        svc.set_explicit_field_for_operator(
            character_id="char-A",
            operator_id="alice",
            field_key="name",
            value="森森",
            observed=True,
        )
    )

    assert repo.order == ["mark", "upsert"]  # learned row retired + new write
    assert result.value == "森森"
    assert result.source == "extraction"  # observed, not user_explicit
    assert result.confidence == 0.85  # below a deliberate edit


def test_correction_rejects_non_editable_field() -> None:
    repo = _FakeRepo(OperatorPersona(character_id="char-A", operator_id="alice"))
    svc = _service(repo)
    with pytest.raises(ValueError):
        asyncio.run(
            svc.set_explicit_field_for_operator(
                character_id="char-A",
                operator_id="alice",
                field_key="secrets",  # layer 3, not editable
                value="x",
            )
        )
    assert repo.upserts == []


def test_correction_rejects_empty_value() -> None:
    repo = _FakeRepo(OperatorPersona(character_id="char-A", operator_id="alice"))
    svc = _service(repo)
    with pytest.raises(ValueError):
        asyncio.run(
            svc.set_explicit_field_for_operator(
                character_id="char-A",
                operator_id="alice",
                field_key="name",
                value="   ",
            )
        )
    assert repo.upserts == []
