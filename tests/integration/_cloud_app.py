"""Build the app in cloud mode for route-level integration tests.

``AppSettings.from_env`` fails closed when cloud mode is on without a
``DATABASE_URL``: Cloud Core has no safe in-memory persistence mode, so the
env-driven boot path refuses to start (``bootstrap/settings.py``). That red
line is deliberate and is pinned by ``tests/unit/test_env_loading.py`` — it
is not what the tests here are about.

These route tests exercise the cloud *auth / session surface* and predate the
gate; they run on in-memory repositories on purpose. The gate's own docstring
sanctions the escape they need ("manual ``AppSettings`` construction remains
available to explicit unit tests"), and this helper takes it in the least
divergent way available: settings are still loaded by the real
``from_env``—with a placeholder DSN in place so the gate is satisfied—and only
``database_url`` is blanked afterwards.

Overriding that one field, rather than assembling ``AppSettings`` by hand, is
what keeps the coupling intact: cloud mode does not just set ``cloud``, it
also rewrites ``auth`` (forces it enabled, swaps in the cloud session TTLs,
clears the bootstrap admin). A hand-built settings object silently loses that
and the fixture ends up testing a shape production never boots.
"""

from __future__ import annotations

import os
from dataclasses import replace
from unittest.mock import patch

from fastapi import FastAPI

from kokoro_link.api.app import create_app
from kokoro_link.bootstrap.settings import AppSettings

# Never connected to: it exists only so the cloud-mode gate in ``from_env``
# sees a database, and it is blanked out of the returned settings before the
# container is built (``use_database`` is ``bool(database_url)``).
_PLACEHOLDER_DSN = "postgresql+asyncpg://tests:tests@localhost:5432/tests"


def cloud_settings_without_database() -> AppSettings:
    """Cloud-mode settings resolved from env, on in-memory persistence."""
    with patch.dict(os.environ, {"DATABASE_URL": _PLACEHOLDER_DSN}):
        settings = AppSettings.from_env()
    if not settings.cloud.active:
        raise AssertionError(
            "cloud env is not configured for this test; set "
            "YURALUME_CLOUD_ENABLED=true (and the cloud URLs/credentials) "
            "before calling this helper",
        )
    return replace(settings, database_url="")


def create_cloud_app() -> FastAPI:
    """``create_app`` in cloud mode without requiring a real database."""
    return create_app(cloud_settings_without_database())
