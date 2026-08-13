"""CB2's split of the encryption path: ``prepare_envelope`` (request-time,
password-touching) + ``encrypt_stream_with_key`` (job-time, key-only).

CB1's ``test_backup_cipher`` pins the envelope format itself; this file
pins the *seam*: material that round-trips through a job payload must
produce an envelope indistinguishable from the one-shot API, and corrupt
payload material must fail loudly instead of writing garbage.
"""

from __future__ import annotations

import io

import pytest

from kokoro_link.infrastructure.security.backup_cipher import (
    BackupFormatError,
    BackupIntegrityError,
    EnvelopeParams,
    HEADER_LEN,
    decrypt_stream,
    encrypt_stream_with_key,
    prepare_envelope,
    verify_backup_password,
)

_FAST = EnvelopeParams(scrypt_n=2**8, scrypt_r=8, scrypt_p=1, chunk_size=1024)
_PASSWORD = "貓咪的一次性密碼"


def _encrypt(payload: bytes) -> bytes:
    prepared = prepare_envelope(_PASSWORD, params=_FAST)
    dest = io.BytesIO()
    encrypt_stream_with_key(
        io.BytesIO(payload), dest,
        header=prepared.header, file_key=prepared.file_key,
    )
    return dest.getvalue()


def test_prepared_material_round_trips_through_decrypt() -> None:
    payload = b"inner-zip-bytes" * 300  # multiple chunks at 1 KiB
    encrypted = _encrypt(payload)

    out = io.BytesIO()
    decrypt_stream(io.BytesIO(encrypted), out, password=_PASSWORD)
    assert out.getvalue() == payload


def test_prepared_header_is_the_wire_header() -> None:
    prepared = prepare_envelope(_PASSWORD, params=_FAST)
    assert len(prepared.header) == HEADER_LEN
    assert len(prepared.file_key) == 32

    dest = io.BytesIO()
    encrypt_stream_with_key(
        io.BytesIO(b"x"), dest,
        header=prepared.header, file_key=prepared.file_key,
    )
    encrypted = dest.getvalue()
    assert encrypted[:HEADER_LEN] == prepared.header
    # Password verification works off the emitted envelope alone.
    header = verify_backup_password(
        io.BytesIO(encrypted), password=_PASSWORD,
    )
    assert header.scrypt_n == _FAST.scrypt_n


def test_empty_payload_still_produces_a_valid_envelope() -> None:
    encrypted = _encrypt(b"")
    out = io.BytesIO()
    decrypt_stream(io.BytesIO(encrypted), out, password=_PASSWORD)
    assert out.getvalue() == b""


def test_wrong_file_key_length_is_rejected() -> None:
    prepared = prepare_envelope(_PASSWORD, params=_FAST)
    with pytest.raises(ValueError):
        encrypt_stream_with_key(
            io.BytesIO(b"x"), io.BytesIO(),
            header=prepared.header, file_key=prepared.file_key[:-1],
        )


def test_corrupted_header_material_fails_loudly() -> None:
    prepared = prepare_envelope(_PASSWORD, params=_FAST)
    with pytest.raises(BackupFormatError):
        encrypt_stream_with_key(
            io.BytesIO(b"x"), io.BytesIO(),
            header=prepared.header[: HEADER_LEN // 2],  # truncated
            file_key=prepared.file_key,
        )
    with pytest.raises(BackupFormatError):
        encrypt_stream_with_key(
            io.BytesIO(b"x"), io.BytesIO(),
            header=prepared.header + b"trailing",
            file_key=prepared.file_key,
        )


def test_mismatched_file_key_yields_undecryptable_payload() -> None:
    """A right-length but wrong key cannot be detected at encrypt time —
    but the archive must then refuse to decrypt (integrity, not silent
    garbage)."""
    first = prepare_envelope(_PASSWORD, params=_FAST)
    second = prepare_envelope(_PASSWORD, params=_FAST)
    dest = io.BytesIO()
    encrypt_stream_with_key(
        io.BytesIO(b"payload"), dest,
        header=first.header, file_key=second.file_key,
    )
    with pytest.raises(BackupIntegrityError):
        decrypt_stream(
            io.BytesIO(dest.getvalue()), io.BytesIO(), password=_PASSWORD,
        )
