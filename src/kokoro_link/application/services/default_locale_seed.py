"""Deploy-time locale seed for the single-user default operator.

Wired into the FastAPI lifespan startup hook (`api/app.py`). The operator
picks an interface/content language and a timezone at install time (the
self-host installer writes ``USER_PRIMARY_LANGUAGE`` / ``USER_TIMEZONE``
into ``.env.container``); this seed stamps those onto the still-
unconfigured ``default`` operator row so a fresh single-machine install
comes up in the operator's language and local time — UI chrome, LLM
content, schedules, and "today" boundaries — without anyone hunting for
the in-app switcher or staying stuck on UTC.

Why a boot seed instead of the entity defaults: ``OperatorProfile.default``
is hard-coded to ``zh-TW`` / ``UTC`` and the row is created by an alembic
migration, so the env can't reach it through construction. Seeding at boot
lets every read path (``/auth/me`` for the SPA chrome, the background
schedulers that load the operator directly, civil-time rendering) agree on
one language and timezone.

Idempotent + immutable-safe by construction: the underlying
``set_default_locale_if_unconfigured`` repository call only writes fields
that differ and only ``WHERE password_hash IS NULL``, so a real
``/auth/setup`` (which pins both alongside credentials) is never
overwritten, and a steady-state restart writes nothing.

Fail-soft: a repository error logs a warning and returns ``False`` rather
than crashing startup — the deployment still boots on the previously
persisted values and the operator can adjust in-app.
"""

from __future__ import annotations

import logging

from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort


_LOGGER = logging.getLogger(__name__)


async def seed_default_locale(
    repository: OperatorProfileRepositoryPort,
    *,
    language: str | None = None,
    timezone_id: str | None = None,
) -> bool:
    """Apply the deploy-time default language/timezone to the unconfigured
    default operator. Return ``True`` only when this call wrote a change.

    ``language`` / ``timezone_id`` come from settings (already normalised
    by their loaders). Both empty short-circuits so deployments that never
    set either env log no noise.
    """
    lang = language if (language and language.strip()) else None
    tz = timezone_id if (timezone_id and timezone_id.strip()) else None
    if lang is None and tz is None:
        return False

    try:
        updated = await repository.set_default_locale_if_unconfigured(
            primary_language=lang,
            timezone_id=tz,
        )
    except Exception as exc:  # repo failure must not block startup
        _LOGGER.warning("default locale seed: failed: %s", exc)
        return False

    if updated is None:
        return False

    _LOGGER.info(
        "default locale seed: default operator set to language=%s "
        "timezone=%s from USER_PRIMARY_LANGUAGE / USER_TIMEZONE",
        updated.primary_language,
        updated.timezone_id,
    )
    return True
