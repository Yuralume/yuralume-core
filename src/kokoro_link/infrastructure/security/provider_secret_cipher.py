"""Encrypted provider-secret envelope.

The project intentionally avoids a new crypto dependency here. The
implementation uses standard-library primitives: PBKDF2-HMAC-SHA256 to
derive independent encryption/MAC keys, an HMAC-SHA256 keystream for
confidentiality, and HMAC-SHA256 authentication over the full envelope.
It is versioned so a future migration to a library AEAD can coexist.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from typing import Any


class ProviderSecretCipherError(ValueError):
    """Raised when a secret cannot be encrypted or decrypted."""


@dataclass(frozen=True, slots=True)
class ProviderSecretCipher:
    key_material: str

    @property
    def configured(self) -> bool:
        return bool(self.key_material.strip())

    def encrypt(self, payload: dict[str, Any]) -> str:
        self._require_key()
        plaintext = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(16)
        enc_key, mac_key = self._derive_keys(salt)
        ciphertext = _xor_bytes(plaintext, _keystream(enc_key, nonce, len(plaintext)))
        mac = hmac.new(
            mac_key,
            b"v1" + salt + nonce + ciphertext,
            hashlib.sha256,
        ).digest()
        envelope = {
            "v": 1,
            "salt": _b64e(salt),
            "nonce": _b64e(nonce),
            "ciphertext": _b64e(ciphertext),
            "mac": _b64e(mac),
        }
        return base64.urlsafe_b64encode(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).decode("ascii")

    def decrypt(self, token: str) -> dict[str, Any]:
        self._require_key()
        if not token:
            return {}
        try:
            raw = base64.urlsafe_b64decode(token.encode("ascii"))
            envelope = json.loads(raw.decode("utf-8"))
            if envelope.get("v") != 1:
                raise ProviderSecretCipherError("unsupported secret envelope version")
            salt = _b64d(str(envelope["salt"]))
            nonce = _b64d(str(envelope["nonce"]))
            ciphertext = _b64d(str(envelope["ciphertext"]))
            expected_mac = _b64d(str(envelope["mac"]))
        except Exception as exc:
            if isinstance(exc, ProviderSecretCipherError):
                raise
            raise ProviderSecretCipherError("invalid secret envelope") from exc
        enc_key, mac_key = self._derive_keys(salt)
        actual_mac = hmac.new(
            mac_key,
            b"v1" + salt + nonce + ciphertext,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(actual_mac, expected_mac):
            raise ProviderSecretCipherError("provider secret authentication failed")
        plaintext = _xor_bytes(ciphertext, _keystream(enc_key, nonce, len(ciphertext)))
        data = json.loads(plaintext.decode("utf-8"))
        if not isinstance(data, dict):
            raise ProviderSecretCipherError("provider secret payload must be an object")
        return data

    def fingerprint(self, payload: dict[str, Any]) -> str:
        self._require_key()
        normalized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hmac.new(
            self.key_material.encode("utf-8"),
            b"provider-secret-fingerprint:" + normalized,
            hashlib.sha256,
        ).hexdigest()
        return digest[-12:]

    def _derive_keys(self, salt: bytes) -> tuple[bytes, bytes]:
        root = hashlib.pbkdf2_hmac(
            "sha256",
            self.key_material.encode("utf-8"),
            salt,
            210_000,
            dklen=64,
        )
        return root[:32], root[32:]

    def _require_key(self) -> None:
        if not self.configured:
            raise ProviderSecretCipherError(
                "CONFIG_ENCRYPTION_KEY is required to store provider secrets",
            )


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks: list[bytes] = []
    counter = 0
    while sum(len(block) for block in blocks) < length:
        blocks.append(
            hmac.new(
                key,
                nonce + counter.to_bytes(8, "big"),
                hashlib.sha256,
            ).digest(),
        )
        counter += 1
    return b"".join(blocks)[:length]


def _xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right, strict=True))


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))
