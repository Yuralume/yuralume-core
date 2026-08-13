"""Discord gateway close codes → failure vocabulary.

These are the only tests that pin *which* close codes mean "wrong
credentials" and, separately, which of those are repaired by editing the
stored credentials. The second axis is what keeps close 4014 alive: it
is a credential failure by every logging and UI criterion, but its fix
is a checkbox in the Discord Developer Portal, so the stored token never
changes and a worker that parks "until the credentials change" parks
forever.

The mapping reads numeric close codes only — never message text.
"""

import pytest

from kokoro_link.domain.value_objects.messaging_failure import (
    MessagingCredentialError,
)
from kokoro_link.infrastructure.messaging.discord.gateway_client import (
    _credential_error_for_close,
)


class _Frame:
    def __init__(self, code: int) -> None:
        self.code = code


class _ConnectionClosed(Exception):
    """Shape of a ``websockets`` close, without importing ``websockets``.

    Only ``rcvd.code`` is read, and reading it by attribute is what lets
    the adapter keep its ``websockets`` import deferred into ``connect``.
    """

    def __init__(self, code: int) -> None:
        super().__init__(f"closed {code}")
        self.rcvd = _Frame(code)


def test_authentication_failure_demands_a_credentials_edit() -> None:
    error = _credential_error_for_close(_ConnectionClosed(4004))

    assert isinstance(error, MessagingCredentialError)
    # Only a new token gets past 4004, and that edit is locally
    # observable — so the worker is licensed to stop until it happens.
    assert error.credentials_must_change is True


def test_disallowed_intents_must_not_demand_a_credentials_edit() -> None:
    """4014's repair leaves the token byte-identical, so it cannot park.

    Message Content Intent ships disabled, which makes 4014 the ordinary
    first connection of a new bot rather than an edge case. Stopping on
    it would strand the account for the life of the process, and a
    hosted player has no process to restart.
    """
    error = _credential_error_for_close(_ConnectionClosed(4014))

    assert isinstance(error, MessagingCredentialError)
    assert error.credentials_must_change is False
    # Point at the checkbox, not at the token...
    assert "Message Content Intent" in error.user_message
    # ...and say the part the operator most needs: nothing to restart.
    assert "restart" in error.user_message


def test_the_two_credential_closes_do_not_share_a_message() -> None:
    """Telling a 4014 operator to reset the token sends them nowhere."""
    authentication = _credential_error_for_close(_ConnectionClosed(4004))
    intents = _credential_error_for_close(_ConnectionClosed(4014))

    assert authentication is not None
    assert intents is not None
    assert authentication.user_message != intents.user_message


@pytest.mark.parametrize("code", [1000, 1006, 4000, 4008, 4013])
def test_other_close_codes_stay_transient(code: int) -> None:
    assert _credential_error_for_close(_ConnectionClosed(code)) is None


def test_a_non_close_exception_stays_transient() -> None:
    assert _credential_error_for_close(RuntimeError("handshake failed")) is None


def _real_connection_closed_error(code: int, reason: str = ""):
    """Build a genuine ``websockets`` close exception, not a stand-in.

    ``_close_code`` reads ``exc.rcvd.code`` by attribute specifically so it
    survives a ``websockets`` upgrade without importing the library into
    production code (see the adapter's module docstring). That also means
    nothing here re-checks that the *real* exception still has that shape
    once the library moves on. Pin it with the actual class.

    Construction pinned against ``websockets==15.0.1``:
    ``ConnectionClosedError(rcvd: Close | None, sent: Close | None)``,
    where ``Close(code, reason)`` lives in ``websockets.frames``. If a
    future ``websockets`` version changes this constructor or drops
    ``rcvd``/``rcvd.code``, this test — not just production code — should
    fail, which is the point: it means ``_close_code()`` needs a matching
    update before 4004 silently stops parking bad-credential accounts.
    """
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    return ConnectionClosedError(Close(code, reason), None)


def test_real_websockets_authentication_close_demands_a_credentials_edit() -> None:
    """Same assertion as the hand-rolled 4004 test, against the real class.

    If ``websockets`` ever renames or removes ``ConnectionClosed.rcvd``,
    ``_close_code()`` degrades to returning ``None`` for every close —
    silently turning 4004 into a TRANSIENT failure that retries forever
    instead of parking. The hand-rolled ``_ConnectionClosed`` above cannot
    catch that regression because it fabricates ``.rcvd`` itself; only a
    real ``websockets`` exception can.
    """
    error = _credential_error_for_close(_real_connection_closed_error(4004))

    assert isinstance(error, MessagingCredentialError)
    assert error.credentials_must_change is True


def test_real_websockets_disallowed_intents_close_keeps_retrying() -> None:
    error = _credential_error_for_close(
        _real_connection_closed_error(4014, "disallowed intents"),
    )

    assert isinstance(error, MessagingCredentialError)
    assert error.credentials_must_change is False
    assert "Message Content Intent" in error.user_message
    assert "restart" in error.user_message
