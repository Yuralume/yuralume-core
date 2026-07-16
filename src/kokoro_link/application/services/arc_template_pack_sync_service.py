"""Startup pack sync — YAML pack files → ``arc_templates`` DB rows.

Bundled YAML under ``src/kokoro_link/data/arc_templates/`` ships with
the repo as the canonical source of pack templates. On every app
start this service reads them through :class:`YAMLArcTemplatePackLoader`
and UPSERTs each as a ``user_id IS NULL`` row through
:meth:`ArcTemplateRepositoryPort.upsert_pack` — same shape as
``RssSourceSyncService``.

The sync is intentionally one-way (YAML → DB). User-authored
templates are written exclusively through the intake save endpoint and
are invisible to this service because they have ``user_id`` set.

We do NOT delete DB pack rows whose YAML went missing — disabling
stays an admin operation so a removed pack stops appearing in the
picker without losing the row's history (or breaking any character
that still points at it).
"""

from __future__ import annotations

import logging

from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.infrastructure.story.yaml_arc_template_repository import (
    YAMLArcTemplatePackLoader,
)

_LOGGER = logging.getLogger(__name__)


class ArcTemplatePackSyncService:
    def __init__(
        self,
        *,
        loader: YAMLArcTemplatePackLoader,
        repository: ArcTemplateRepositoryPort,
    ) -> None:
        self._loader = loader
        self._repository = repository

    async def sync(self) -> int:
        """Read every pack YAML and upsert it. Returns rows touched."""
        try:
            entries = self._loader.load_all()
        except Exception:
            _LOGGER.exception("arc template pack sync: loader crashed")
            return 0
        touched = 0
        for entry in entries:
            try:
                await self._repository.upsert_pack(
                    entry.template,
                    pack_id=entry.pack_id,
                    external_id=entry.external_id,
                )
            except ValueError:
                # User-authored row already owns this slug — log + skip.
                # Operator's authored template wins; the pack will start
                # appearing again once they rename or remove their row.
                _LOGGER.warning(
                    "arc template pack sync: slug %s is held by a "
                    "user-authored row, skipping pack upsert",
                    entry.template.id,
                )
                continue
            except Exception:
                _LOGGER.exception(
                    "arc template pack sync: upsert failed for %s",
                    entry.template.id,
                )
                continue
            touched += 1
        return touched


__all__ = ["ArcTemplatePackSyncService"]
