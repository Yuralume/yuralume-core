"""Port for the story-arc template registry.

Templates used to live as YAML files only and were read-only at runtime.
Containerised builds can't write into the bundled directory, and there
was no per-user authorship — the intake wizard's save would clobber a
shared file other users could see. After migration ``cy0d2e50075`` the
authoritative store is the ``arc_templates`` table:

- Pack rows (``user_id IS NULL``) are upserted from the shipped YAML
  on startup by ``ArcTemplatePackSyncService``. Visible to every user.
- User-authored rows (``user_id = <owner>``) are written by the intake
  save endpoint and only visible to their owner.

The port keeps a string-id surface so ``Character.arc_template_id``
(which stores the slug as a plain string) keeps working without
schema changes on the characters table. The ownership check is the
repository's responsibility: ``get_for_user`` returns the row when it
is either a pack row or owned by ``user_id``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kokoro_link.domain.entities.arc_template import ArcTemplate


class ArcTemplateRepositoryPort(ABC):
    @abstractmethod
    async def get_for_user(
        self, template_id: str, *, user_id: str | None,
    ) -> ArcTemplate | None:
        """Return the template if visible to ``user_id`` (pack or owned).

        Implementations must not raise on missing / not-visible templates
        — service callers fall back to LLM planning when the answer is
        ``None``. ``user_id=None`` is reserved for background paths that
        only need pack templates.
        """

    @abstractmethod
    async def list_for_user(self, user_id: str | None) -> list[ArcTemplate]:
        """All templates visible to ``user_id`` — pack rows plus the
        caller's own rows, in stable id order.

        UI dropdowns consume this list; ordering should be stable so the
        operator's eye-position survives reloads. ``user_id=None``
        returns pack rows only.
        """

    @abstractmethod
    async def list_packs(self) -> list[ArcTemplate]:
        """Pack rows only (``user_id IS NULL``).

        Used by the pack sync service to compare on-disk YAML against
        what's currently in the DB. Disabled rows are still returned
        so the sync can re-enable them when a missing YAML reappears.
        """

    @abstractmethod
    async def save_for_user(
        self,
        template: ArcTemplate,
        *,
        user_id: str,
        overwrite: bool = False,
    ) -> str:
        """Persist a user-authored template.

        ``overwrite=False`` makes an existing-slug collision raise
        ``ValueError`` so the intake wizard can present a "rename or
        overwrite" choice. Slug collision against a pack row always
        raises regardless of ``overwrite`` — pack ids are reserved.
        Returns the slug the caller can echo.
        """

    @abstractmethod
    async def delete_for_user(
        self, template_id: str, *, user_id: str,
    ) -> bool:
        """Remove a user-authored template owned by ``user_id``.

        Returns ``True`` if a row was removed, ``False`` if the slug
        wasn't found or wasn't owned by the caller. Pack rows are
        immutable from this surface — admins manage packs via the YAML
        pack sync.
        """

    @abstractmethod
    async def upsert_pack(
        self,
        template: ArcTemplate,
        *,
        pack_id: str,
        external_id: str | None = None,
    ) -> str:
        """Insert or update a pack row (``user_id IS NULL``).

        Used exclusively by ``ArcTemplatePackSyncService`` during the
        startup YAML → DB upsert. ``pack_id`` is the source filename
        stem; ``external_id`` is the original ``id`` field declared
        inside the YAML when the author overrode it from the stem.
        Idempotent — repeated calls with the same content are no-ops.
        """
