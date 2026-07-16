"""In-memory ``ArcTemplateRepositoryPort`` implementation for tests.

Production runs ``SAArcTemplateRepository`` against the ``arc_templates``
table. Tests that don't want to bring a database up (intake service
unit tests, route fixtures) inject this stub instead — same surface,
zero IO. Behaviour mirrors the SA repo's ownership rules so a passing
unit test implies the same rules hold at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.domain.entities.arc_template import ArcTemplate


@dataclass(slots=True)
class _StoredTemplate:
    template: ArcTemplate
    user_id: str | None
    pack_id: str | None = None
    external_id: str | None = None
    enabled: bool = True


@dataclass(slots=True)
class InMemoryArcTemplateRepository(ArcTemplateRepositoryPort):
    """Dict-backed port impl — pack rows + user rows in one map."""

    _rows: dict[str, _StoredTemplate] = field(default_factory=dict)

    async def get_for_user(
        self, template_id: str, *, user_id: str | None,
    ) -> ArcTemplate | None:
        row = self._rows.get(template_id)
        if row is None or not row.enabled:
            return None
        if row.user_id is None:
            return row.template
        if user_id is not None and row.user_id == user_id:
            return row.template
        return None

    async def list_for_user(
        self, user_id: str | None,
    ) -> list[ArcTemplate]:
        out: list[_StoredTemplate] = []
        for row in self._rows.values():
            if not row.enabled:
                continue
            if row.user_id is None:
                out.append(row)
                continue
            if user_id is not None and row.user_id == user_id:
                out.append(row)
        return [row.template for row in sorted(out, key=lambda r: r.template.id)]

    async def list_packs(self) -> list[ArcTemplate]:
        return [
            r.template
            for r in sorted(self._rows.values(), key=lambda r: r.template.id)
            if r.user_id is None
        ]

    async def save_for_user(
        self,
        template: ArcTemplate,
        *,
        user_id: str,
        overwrite: bool = False,
    ) -> str:
        existing = self._rows.get(template.id)
        if existing is not None:
            if existing.user_id is None:
                raise ValueError(
                    f"Template id {template.id!r} is reserved by a "
                    "bundled pack — choose a different id."
                )
            if existing.user_id != user_id:
                raise ValueError(
                    f"Template id {template.id!r} already exists — "
                    "choose a different id."
                )
            if not overwrite:
                raise ValueError(
                    f"Template id {template.id!r} already exists — "
                    "pass overwrite=True to replace."
                )
            existing.template = template
            return template.id
        self._rows[template.id] = _StoredTemplate(
            template=template, user_id=user_id, enabled=True,
        )
        return template.id

    async def delete_for_user(
        self, template_id: str, *, user_id: str,
    ) -> bool:
        row = self._rows.get(template_id)
        if row is None or row.user_id != user_id:
            return False
        del self._rows[template_id]
        return True

    async def upsert_pack(
        self,
        template: ArcTemplate,
        *,
        pack_id: str,
        external_id: str | None = None,
    ) -> str:
        existing = self._rows.get(template.id)
        if existing is not None and existing.user_id is not None:
            raise ValueError(
                f"Cannot upsert pack {template.id!r}: a user-authored "
                "row already owns this slug."
            )
        self._rows[template.id] = _StoredTemplate(
            template=template,
            user_id=None,
            pack_id=pack_id,
            external_id=external_id,
            enabled=True,
        )
        return template.id


__all__ = ["InMemoryArcTemplateRepository"]
