"""Service-to-service external-chat roster read (LH2).

``GET /api/internal/v1/external-chat/roster?tenant_id&account_id`` — the
hosted LINE official-channel Cloud service reads the authoritative roster
for a ``(tenant_id, account_id)`` pair here before routing an inbound
message to a character picker.

Auth mirrors ``internal_cloud.py``: a versioned service credential
(``X-Yuralume-Service-*`` headers), never the operator JWT/session, sourced
from the shared ``KOKORO_CLOUD_INTERNAL_CREDENTIALS`` rotation set. This
route is distinguished from the Cloud→Core freeze/tier callers by requiring
``caller=cloud-channel`` (they use ``cloud-user``) and
``scope=external-chat:roster-read``. Fail-closed: unconfigured → 503, a
credential matching none of the entries → 401. A validly-authenticated but
unresolvable pair → an opaque 404 (never revealing which half was wrong,
nor crossing a tenant boundary).
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from kokoro_link.api.dependencies import get_container
from kokoro_link.application.dto.external_chat import (
    ExternalChatAttachmentInput,
    ExternalChatTurnCommand,
)
from kokoro_link.application.services.external_chat.turn_support import (
    error_envelope,
)
from kokoro_link.application.services.external_chat_attachment_service import (
    Stored,
    TooLarge,
    UnknownAccount,
    UnknownCharacter,
    UnsupportedType,
)
from kokoro_link.application.services.external_chat_roster_service import (
    RosterView,
)
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.infrastructure.cloud.internal_service_auth import (
    InternalServiceCredential,
)

router = APIRouter(prefix="/external-chat", tags=["internal-external-chat"])

# Full mounted path prefix of this router (``/api/internal/v1`` include-prefix +
# the router prefix). The scoped ``RequestValidationError`` handler keys off it
# so only external-chat request bodies get an ErrorEnvelope 422; every other
# route keeps FastAPI's default validation body (M11 — no global change).
_EXTERNAL_CHAT_PATH_PREFIX = "/api/internal/v1/external-chat"


class ExternalChatCredentialError(Exception):
    """Service-credential gate failure carrying an ErrorEnvelope verdict.

    Raised in place of a bare ``HTTPException`` so the fail-closed 401 / 503
    bodies are the same ``{"error": {...}}`` envelope as every other
    external-chat error, instead of FastAPI's default ``{"detail": ...}`` (M11).
    """

    def __init__(
        self, *, status_code: int, code: str, message: str, retryable: bool,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


def _envelope_response(
    *, status_code: int, code: str, message: str, retryable: bool,
) -> JSONResponse:
    """One OpenAPI ``ErrorEnvelope`` builder for every external-chat error body.

    The turn route already returns the service's pre-serialised envelope; the
    roster / attachment routes route their outcome + not-wired errors through
    here so 401 / 404 / 409 / 413 / 415 / 422 / 429 / 503 all share the exact
    ``{"error": {"code", "message", "retryable"}}`` shape (M11).
    """
    return JSONResponse(
        status_code=status_code,
        content=error_envelope(code=code, message=message, retryable=retryable),
    )


async def _credential_error_handler(
    request: Request, exc: ExternalChatCredentialError,
) -> JSONResponse:
    return _envelope_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        retryable=exc.retryable,
    )


async def _validation_error_handler(
    request: Request, exc: RequestValidationError,
) -> JSONResponse:
    """Return an ErrorEnvelope 422 for external-chat paths only; delegate every
    other path to FastAPI's default so global behaviour is untouched (M11)."""
    if request.url.path.startswith(_EXTERNAL_CHAT_PATH_PREFIX):
        return _envelope_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="invalid_request",
            message="request validation failed",
            retryable=False,
        )
    return await request_validation_exception_handler(request, exc)


def register_external_chat_exception_handlers(app: object) -> None:
    """Attach the external-chat-scoped credential + validation error handlers.

    The ``RequestValidationError`` handler is registered app-wide (FastAPI has
    no per-router hook) but is a no-op for non-external-chat paths — it forwards
    them to FastAPI's default handler unchanged."""
    app.add_exception_handler(  # type: ignore[attr-defined]
        ExternalChatCredentialError, _credential_error_handler,
    )
    app.add_exception_handler(  # type: ignore[attr-defined]
        RequestValidationError, _validation_error_handler,
    )

