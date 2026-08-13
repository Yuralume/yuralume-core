"""Versioned password-based encryption envelope for ``.lumebackup`` files.

The backup file is an offline attack surface — it may leak, be copied, or
sit in cold storage for years — so unlike :mod:`provider_secret_cipher`
(std-lib keystream tuned for a high-entropy env key) this envelope uses a
library AEAD with a strong password KDF:

```
magic "LUMEBAK1" | envelope_version (u16)
| scrypt n / r / p (u32 each) | salt (16B)
| chunk_size (u32) | nonce_prefix (4B) | wrap_nonce (12B)
| wrapped_file_key (32B key + 16B GCM tag)          <- fixed 106-byte header
| frame*                                            <- payload
frame = ct_len (u32) | final_flag (u8) | AES-256-GCM ciphertext+tag
```

Key hierarchy
    ``KEK = scrypt(password, salt, n, r, p)`` wraps a random 32-byte
    *file key* (AES-256-GCM, ``wrap_nonce``, AAD = header bytes before the
    wrapped key). Password verification therefore only needs the 106-byte
    header — a wrong password fails at unwrap without touching the
    payload — and a future "change password" / multi-recipient feature can
    re-wrap the file key without re-encrypting data.

Payload framing
    Plaintext is encrypted in ``chunk_size`` chunks (8 MiB default) with
    the file key. Each chunk's AAD is
    ``sha256(header) || chunk_index (u64 BE) || final_flag (u8)`` which
    rejects reordering (index), deletion (later indices shift), splicing
    across envelopes (header hash + per-file key), flag tampering, and
    truncation (a stream may only end right after a ``final_flag=1``
    frame; trailing bytes after it are rejected too).

Nonce uniqueness
    Chunk nonce = ``nonce_prefix (4B) || chunk_index (u64 BE)``. The file
    key is freshly random per envelope, so uniqueness only has to hold
    *within* one envelope, where the index is strictly increasing. The
    random per-envelope prefix is defence in depth should a file key ever
    be reused by a future bug.

Forward adjustability
    All KDF parameters and the chunk size are read back from the header,
    so future exporters may tune them without a version bump. Structural
    layout changes bump ``envelope_version``; a build refuses versions
    newer than itself (same stance as ``UnsupportedCardSchemaError`` on
    card import). Decrypt-side parameter sanity caps keep a hostile
    header from turning scrypt or chunk buffering into a memory bomb.

Streaming: both directions read/write file-likes chunk by chunk; peak
memory is a few chunk buffers, never the whole file.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import struct
from dataclasses import dataclass
from typing import IO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

ENVELOPE_MAGIC = b"LUMEBAK1"
ENVELOPE_VERSION = 1

DEFAULT_SCRYPT_N = 2**17
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024

_SALT_LEN = 16
_NONCE_PREFIX_LEN = 4
_WRAP_NONCE_LEN = 12
_FILE_KEY_LEN = 32
_GCM_TAG_LEN = 16
_WRAPPED_KEY_LEN = _FILE_KEY_LEN + _GCM_TAG_LEN

# magic | version | n | r | p | salt | chunk_size | nonce_prefix | wrap_nonce
_HEADER_PREFIX_FMT = ">8sHIII16sI4s12s"
_HEADER_PREFIX_LEN = struct.calcsize(_HEADER_PREFIX_FMT)  # 58
HEADER_LEN = _HEADER_PREFIX_LEN + _WRAPPED_KEY_LEN  # 106

_FRAME_HEAD_FMT = ">IB"
_FRAME_HEAD_LEN = struct.calcsize(_FRAME_HEAD_FMT)  # 5

# Decrypt-side sanity caps — a hostile header must not be able to demand
# unbounded scrypt memory (128 * n * r bytes) or a giant chunk buffer. The
# memory ceiling sits just above the honest default (n=2**17, r=8 → 128 MiB)
# so a preview/import cannot be turned into a ~1 GiB-per-call memory bomb
# with n=2**22,r=2; ``_MAX_SCRYPT_N`` is the largest n that fits the ceiling
# at r=1 (128 * 2**21 * 1 = 256 MiB), so the two caps agree. Honest default
# parameters stay comfortably inside both (S4).
_MAX_SCRYPT_N = 2**21
_MAX_SCRYPT_R = 64
_MAX_SCRYPT_P = 16
_MAX_SCRYPT_MEMORY_BYTES = 256 * 1024 * 1024  # 128 * n * r <= 256 MiB
_MAX_CHUNK_SIZE = 128 * 1024 * 1024

_COPY_BUFFER = 1024 * 1024


class BackupCipherError(Exception):
    """Base error for the ``.lumebackup`` encryption envelope."""


class BackupFormatError(BackupCipherError):
    """The blob is not a readable envelope (bad magic, malformed header,
    insane parameters, malformed frame)."""


class BackupEnvelopeVersionError(BackupCipherError):
    """The envelope declares a version newer than this build supports."""


class BackupWrongPasswordError(BackupCipherError):
    """The password failed to unwrap the file key.

    Cryptographically indistinguishable from a tampered header — GCM
    only reports that the unwrap tag did not verify.
    """


class BackupIntegrityError(BackupCipherError):
    """The payload failed authentication: a chunk was tampered with,
    reordered, deleted, the stream was truncated, or data trails the
    final chunk."""


@dataclass(frozen=True, slots=True)
class EnvelopeParams:
    """Encryption-side tunables, all recorded in the header."""

    scrypt_n: int = DEFAULT_SCRYPT_N
    scrypt_r: int = DEFAULT_SCRYPT_R
    scrypt_p: int = DEFAULT_SCRYPT_P
    chunk_size: int = DEFAULT_CHUNK_SIZE


DEFAULT_ENVELOPE_PARAMS = EnvelopeParams()


@dataclass(frozen=True, slots=True)
class EnvelopeHeader:
    """Public view of a parsed envelope header."""

    version: int
    scrypt_n: int
    scrypt_r: int
    scrypt_p: int
    chunk_size: int


@dataclass(frozen=True, slots=True)
class _ParsedHeader:
    header: EnvelopeHeader
    salt: bytes
    nonce_prefix: bytes
    wrap_nonce: bytes
    wrapped_key: bytes
    raw: bytes

    @property
    def wrap_aad(self) -> bytes:
        return self.raw[:_HEADER_PREFIX_LEN]

    @property
    def payload_binding(self) -> bytes:
        return hashlib.sha256(self.raw).digest()


@dataclass(frozen=True, slots=True)
class PreparedEnvelope:
    """KDF + key-wrap output split off from :func:`encrypt_stream`.

    Produced at API-request time so a background export job can encrypt
    **without ever seeing the password** (by design:
    the job row only holds the random file key, wrapped inside the
    header, and the raw file key — both scrubbed on the job's terminal
    transition). ``header`` is the complete 106-byte envelope header
    (including the password-wrapped file key); ``file_key`` is the raw
    32-byte AES key that :func:`encrypt_stream_with_key` needs.
    """

    header: bytes
    file_key: bytes


def prepare_envelope(
    password: str,
    *,
    params: EnvelopeParams = DEFAULT_ENVELOPE_PARAMS,
) -> PreparedEnvelope:
    """Run the scrypt KDF and wrap a fresh random file key.

    This is the only password-touching step of encryption; everything
    after it (:func:`encrypt_stream_with_key`) operates on the returned
    material alone. Raises :class:`ValueError` for an empty password or
    out-of-range ``params`` (programming errors, not hostile input).
    """
    if not password:
        raise ValueError("backup password must not be empty")
    _validate_params(
        params.scrypt_n,
        params.scrypt_r,
        params.scrypt_p,
        params.chunk_size,
        error=ValueError,
    )

    salt = secrets.token_bytes(_SALT_LEN)
    nonce_prefix = secrets.token_bytes(_NONCE_PREFIX_LEN)
    wrap_nonce = secrets.token_bytes(_WRAP_NONCE_LEN)
    file_key = secrets.token_bytes(_FILE_KEY_LEN)

    header_prefix = struct.pack(
        _HEADER_PREFIX_FMT,
        ENVELOPE_MAGIC,
        ENVELOPE_VERSION,
        params.scrypt_n,
        params.scrypt_r,
        params.scrypt_p,
        salt,
        params.chunk_size,
        nonce_prefix,
        wrap_nonce,
    )
    kek = _derive_kek(password, salt, params.scrypt_n, params.scrypt_r, params.scrypt_p)
    wrapped_key = AESGCM(kek).encrypt(wrap_nonce, file_key, header_prefix)
    return PreparedEnvelope(
        header=header_prefix + wrapped_key,
        file_key=file_key,
    )


def encrypt_stream_with_key(
    source: IO[bytes],
    dest: IO[bytes],
    *,
    header: bytes,
    file_key: bytes,
) -> None:
    """Encrypt ``source`` into ``dest`` using prepared envelope material.

    ``header`` / ``file_key`` come from :func:`prepare_envelope` (possibly
    round-tripped through a job row). The header is re-parsed here — the
    same validation as the decrypt side — so corrupted job payload
    material fails loudly instead of producing an unreadable artifact.
    Streams chunk by chunk; peak memory is a few chunk buffers.
    """
    if len(file_key) != _FILE_KEY_LEN:
        raise ValueError(
            f"file key must be {_FILE_KEY_LEN} bytes, got {len(file_key)}",
        )
    parsed = _read_and_parse_header(io.BytesIO(header))
    if len(header) != HEADER_LEN:
        raise BackupFormatError("envelope header has trailing bytes")
    chunk_size = parsed.header.chunk_size
    nonce_prefix = parsed.nonce_prefix
    dest.write(parsed.raw)

    binding = parsed.payload_binding
    aead = AESGCM(file_key)
    index = 0
    current = _read_up_to(source, chunk_size)
    while True:
        lookahead = b""
        if len(current) == chunk_size:
            lookahead = _read_up_to(source, chunk_size)
        final = not lookahead
        ciphertext = aead.encrypt(
            _chunk_nonce(nonce_prefix, index),
            current,
            _chunk_aad(binding, index, final),
        )
        dest.write(struct.pack(_FRAME_HEAD_FMT, len(ciphertext), 1 if final else 0))
        dest.write(ciphertext)
        if final:
            return
        current = lookahead
        index += 1


def encrypt_stream(
    source: IO[bytes],
    dest: IO[bytes],
    *,
    password: str,
    params: EnvelopeParams = DEFAULT_ENVELOPE_PARAMS,
) -> None:
    """Encrypt ``source`` into a ``.lumebackup`` envelope written to ``dest``.

    Composition of :func:`prepare_envelope` (KDF + key wrap) and
    :func:`encrypt_stream_with_key` (framing) — split so a background
    job can run the second half without the password. Streams chunk by
    chunk; peak memory is bounded by a few ``params.chunk_size``
    buffers. Raises :class:`ValueError` for an empty password or
    out-of-range ``params`` (programming errors, not hostile input).
    """
    prepared = prepare_envelope(password, params=params)
    encrypt_stream_with_key(
        source,
        dest,
        header=prepared.header,
        file_key=prepared.file_key,
    )


@dataclass(frozen=True, slots=True)
class UnwrappedEnvelope:
    """Password-verified decrypt material (CB3 restore-job seam).

    The mirror of :class:`PreparedEnvelope` for the decrypt direction: a
    restore runs as a background job that must never see the password, so
    the API request unwraps the file key synchronously (this is also the
    password check) and the job decrypts with
    :func:`decrypt_stream_with_key` alone. The raw ``file_key`` rides the
    job row and is scrubbed on its terminal transition, exactly like the
    export side's prepared material.
    """

    header: EnvelopeHeader
    file_key: bytes


def verify_and_unwrap(
    source: IO[bytes], *, password: str,
) -> UnwrappedEnvelope:
    """Verify ``password`` against the header and return the file key.

    Reads exactly the 106-byte header (like
    :func:`verify_backup_password`) and raises
    :class:`BackupWrongPasswordError` on mismatch — but hands back the
    unwrapped file key so a background job can later decrypt without the
    password.
    """
    parsed = _read_and_parse_header(source)
    file_key = _unwrap_file_key(parsed, password)
    return UnwrappedEnvelope(header=parsed.header, file_key=file_key)


def decrypt_stream_with_key(
    source: IO[bytes],
    dest: IO[bytes],
    *,
    file_key: bytes,
) -> None:
    """Decrypt an envelope using an already-unwrapped file key.

    The password-free half of :func:`decrypt_stream` (its exact framing
    twin) — pair with :func:`verify_and_unwrap`. A wrong or corrupted
    key surfaces as :class:`BackupIntegrityError` on the first chunk;
    header parsing and payload authentication are identical to the
    password path.
    """
    if len(file_key) != _FILE_KEY_LEN:
        raise ValueError(
            f"file key must be {_FILE_KEY_LEN} bytes, got {len(file_key)}",
        )
    parsed = _read_and_parse_header(source)
    _decrypt_frames(parsed, source, dest, file_key)


def decrypt_stream(
    source: IO[bytes],
    dest: IO[bytes],
    *,
    password: str,
) -> None:
    """Decrypt a ``.lumebackup`` envelope from ``source`` into ``dest``.

    A wrong password fails fast at the 106-byte header
    (:class:`BackupWrongPasswordError`) without reading the payload. Any
    payload tampering — bit flips, chunk reorder/deletion, truncation,
    trailing data — raises :class:`BackupIntegrityError`; nothing about
    the failure is recoverable, callers must discard any partial output.
    """
    parsed = _read_and_parse_header(source)
    file_key = _unwrap_file_key(parsed, password)
    _decrypt_frames(parsed, source, dest, file_key)


def _decrypt_frames(
    parsed: _ParsedHeader,
    source: IO[bytes],
    dest: IO[bytes],
    file_key: bytes,
) -> None:
    aead = AESGCM(file_key)
    binding = parsed.payload_binding
    chunk_size = parsed.header.chunk_size

    index = 0
    while True:
        frame_head = _read_up_to(source, _FRAME_HEAD_LEN)
        if len(frame_head) < _FRAME_HEAD_LEN:
            raise BackupIntegrityError(
                f"envelope truncated in frame header at chunk {index}",
            )
        ct_len, final_flag = struct.unpack(_FRAME_HEAD_FMT, frame_head)
        if final_flag not in (0, 1):
            raise BackupFormatError(f"invalid frame flag {final_flag}")
        if ct_len < _GCM_TAG_LEN or ct_len > chunk_size + _GCM_TAG_LEN:
            raise BackupFormatError(f"invalid frame length {ct_len}")
        ciphertext = _read_up_to(source, ct_len)
        if len(ciphertext) < ct_len:
            raise BackupIntegrityError(
                f"envelope truncated inside chunk {index}",
            )
        try:
            plaintext = aead.decrypt(
                _chunk_nonce(parsed.nonce_prefix, index),
                ciphertext,
                _chunk_aad(binding, index, bool(final_flag)),
            )
        except InvalidTag as exc:
            raise BackupIntegrityError(
                f"chunk {index} failed authentication",
            ) from exc
        dest.write(plaintext)
        if final_flag:
            if _read_up_to(source, 1):
                raise BackupIntegrityError("data after final chunk")
            return
        index += 1


def verify_backup_password(source: IO[bytes], *, password: str) -> EnvelopeHeader:
    """Check ``password`` against the envelope header only.

    Reads exactly the header from ``source`` (leaving it positioned at
    the first payload frame) and attempts the file-key unwrap. Raises
    :class:`BackupWrongPasswordError` on mismatch. This is the cheap
    pre-flight for import preview — no payload is read.
    """
    parsed = _read_and_parse_header(source)
    _unwrap_file_key(parsed, password)
    return parsed.header


def read_envelope_header(source: IO[bytes]) -> EnvelopeHeader:
    """Parse the envelope header without a password (version/KDF params)."""
    return _read_and_parse_header(source).header


def _read_and_parse_header(source: IO[bytes]) -> _ParsedHeader:
    intro_len = len(ENVELOPE_MAGIC) + 2
    intro = _read_up_to(source, intro_len)
    if len(intro) < intro_len or intro[: len(ENVELOPE_MAGIC)] != ENVELOPE_MAGIC:
        raise BackupFormatError("not a .lumebackup envelope")
    (version,) = struct.unpack(">H", intro[len(ENVELOPE_MAGIC) :])
    if version > ENVELOPE_VERSION:
        raise BackupEnvelopeVersionError(
            f"envelope version {version} is newer than this build "
            f"supports (max {ENVELOPE_VERSION}); upgrade before importing",
        )
    if version != 1:
        raise BackupFormatError(f"invalid envelope version {version}")

    rest = _read_up_to(source, HEADER_LEN - intro_len)
    if len(rest) < HEADER_LEN - intro_len:
        raise BackupFormatError("envelope header truncated")
    raw = intro + rest
    (
        _magic,
        _version,
        scrypt_n,
        scrypt_r,
        scrypt_p,
        salt,
        chunk_size,
        nonce_prefix,
        wrap_nonce,
    ) = struct.unpack(_HEADER_PREFIX_FMT, raw[:_HEADER_PREFIX_LEN])
    _validate_params(scrypt_n, scrypt_r, scrypt_p, chunk_size, error=BackupFormatError)
    return _ParsedHeader(
        header=EnvelopeHeader(
            version=version,
            scrypt_n=scrypt_n,
            scrypt_r=scrypt_r,
            scrypt_p=scrypt_p,
            chunk_size=chunk_size,
        ),
        salt=salt,
        nonce_prefix=nonce_prefix,
        wrap_nonce=wrap_nonce,
        wrapped_key=raw[_HEADER_PREFIX_LEN:],
        raw=raw,
    )


def _unwrap_file_key(parsed: _ParsedHeader, password: str) -> bytes:
    kek = _derive_kek(
        password,
        parsed.salt,
        parsed.header.scrypt_n,
        parsed.header.scrypt_r,
        parsed.header.scrypt_p,
    )
    try:
        return AESGCM(kek).decrypt(
            parsed.wrap_nonce,
            parsed.wrapped_key,
            parsed.wrap_aad,
        )
    except InvalidTag as exc:
        raise BackupWrongPasswordError(
            "wrong password (or a tampered envelope header)",
        ) from exc


def _derive_kek(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=salt, length=_FILE_KEY_LEN, n=n, r=r, p=p)
    return kdf.derive(password.encode("utf-8"))


def _chunk_nonce(prefix: bytes, index: int) -> bytes:
    return prefix + struct.pack(">Q", index)


def _chunk_aad(binding: bytes, index: int, final: bool) -> bytes:
    return binding + struct.pack(">QB", index, 1 if final else 0)


def _validate_params(
    n: int,
    r: int,
    p: int,
    chunk_size: int,
    *,
    error: type[Exception],
) -> None:
    if n < 2 or n > _MAX_SCRYPT_N or (n & (n - 1)) != 0:
        raise error(f"scrypt n out of range or not a power of two: {n}")
    if not 1 <= r <= _MAX_SCRYPT_R:
        raise error(f"scrypt r out of range: {r}")
    if not 1 <= p <= _MAX_SCRYPT_P:
        raise error(f"scrypt p out of range: {p}")
    if 128 * n * r > _MAX_SCRYPT_MEMORY_BYTES:
        raise error(f"scrypt parameters demand too much memory (n={n}, r={r})")
    if not 1 <= chunk_size <= _MAX_CHUNK_SIZE:
        raise error(f"chunk size out of range: {chunk_size}")


def _read_up_to(stream: IO[bytes], count: int) -> bytes:
    """Read up to ``count`` bytes, looping over short reads until EOF."""
    parts: list[bytes] = []
    remaining = count
    while remaining > 0:
        piece = stream.read(remaining)
        if not piece:
            break
        parts.append(piece)
        remaining -= len(piece)
    if len(parts) == 1:
        return parts[0]
    return b"".join(parts)
