"""TDD for the ``.lumebackup`` encryption envelope (CB1).

The envelope must hold three promises (CHARACTER_FULL_BACKUP_PLAN §5 /
§12 CB1): a wrong password fails at the header without scanning the
payload; any chunk deletion/reorder/tamper/truncation rejects the whole
file; KDF parameters and version are recorded in the header and honoured
on decrypt.

Tests run with a lowered scrypt ``n`` (2**14) and mostly a tiny chunk
size so chunk-boundary behaviour is exercised cheaply; one test keeps
the real 8 MiB default chunk to pin the shipped boundary.
"""

from __future__ import annotations

import io
import struct

import pytest

from kokoro_link.infrastructure.security.backup_cipher import (
    DEFAULT_CHUNK_SIZE,
    ENVELOPE_VERSION,
    HEADER_LEN,
    BackupEnvelopeVersionError,
    BackupFormatError,
    BackupIntegrityError,
    BackupWrongPasswordError,
    EnvelopeParams,
    decrypt_stream,
    encrypt_stream,
    read_envelope_header,
    verify_backup_password,
)

_PASSWORD = "correct horse battery staple"
# Fast-but-real scrypt for tests: 2**14 * 8 * 128 = 16 MiB, a few ms.
_FAST = EnvelopeParams(scrypt_n=2**14, chunk_size=1024)
_FAST_8MIB = EnvelopeParams(scrypt_n=2**14)  # default 8 MiB chunks

_MAGIC_LEN = 8
_VERSION_OFFSET = _MAGIC_LEN  # u16 BE right after the magic
_SCRYPT_N_OFFSET = _MAGIC_LEN + 2
_SALT_OFFSET = _MAGIC_LEN + 2 + 12  # after n/r/p (u32 each)
_FRAME_HEAD_LEN = 5


def _encrypt(data: bytes, *, params: EnvelopeParams = _FAST) -> bytes:
    dest = io.BytesIO()
    encrypt_stream(io.BytesIO(data), dest, password=_PASSWORD, params=params)
    return dest.getvalue()


def _decrypt(envelope: bytes, *, password: str = _PASSWORD) -> bytes:
    dest = io.BytesIO()
    decrypt_stream(io.BytesIO(envelope), dest, password=password)
    return dest.getvalue()


def _split_frames(envelope: bytes) -> tuple[bytes, list[bytes]]:
    """Split an envelope into (header, [frame bytes...])."""
    header = envelope[:HEADER_LEN]
    frames: list[bytes] = []
    offset = HEADER_LEN
    while offset < len(envelope):
        (ct_len,) = struct.unpack(">I", envelope[offset : offset + 4])
        end = offset + _FRAME_HEAD_LEN + ct_len
        frames.append(envelope[offset:end])
        offset = end
    assert offset == len(envelope)
    return header, frames


def _flip_byte(blob: bytes, index: int) -> bytes:
    mutated = bytearray(blob)
    mutated[index] ^= 0x01
    return bytes(mutated)


class _CountingReader:
    """File-like that records how many bytes were read from it."""

    def __init__(self, data: bytes) -> None:
        self._bio = io.BytesIO(data)
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        piece = self._bio.read(size)
        self.bytes_read += len(piece)
        return piece


# --- round trips -----------------------------------------------------------


@pytest.mark.parametrize(
    "size",
    [0, 1, 1023, 1024, 1025, 3 * 1024 + 7],
    ids=["empty", "one", "chunk-1", "chunk", "chunk+1", "multi"],
)
def test_round_trip_across_chunk_boundaries(size: int) -> None:
    data = bytes(i % 251 for i in range(size))
    assert _decrypt(_encrypt(data)) == data