_INTERNAL_CREDENTIALS_ENV = "KOKORO_CLOUD_INTERNAL_CREDENTIALS"
_INTERNAL_AUDIENCE = "yuralume-core"
_INTERNAL_CALLER = "cloud-channel"
_ROSTER_SCOPE = "external-chat:roster-read"
_ATTACHMENT_SCOPE = "external-chat:attachment-write"
_TURN_SCOPE = "external-chat:turn-write"
_ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024  # 8 MB — matches the service cap.
_LOG = logging.getLogger(__name__)


class RosterCharacterResponse(BaseModel):
    character_id: str
    display_name: str
    background_frozen: bool
    avatar_object_ref: str | None
    presentation_version: str


class RosterResponse(BaseModel):
    tenant_id: str
    account_id: str
    characters: list[RosterCharacterResponse]


def _configured_credentials() -> tuple[InternalServiceCredential, ...]:
    "Parse the credential rotation set at request time."
    raw = os.getenv(_INTERNAL_CREDENTIALS_ENV, "")
    credentials: list[InternalServiceCredential] = []
    for descriptor in raw.split(";"):
        if not descriptor.strip():
            continue
        try:
            credentials.append(InternalServiceCredential.parse(descriptor))
        except ValueError:
            _LOG.error("invalid cloud internal credential descriptor")
            return ()
    return tuple(credentials)


def _credential_matches(
    credentials: tuple[InternalServiceCredential, ...],
    *,
    required_scope: str,
    token: str | None,
    key_id: str | None,
    caller: str | None,
    audience: str | None,
    scope: str | None,
) -> bool:
    if not token or not key_id or not caller or not audience:
        return False
    requested_scopes = {
        item.strip() for item in (scope or "").split(",") if item.strip()
    }
    if required_scope not in requested_scopes:
        return False
    if caller != _INTERNAL_CALLER or audience != _INTERNAL_AUDIENCE:
        return False
    for credential in credentials:
        if credential.key_id != key_id or credential.caller != caller:
            continue
        if credential.audience != audience or not requested_scopes.issubset(
            credential.scopes,
        ):
            continue
        if secrets.compare_digest(
            credential.secret.encode("utf-8"),
            token.encode("utf-8"),
        ):
            return True
    return False


def _enforce_credential(
    *,
    required_scope: str,
    token: str | None,
    key_id: str | None,
    caller: str | None,
    audience: str | None,
    scope: str | None,
) -> None:
    """Shared fail-closed gate: unconfigured → 503, any mismatch → 401.

    Both failures raise :class:`ExternalChatCredentialError` so the body is the
    shared ErrorEnvelope, not FastAPI's default ``{"detail": ...}`` (M11)."""
    credentials = _configured_credentials()
    if not credentials:
        raise ExternalChatCredentialError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="cloud internal channel not configured",
            retryable=True,
        )
    if _credential_matches(
        credentials,
        required_scope=required_scope,
        token=token,
        key_id=key_id,
        caller=caller,
        audience=audience,
        scope=scope,
    ):
        return
    raise ExternalChatCredentialError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        code="unauthorized",
        message="invalid internal service credential",
        retryable=False,
    )


async def require_roster_read_credential(
    service_token: str | None = Header(
        default=None, alias="X-Yuralume-Service-Token",
    ),
    key_id: str | None = Header(default=None, alias="X-Yuralume-Service-Key-Id"),
    caller: str | None = Header(default=None, alias="X-Yuralume-Service-Caller"),
    audience: str | None = Header(
        default=None, alias="X-Yuralume-Service-Audience",
    ),
    scope: str | None = Header(default=None, alias="X-Yuralume-Service-Scope"),
) -> None:
    _enforce_credential(
        required_scope=_ROSTER_SCOPE,
        token=service_token,
        key_id=key_id,
        caller=caller,
        audience=audience,
        scope=scope,
    )


