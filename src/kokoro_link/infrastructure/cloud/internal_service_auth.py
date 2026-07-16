"""Versioned service-to-service credential helpers.

The wire format is shared with Cloud Java services through
``contracts/internal-service-auth.json``::

    key_id|caller|audience|scope1,scope2|secret
    key_id|caller|audience|scope1,scope2|channel1,channel2|secret

The descriptor is configuration, never an HTTP query parameter. Secrets are only
materialised into the outbound header map and are not included in repr/log output.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InternalServiceCredential:
    key_id: str
    caller: str
    audience: str
    scopes: frozenset[str]
    secret: str

    @classmethod
    def parse(cls, descriptor: str) -> "InternalServiceCredential":
        fields = (descriptor or "").strip().split("|")
        if len(fields) not in {5, 6}:
            raise ValueError(
                "service credentials must use "
                "key|caller|audience|scopes|secret format",
            )
        key_id, caller, audience, scopes_raw = (
            value.strip() for value in fields[:4]
        )
        secret = fields[-1].strip()
        if not all((key_id, caller, audience, secret)):
            raise ValueError("service credential metadata and secret must not be blank")
        scopes = frozenset(
            scope.strip() for scope in scopes_raw.split(",") if scope.strip()
        )
        if not scopes:
            raise ValueError("service credential scopes must not be empty")
        return cls(
            key_id=key_id,
            caller=caller,
            audience=audience,
            scopes=scopes,
            secret=secret,
        )

    def headers(self) -> dict[str, str]:
        return {
            "X-Yuralume-Service-Token": self.secret,
            "X-Yuralume-Service-Key-Id": self.key_id,
            "X-Yuralume-Service-Caller": self.caller,
            "X-Yuralume-Service-Audience": self.audience,
            "X-Yuralume-Service-Scope": ",".join(sorted(self.scopes)),
        }


def outbound_headers(
    descriptor: str,
    *,
    legacy_token: str = "",
) -> dict[str, str]:
    """Build new credential headers, with an explicit R1a legacy fallback."""
    if descriptor.strip():
        return InternalServiceCredential.parse(descriptor).headers()
    token = legacy_token.strip()
    return {"X-Internal-Token": token} if token else {}
