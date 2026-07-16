"""Bcrypt + fake hasher round-trip + edge cases."""

from __future__ import annotations

import pytest

from kokoro_link.infrastructure.security.password_hasher import (
    BcryptPasswordHasher,
    FakePasswordHasher,
)


def test_fake_hasher_round_trip() -> None:
    hasher = FakePasswordHasher()
    h = hasher.hash("hunter2")
    assert hasher.verify("hunter2", h) is True
    assert hasher.verify("wrong", h) is False


def test_fake_hasher_rejects_empty() -> None:
    hasher = FakePasswordHasher()
    with pytest.raises(ValueError):
        hasher.hash("")
    with pytest.raises(ValueError):
        hasher.hash("   ")


def test_fake_hasher_verify_handles_blank_inputs() -> None:
    hasher = FakePasswordHasher()
    h = hasher.hash("hunter2")
    assert hasher.verify("", h) is False
    assert hasher.verify("hunter2", "") is False


def test_bcrypt_hasher_round_trip() -> None:
    hasher = BcryptPasswordHasher()
    h = hasher.hash("hunter2")
    # bcrypt hashes are opaque + non-deterministic (random salt) — two
    # hashes of the same password must differ but both verify.
    h2 = hasher.hash("hunter2")
    assert h != h2
    assert hasher.verify("hunter2", h) is True
    assert hasher.verify("hunter2", h2) is True
    assert hasher.verify("wrong", h) is False


def test_bcrypt_hasher_handles_malformed_hash_as_false() -> None:
    hasher = BcryptPasswordHasher()
    # Malformed input shouldn't surface as an exception — auth code
    # treats this the same as "wrong password" to avoid leaking which
    # side of the comparison broke.
    assert hasher.verify("hunter2", "not-a-bcrypt-hash") is False
    assert hasher.verify("hunter2", "") is False
    assert hasher.verify("", "irrelevant") is False