async def require_attachment_write_credential(
    service_token: str | None = Header(
        default=None, alias="X-Yuralume-Service-Token",
    ),
    key_id: str | None = Header(default=None, alias="X-Yuralume-Service-Key-Id"),
    caller: str | None = Header(default=None, alias="X-Yuralume-Service-Caller"),
    audience: str | None = Header(
        default=None, alias="X-Yuralume-Service-Audience",
    ),
    scope: str | None = Header(default=None, alias="X-Yuralume-Service-Scope"),
) -> None:
    _enforce_credential(
        required_scope=_ATTACHMENT_SCOPE,
        token=service_token,
        key_id=key_id,
        caller=caller,
        audience=audience,
        scope=scope,
    )


async def require_turn_write_credential(
    service_token: str | None = Header(
        default=None, alias="X-Yuralume-Service-Token",
    ),
    key_id: str | None = Header(default=None, alias="X-Yuralume-Service-Key-Id"),
    caller: str | None = Header(default=None, alias="X-Yuralume-Service-Caller"),
    audience: str | None = Header(
        default=None, alias="X-Yuralume-Service-Audience",
    ),
    scope: str | None = Header(default=None, alias="X-Yuralume-Service-Scope"),
) -> None:
    _enforce_credential(
        required_scope=_TURN_SCOPE,
        token=service_token,
        key_id=key_id,
        caller=caller,
        audience=audience,
        scope=scope,
    )


@router.get(
    "/roster",
    response_model=RosterResponse,
    dependencies=[Depends(require_roster_read_credential)],
)
async def get_external_chat_roster(
    tenant_id: str = Query(..., min_length=1, max_length=64),
    account_id: str = Query(..., min_length=1, max_length=64),
    container: ServiceContainer = Depends(get_container),
) -> RosterResponse | JSONResponse:
    """Return the authoritative roster for a cloud pair.

    An unresolvable pair (unknown account, tenant mismatch, or a non-cloud
    operator) is an opaque 404 — identical to a genuinely missing resource,
    so the caller learns nothing about which half failed."""
    service = container.external_chat_roster_service
    if service is None:
        return _envelope_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="external chat roster subsystem not wired",
            retryable=True,
        )
    view = await service.resolve_roster(
        tenant_id=tenant_id, account_id=account_id,
    )
    if view is None:
        return _envelope_response(
            status_code=status.HTTP_404_NOT_FOUND,
            code="not_found",
            message="not found",
            retryable=False,
        )
    return _to_response(view)


class AttachmentRefResponse(BaseModel):
    """Wire shape of the OpenAPI ``AttachmentRef`` (snake_case)."""

    object_ref: str
    media_type: str
    size_bytes: int
    sha256: str


@router.post(
    "/attachments",
    response_model=AttachmentRefResponse,
    dependencies=[Depends(require_attachment_write_credential)],
)
async def create_external_chat_attachment(
    tenant_id: str = Form(..., min_length=1, max_length=64),
    account_id: str = Form(..., min_length=1, max_length=64),
    character_id: str = Form(..., min_length=1, max_length=64),
    file: UploadFile = File(...),
    container: ServiceContainer = Depends(get_container),
) -> AttachmentRefResponse | JSONResponse:
    """Validate and store an external-chat image attachment.

    Core owns MIME sniffing, the size ceiling, and pixel-bomb protection; the
    returned ``object_ref`` is an opaque Core object key, never a fetch URL.
    Unknown account / character both collapse to an opaque 404 identical to
    the roster route's, so the caller learns nothing about which half failed.
    """
    service = container.external_chat_attachment_service
    if service is None:
        return _envelope_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="external chat attachment subsystem not wired",
            retryable=True,
        )

    # Bound memory: read at most one byte past the cap so an oversized upload
    # trips TooLarge without materialising the whole payload.
    data = await file.read(_ATTACHMENT_MAX_BYTES + 1)

    outcome = await service.ingest(
        tenant_id=tenant_id,
        account_id=account_id,
        character_id=character_id,
        data=data,
        declared_content_type=file.content_type or "",
    )

    match outcome:
        case Stored(attachment=attachment):
            return AttachmentRefResponse(
                object_ref=attachment.object_ref,
                media_type=attachment.media_type,
                size_bytes=attachment.size_bytes,
                sha256=attachment.sha256,
            )
        case UnknownAccount() | UnknownCharacter():
            # Opaque 404 — identical envelope to the roster route's not-found.
            return _envelope_response(
                status_code=status.HTTP_404_NOT_FOUND,
                code="not_found",
                message="not found",
                retryable=False,
            )
        case TooLarge():
            return _envelope_response(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                code="payload_too_large",
                message="attachment exceeds the size limit",
                retryable=False,
            )
        case UnsupportedType():
            return _envelope_response(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                code="unsupported_media_type",
                message="attachment type is not a supported image",
                retryable=False,
            )

    # Exhaustive above; defensive fallthrough keeps the type-checker honest.
    return _envelope_response(  # pragma: no cover
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal_error",
        message="unhandled attachment outcome",
        retryable=True,
    )


