"""Cloud Gateway OpenAI-compatible embeddings adapter."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

import httpx

from kokoro_link.application.services.cloud_identity_context import current_cloud_actor
from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentity,
    CloudGatewayIdentityResolverPort,
    CloudIdentityUnavailable,
    CloudResourceContext,
    character_origin_headers,
)
from kokoro_link.contracts.cloud_routing_profile import CloudRoutingProfilePort
from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
from kokoro_link.contracts.generation_trigger import (
    TRIGGER_HEADER_NAME,
    generation_trigger_header_value,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_BATCH = 32
_CAPABILITY = "embedding"
_DEFAULT_FEATURE_KEY = "embedding"


class CloudGatewayEmbedder(EmbedderPort):
    """Route semantic-memory embeddings through the hosted Cloud Gateway.

    The User profile field is intentionally optional during the rollout. A profile
    without embedding_feature_presets falls back to the cloud env/default preset;
    Gateway remains the final entitlement/demo-allowlist boundary.

    TODO(user-service): expose embedding in CoreProfileResponse/effective scopes and
    preflight before making this capability credit-billable.
    """

    def __init__(
        self,
        *,
        base_url: str,
        deployment_token: str,
        default_model: str,
        dimension: int = 1024,
        identity: CloudGatewayIdentity | None = None,
        identity_resolver: CloudGatewayIdentityResolverPort | None = None,
        routing_profile_port: CloudRoutingProfilePort | None = None,
        deployment_id: str = "hosted-primary",
        audience: str = "yuralume-gateway",
        feature_key: str = _DEFAULT_FEATURE_KEY,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not base_url.strip():
            raise ValueError("cloud gateway embedding base_url is required")
        if not deployment_token.strip():
            raise ValueError("cloud gateway embedding deployment_token is required")
        if not default_model.strip():
            raise ValueError("cloud gateway embedding default_model is required")
        if not deployment_id.strip():
            raise ValueError("cloud gateway embedding deployment_id is required")
        if not audience.strip():
            raise ValueError("cloud gateway embedding audience is required")
        self._base_url = base_url.rstrip("/")
        self._deployment_token = deployment_token
        self._default_model = default_model.strip()
        self._dimension = dimension
        self._identity = identity
        self._identity_resolver = identity_resolver
        self._routing_profile_port = routing_profile_port
        self._deployment_id = deployment_id.strip()
        self._audience = audience.strip()
        self._feature_key = feature_key.strip() or _DEFAULT_FEATURE_KEY
        self._timeout = httpx.Timeout(
            connect=10.0,
            read=max(30.0, timeout_seconds),
            write=30.0,
            pool=10.0,
        )
        self.last_request_id = ""

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def is_operational(self) -> bool:
        return True

    async def embed(self, text: str) -> tuple[float, ...] | None:
        results = await self.embed_many([text])
        return results[0] if results else None

    async def embed_many(
        self,
        texts: Sequence[str],
    ) -> list[tuple[float, ...] | None]:
        if not texts:
            return []
        identity = await self._resolve_identity()
        model = await self._resolve_model(identity)
        output: list[tuple[float, ...] | None] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for start in range(0, len(texts), _MAX_BATCH):
                chunk = list(texts[start : start + _MAX_BATCH])
                output.extend(await self._embed_chunk(client, chunk, model, identity))
        return output

    async def _resolve_identity(self) -> CloudGatewayIdentity:
        if self._identity is not None:
            return self._identity
        if self._identity_resolver is None:
            raise CloudIdentityUnavailable(
                "cloud gateway embedding calls require cloud account identity",
            )
        actor = current_cloud_actor()
        if actor is None:
            raise CloudIdentityUnavailable(
                "cloud gateway embedding calls require an ambient cloud actor",
            )
        context = (
            CloudResourceContext.for_character(actor.character)
            if actor.character is not None
            else CloudResourceContext.for_account(actor.operator_id or "")
        )
        return await self._identity_resolver.resolve_context(context)

    async def _resolve_model(self, identity: CloudGatewayIdentity) -> str:
        if self._routing_profile_port is None:
            return self._default_model
        try:
            profile = await self._routing_profile_port.get_profile(
                tenant_id=identity.tenant_id,
                account_id=identity.account_id,
                user_id=identity.account_id,
                tier=identity.tenant_tier,
            )
        except Exception as exc:
            raise EmbedderError("cloud embedding routing profile unavailable") from exc
        if profile.is_disabled(_CAPABILITY, self._feature_key):
            raise EmbedderError(
                f"cloud embedding feature {self._feature_key!r} is disabled "
                f"by policy [{profile.source}]",
            )
        # This field is optional until User/CoreProfileResponse is extended. Do
        # not apply strict_no_fallback here: missing embedding support must
        # gracefully use the env/default preset during the compatibility window.
        return profile.preset_for(_CAPABILITY, self._feature_key) or self._default_model

    async def _embed_chunk(
        self,
        client: httpx.AsyncClient,
        chunk: list[str],
        model: str,
        identity: CloudGatewayIdentity,
    ) -> list[tuple[float, ...]]:
        payload = {"model": model, "input": chunk}
        try:
            response = await client.post(
                f"{self._base_url}/v1/embeddings",
                headers=self._headers(identity),
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            _LOGGER.warning(
                "cloud embedding gateway request failed feature=%s account=%s: %s",
                self._feature_key,
                identity.account_id,
                exc,
            )
            raise EmbedderError(
                f"Cloud Gateway /v1/embeddings HTTP failure for {len(chunk)} texts",
            ) from exc
        except ValueError as exc:
            raise EmbedderError(
                "Cloud Gateway /v1/embeddings returned non-JSON body",
            ) from exc

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise EmbedderError("Cloud Gateway embedding response missing 'data' array")
        ordered: list[tuple[float, ...] | None] = [None] * len(chunk)
        for entry in data:
            if not isinstance(entry, dict):
                continue
            raw_vector = entry.get("embedding")
            if not isinstance(raw_vector, list):
                continue
            try:
                vector = tuple(float(value) for value in raw_vector)
            except (TypeError, ValueError) as exc:
                raise EmbedderError(
                    "Cloud Gateway embedding response contains a non-numeric vector",
                ) from exc
            index = entry.get("index")
            if isinstance(index, int) and 0 <= index < len(chunk):
                ordered[index] = vector
                continue
            for position, current in enumerate(ordered):
                if current is None:
                    ordered[position] = vector
                    break
        missing = [index for index, vector in enumerate(ordered) if vector is None]
        if missing:
            raise EmbedderError(
                f"Cloud Gateway embedding response missed {len(missing)} inputs",
            )
        return [vector for vector in ordered if vector is not None]

    def _headers(self, identity: CloudGatewayIdentity) -> dict[str, str]:
        request_id = f"embedding-{uuid.uuid4().hex}"
        self.last_request_id = request_id
        return {
            "Authorization": f"Bearer {self._deployment_token}",
            "X-Yuralume-Deployment": self._deployment_id,
            "X-Yuralume-Audience": self._audience,
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
            "X-Yuralume-Tenant": identity.tenant_id,
            "X-Yuralume-Account": identity.account_id,
            "X-Yuralume-Feature": self._feature_key,
            "X-Yuralume-Character": identity.character_ref,
            # EC7: origin attribution, present only for a managed character.
            **character_origin_headers(identity),
            TRIGGER_HEADER_NAME: generation_trigger_header_value(),
        }
