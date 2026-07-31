"""AuthSettings.from_env — BOOTSTRAP_ADMIN_* parsing.

Other AuthSettings fields are covered indirectly by the integration
tests that boot the full container; this module zooms in on the new
first-run-bootstrap env vars so a regression there can't slip past
unit-only CI.
"""

from __future__ import annotations

import pytest

from kokoro_link.bootstrap.settings import AuthSettings


def test_bootstrap_admin_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("KOKORO_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("KOKORO_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    auth = AuthSettings.from_env()
    assert auth.bootstrap_admin_email == ""
    assert auth.bootstrap_admin_password == ""


def test_bootstrap_admin_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "  admin@example.com  ")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "hunter2")
    auth = AuthSettings.from_env()
    # Whitespace stripped on email so the operator can quote loosely.
    # Password passed through verbatim — leading / trailing spaces in
    # a password are a (bad but) legitimate operator choice.
    assert auth.bootstrap_admin_email == "admin@example.com"
    assert auth.bootstrap_admin_password == "hunter2"


def test_bootstrap_admin_legacy_kokoro_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy ``KOKORO_BOOTSTRAP_ADMIN_*`` is accepted as fallback so a
    deployment migrating off the old env-var convention doesn't lose
    its automation in the gap."""
    monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("KOKORO_BOOTSTRAP_ADMIN_EMAIL", "legacy@example.com")
    monkeypatch.setenv("KOKORO_BOOTSTRAP_ADMIN_PASSWORD", "old-secret")
    auth = AuthSettings.from_env()
    assert auth.bootstrap_admin_email == "legacy@example.com"
    assert auth.bootstrap_admin_password == "old-secret"


def test_bootstrap_admin_new_env_takes_precedence_over_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "new@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "new-secret")
    monkeypatch.setenv("KOKORO_BOOTSTRAP_ADMIN_EMAIL", "legacy@example.com")
    monkeypatch.setenv("KOKORO_BOOTSTRAP_ADMIN_PASSWORD", "old-secret")
    auth = AuthSettings.from_env()
    assert auth.bootstrap_admin_email == "new@example.com"
    assert auth.bootstrap_admin_password == "new-secret"


# ----------------------------------------------------------------------
# Sliding-renewal absolute cap
# ----------------------------------------------------------------------


def test_absolute_ttl_defaults_to_unbounded_for_self_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-host renewal is unbounded — a single-machine owner should never be
    signed out on a timer just because they kept the tab open."""
    monkeypatch.delenv("JWT_ABSOLUTE_TTL_SECONDS", raising=False)
    monkeypatch.delenv("KOKORO_JWT_ABSOLUTE_TTL_SECONDS", raising=False)

    assert AuthSettings.from_env().jwt_absolute_ttl_seconds == 0


def test_absolute_ttl_reads_env_and_accepts_the_legacy_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KOKORO_JWT_ABSOLUTE_TTL_SECONDS", "604800")
    monkeypatch.delenv("JWT_ABSOLUTE_TTL_SECONDS", raising=False)
    assert AuthSettings.from_env().jwt_absolute_ttl_seconds == 604800

    monkeypatch.setenv("JWT_ABSOLUTE_TTL_SECONDS", "86400")
    assert AuthSettings.from_env().jwt_absolute_ttl_seconds == 86400


def test_negative_absolute_ttl_clamps_to_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative cap would reject every renewal; treat it as "not configured"
    rather than silently bricking sign-in for the whole deployment."""
    monkeypatch.setenv("JWT_ABSOLUTE_TTL_SECONDS", "-1")

    assert AuthSettings.from_env().jwt_absolute_ttl_seconds == 0