class ExternalChatTurnAttachmentRequest(BaseModel):
    """Wire shape of one inbound ``AttachmentRef`` (already ingested)."""

    model_config = ConfigDict(extra="forbid")

    object_ref: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExternalChatTurnRequestModel(BaseModel):
    """Field-for-field mirror of the OpenAPI ``ExternalChatTurnRequest``.

    ``additionalProperties: false`` → ``extra="forbid"``; ``message`` and
    ``request_id`` carry their length bounds; ``channel`` / ``contract_version``
    are consts; ``attachments`` is capped at 5.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    character_id: str = Field(min_length=1)
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=20000)
    attachments: list[ExternalChatTurnAttachmentRequest] = Field(max_length=5)
    channel: Literal["line"]
    contract_version: Literal[1]


@router.post(
    "/turns",
    dependencies=[Depends(require_turn_write_credential)],
)
async def create_external_chat_turn(
    request: ExternalChatTurnRequestModel,
    container: ServiceContainer = Depends(get_container),
) -> Response:
    """Claim, execute (or replay) one recoverable external-chat turn.

    The turn service returns an opaque, already-serialised outcome: a fresh
    durable claim returns 202 immediately and is driven outside this request;
    a later 200 carries its byte-identical response snapshot, while 202 means
    the same request is still processing (retry after the header delay),
    and 404 / 409 carry stable error envelopes safe to replay."""
    service = container.external_chat_turn_service
    if service is None:
        return _envelope_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="unavailable",
            message="external chat turn subsystem not wired",
            retryable=True,
        )
    command = ExternalChatTurnCommand(
        request_id=request.request_id,
        tenant_id=request.tenant_id,
        account_id=request.account_id,
        character_id=request.character_id,
        message=request.message,
        attachments=tuple(
            ExternalChatAttachmentInput(
                object_ref=item.object_ref,
                media_type=item.media_type,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
            )
            for item in request.attachments
        ),
        channel=request.channel,
        contract_version=request.contract_version,
        conversation_id=request.conversation_id,
        conversation_id_present="conversation_id" in request.model_fields_set,
    )
    result = await service.accept(command)
    headers: dict[str, str] = {}
    if result.retry_after_seconds is not None:
        headers["Retry-After"] = str(result.retry_after_seconds)
    if result.body_json is None:
        return Response(status_code=result.status_code, headers=headers)
    return Response(
        content=result.body_json,
        media_type="application/json",
        status_code=result.status_code,
        headers=headers,
    )


def _to_response(view: RosterView) -> RosterResponse:
    return RosterResponse(
        tenant_id=view.tenant_id,
        account_id=view.account_id,
        characters=[
            RosterCharacterResponse(
                character_id=item.character_id,
                display_name=item.display_name,
                background_frozen=item.background_frozen,
                avatar_object_ref=item.avatar_object_ref,
                presentation_version=item.presentation_version,
            )
            for item in view.characters
        ],
    )