@pytest.mark.parametrize("extra", [0, 1], ids=["exact-8mib", "8mib+1"])
def test_round_trip_default_8mib_chunk_boundary(extra: int) -> None:
    data = bytes(1024) * (DEFAULT_CHUNK_SIZE // 1024) + b"\x07" * extra
    envelope = _encrypt(data, params=_FAST_8MIB)
    _header, frames = _split_frames(envelope)
    # Exactly one chunk at 8 MiB, a second (tiny, final) chunk past it.
    assert len(frames) == 1 + (1 if extra else 0)
    assert _decrypt(envelope) == data


def test_empty_payload_still_carries_one_authenticated_final_frame() -> None:
    envelope = _encrypt(b"")
    _header, frames = _split_frames(envelope)
    assert len(frames) == 1
    ct_len, final_flag = struct.unpack(">IB", frames[0][:_FRAME_HEAD_LEN])
    assert ct_len == 16  # GCM tag alone authenticates the emptiness
    assert final_flag == 1


# --- password handling -----------------------------------------------------


def test_wrong_password_fails_fast_without_reading_payload() -> None:
    envelope = _encrypt(bytes(100 * 1024))  # ~100 frames at 1 KiB chunks
    source = _CountingReader(envelope)

    with pytest.raises(BackupWrongPasswordError):
        decrypt_stream(source, io.BytesIO(), password="not the password")

    assert source.bytes_read <= HEADER_LEN


def test_verify_backup_password_reads_header_only() -> None:
    envelope = _encrypt(bytes(64 * 1024))
    source = _CountingReader(envelope)

    header = verify_backup_password(source, password=_PASSWORD)

    assert header.version == ENVELOPE_VERSION
    assert source.bytes_read == HEADER_LEN
    with pytest.raises(BackupWrongPasswordError):
        verify_backup_password(io.BytesIO(envelope), password="nope")


def test_empty_password_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        encrypt_stream(io.BytesIO(b"x"), io.BytesIO(), password="")


# --- integrity: any structural mutation rejects the whole file -------------


def test_single_byte_tamper_in_payload_rejected() -> None:
    envelope = _encrypt(b"a" * 4096)
    tampered = _flip_byte(envelope, HEADER_LEN + _FRAME_HEAD_LEN + 10)
    with pytest.raises(BackupIntegrityError):
        _decrypt(tampered)


def test_tampered_header_salt_fails_as_wrong_password() -> None:
    # GCM cannot distinguish "wrong password" from "tampered header";
    # both fail the unwrap and must not reach the payload.
    envelope = _encrypt(b"a" * 4096)
    tampered = _flip_byte(envelope, _SALT_OFFSET)
    with pytest.raises(BackupWrongPasswordError):
        _decrypt(tampered)


def test_final_flag_flip_rejected() -> None:
    envelope = _encrypt(b"a" * 4096)
    header, frames = _split_frames(envelope)
    demoted = bytearray(frames[-1])
    demoted[4] = 0  # claim the true final chunk is not final
    with pytest.raises(BackupIntegrityError):
        _decrypt(header + b"".join(frames[:-1]) + bytes(demoted))


def test_chunk_swap_rejected() -> None:
    envelope = _encrypt(b"a" * 4096)  # 4 full frames + final
    header, frames = _split_frames(envelope)
    assert len(frames) >= 3
    frames[0], frames[1] = frames[1], frames[0]
    with pytest.raises(BackupIntegrityError):
        _decrypt(header + b"".join(frames))


def test_middle_chunk_deletion_rejected() -> None:
    envelope = _encrypt(b"a" * 4096)
    header, frames = _split_frames(envelope)
    del frames[1]
    with pytest.raises(BackupIntegrityError):
        _decrypt(header + b"".join(frames))


def test_final_chunk_deletion_rejected() -> None:
    envelope = _encrypt(b"a" * 4096)
    header, frames = _split_frames(envelope)
    with pytest.raises(BackupIntegrityError):
        _decrypt(header + b"".join(frames[:-1]))


@pytest.mark.parametrize("cut", [HEADER_LEN + 2, HEADER_LEN + 600])
def test_truncation_mid_frame_rejected(cut: int) -> None:
    envelope = _encrypt(b"a" * 4096)
    with pytest.raises(BackupIntegrityError):
        _decrypt(envelope[:cut])


def test_trailing_garbage_rejected() -> None:
    envelope = _encrypt(b"a" * 4096)
    with pytest.raises(BackupIntegrityError):
        _decrypt(envelope + b"junk")


# --- header: versioning and parameters -------------------------------------


def test_version_newer_than_build_rejected() -> None:
    envelope = bytearray(_encrypt(b"data"))
    struct.pack_into(">H", envelope, _VERSION_OFFSET, ENVELOPE_VERSION + 1)
    with pytest.raises(BackupEnvelopeVersionError):
        _decrypt(bytes(envelope))


def test_bad_magic_rejected() -> None:
    envelope = _encrypt(b"data")
    with pytest.raises(BackupFormatError):
        _decrypt(b"NOTLUME1" + envelope[_MAGIC_LEN:])
    with pytest.raises(BackupFormatError):
        _decrypt(b"")


def test_header_params_are_recorded_and_honoured() -> None:
    params = EnvelopeParams(scrypt_n=2**15, scrypt_r=2, scrypt_p=2, chunk_size=2048)
    envelope = _encrypt(b"b" * 5000, params=params)

    header = read_envelope_header(io.BytesIO(envelope))

    assert header.version == ENVELOPE_VERSION
    assert header.scrypt_n == 2**15
    assert header.scrypt_r == 2
    assert header.scrypt_p == 2
    assert header.chunk_size == 2048
    # Decrypt is told nothing — it must run entirely off the header.
    assert _decrypt(envelope) == b"b" * 5000


def test_hostile_kdf_params_rejected_before_any_derivation() -> None:
    # A crafted header demanding absurd scrypt memory must be rejected as
    # malformed (cheap, before scrypt runs) — not attempted.
    envelope = bytearray(_encrypt(b"data"))
    struct.pack_into(">I", envelope, _SCRYPT_N_OFFSET, 2**30)
    with pytest.raises(BackupFormatError):
        _decrypt(bytes(envelope))


@pytest.mark.parametrize(
    "params",
    [
        EnvelopeParams(scrypt_n=3),  # not a power of two
        EnvelopeParams(scrypt_n=2**30),  # over the memory cap
        EnvelopeParams(chunk_size=0),
        EnvelopeParams(scrypt_r=0),
        EnvelopeParams(scrypt_p=0),
    ],
    ids=["n-not-pow2", "n-too-big", "chunk-zero", "r-zero", "p-zero"],
)
def test_invalid_encrypt_params_are_programming_errors(
    params: EnvelopeParams,
) -> None:
    with pytest.raises(ValueError):
        encrypt_stream(io.BytesIO(b"x"), io.BytesIO(), password=_PASSWORD, params=params)


# --- S4: scrypt memory ceiling tightened toward the honest default --------


def test_honest_default_scrypt_params_still_accepted() -> None:
    """The tightened caps must not exclude the shipped default
    (n=2**17, r=8 → 128 MiB): a normal export/import cannot be caught by
    the anti-DoS ceiling."""
    from kokoro_link.infrastructure.security.backup_cipher import (
        DEFAULT_SCRYPT_N,
        DEFAULT_SCRYPT_P,
        DEFAULT_SCRYPT_R,
        _validate_params,
    )

    # Would raise if the ceiling had been dropped below the default.
    _validate_params(
        DEFAULT_SCRYPT_N,
        DEFAULT_SCRYPT_R,
        DEFAULT_SCRYPT_P,
        DEFAULT_CHUNK_SIZE,
        error=ValueError,
    )


def test_roughly_one_gib_scrypt_header_is_now_rejected() -> None:
    """S4 reproduction: n=2**22, r=2 demands ~1 GiB of scrypt memory and
    used to sit right at the old cap — a per-call memory bomb. The lowered
    256 MiB ceiling rejects it as a malformed header, before derivation."""
    from kokoro_link.infrastructure.security.backup_cipher import (
        _validate_params,
    )

    with pytest.raises(BackupFormatError):
        _validate_params(
            2**22, 2, 1, DEFAULT_CHUNK_SIZE, error=BackupFormatError,
        )
    # And the largest n the ceiling allows at r=1 (2**21 → 256 MiB) passes.
    _validate_params(2**21, 1, 1, DEFAULT_CHUNK_SIZE, error=BackupFormatError)
