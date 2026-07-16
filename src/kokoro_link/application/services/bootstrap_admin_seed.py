"""First-run admin bootstrap from environment variables.

Wired into the FastAPI lifespan startup hook (`api/app.py`). When the
operator sets ``BOOTSTRAP_ADMIN_EMAIL`` + ``BOOTSTRAP_ADMIN_PASSWORD``
in ``.env`` / ``.env.container`` and the default user still has no
credentials, attach them in a single ``setup_initial_admin`` call so
the deployment can be driven by config-as-code instead of a manual
``/auth/setup`` browser round-trip.

Idempotent by construction: the underlying ``set_default_password_if_unset``
repository call uses a ``WHERE password_hash IS NULL`` predicate, so a
second container start finds ``needs_setup() == False`` and bails out
before any DB write. The env vars then become no-ops and the operator
can safely leave them in their config or delete them — both are fine.

Fail-soft: bad values (empty after strip, invalid email, race with
manual setup) log a warning and return ``False`` instead of crashing
the startup. Auth itself stays usable; the operator can still run
``/auth/setup`` by hand.
"""

from __future__ import annotations

import logging

from kokoro_link.application.exceptions import (
    InvalidCredentials,
    SetupAlreadyComplete,
    SetupNotAllowed,
)
from kokoro_link.application.services.auth_service import AuthService


_LOGGER = logging.getLogger(__name__)


async def seed_bootstrap_admin(
    auth_service: AuthService,
    *,
    email: str,
    password: str,
) -> bool:
    """Attempt to seed the default-admin credentials. Return ``True``
    only when the seed actually wrote new credentials this call.

    ``email`` / ``password`` come from settings (already whitespace-
    trimmed for the email; password is passed through as the operator
    typed it). Empty values short-circuit — env vars left unset are
    the common case and must not log noise on every cold start.
    """
    if not email or not password.strip():
        return False

    try:
        if not await auth_service.needs_setup():
            return False
    except Exception as exc:  # repo failure shouldn't block startup
        _LOGGER.warning("bootstrap admin: needs_setup probe failed: %s", exc)
        return False

    try:
        await auth_service.setup_initial_admin(email=email, password=password)
    except SetupAlreadyComplete:
        # Race with a manual /auth/setup that landed between the probe
        # and the write — the operator already has working credentials,
        # no further action needed.
        return False
    except (InvalidCredentials, SetupNotAllowed) as exc:
        _LOGGER.warning(
            "bootstrap admin: setup rejected (%s) — fix env or run "
            "/auth/setup manually",
            exc,
        )
        return False
    except Exception as exc:  # last-resort fail-soft
        _LOGGER.warning("bootstrap admin: unexpected failure: %s", exc)
        return False

    _LOGGER.info(
        "bootstrap admin: seeded default user from BOOTSTRAP_ADMIN_* env"
    )
    return True
