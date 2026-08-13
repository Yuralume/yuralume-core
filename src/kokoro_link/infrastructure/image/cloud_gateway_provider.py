from __future__ import annotations

import base64
import json
import logging
import mimetypes
from collections.abc import Mapping, Sequence
from urllib.parse import urljoin
from uuid import uuid4

import httpx

from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentityResolverPort,
    CloudResourceContext,
    character_origin_headers,
)
from kokoro_link.contracts.generation_trigger import (
    TRIGGER_HEADER_NAME,
    generation_trigger_header_value,
)
from kokoro_link.contracts.interaction_context import (
    covered_call_receipt,
    interaction_headers,
)
from kokoro_link.contracts.image_provider import (
    ImageGenerationError,
    ImageNoOutputError,
    ImageTimeoutError,
    ImageTokenUsage,
)
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.infrastructure.http_error_logging import (
    log_expected_refusal,
    log_http_error_response,
)
from kokoro_link.infrastructure.image.reference_image_budget import (
    DEFAULT_INLINE_PAYLOAD_BUDGET_BYTES,
    DEFAULT_MAX_REFERENCE_IMAGE_BYTES,
    fit_reference_image,
)
from kokoro_link.infrastructure.llm.cloud_refusal import (
    classify_refusal,
    refusal_from_response,
)
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_visual_identity_lines,
)
from kokoro_link.infrastructure.prompt.visual_subject import (
    render_character_visual_subject_lines,
)

ASPECT_TO_SIZE: dict[str, str] = {
    "portrait": "1024x1536",
    "landscape": "1536x1024",
    "square": "1024x1024",
}

_LOGGER = logging.getLogger(__name__)


class CloudGatewayImageProvider:
    provider_id = "cloud_gateway"

    def __init__(
        self,
        *,
        base_url: str,
        deployment_token: str,
        preset: str,
        feature_key: str,
        identity_resolver: CloudGatewayIdentityResolverPort,
        object_storage: ObjectStoragePort | None = None,
        deployment_id: str = "hosted-primary",
        audience: str = "yuralume-gateway",
        timeout_seconds: float = 180.0,
        max_reference_image_bytes: int = DEFAULT_MAX_REFERENCE_IMAGE_BYTES,
        inline_payload_budget_bytes: int = DEFAULT_INLINE_PAYLOAD_BUDGET_BYTES,
    ) -> None:
        if not base_url.strip():
            raise ValueError("cloud gateway image base_url is required")
        if not deployment_token.strip():
            raise ValueError("cloud gateway deployment_token is required")
        if not deployment_id.strip():
            raise ValueError("cloud gateway deployment_id is required")
        if not audience.strip():
            raise ValueError("cloud gateway audience is required")
        if not preset.strip():
            raise ValueError("cloud gateway image preset is required")
        self._base_url = base_url.rstrip("/")
        self._deployment_token = deployment_token
        self._deployment_id = deployment_id.strip()
        self._audience = audience.strip()
        self._preset = preset
        self._feature_key = feature_key.strip() or "image"
        self._identity_resolver = identity_resolver
        # EC6: reference art lives in this deployment's own object storage, at
        # a URL only this deployment can serve. The upstream model is on the
        # far side of the Gateway and cannot fetch it, so the bytes have to
        # travel *in* the request — this port is how the adapter reads them.
        self._object_storage = object_storage
        # FC1: the art rides in the body, so its size is the request's size.
        # Both ceilings are constructor state rather than constants because
        # the Gateway's own inbound limit is deployment-configurable — a
        # deployment that raises one has to be able to raise these.
        self._max_reference_image_bytes = max(1, int(max_reference_image_bytes))
        self._inline_payload_budget_bytes = max(1, int(inline_payload_budget_bytes))
        self._timeout = timeout_seconds
        self.last_request_id = ""
        self.last_provider_id = self.provider_id
        self.last_model_id = preset
        self.last_usage: ImageTokenUsage | None = None
        self.last_cost_amount_usd: float | None = None

    async def generate(
        self,
        *,
        character,
        positive: str,
        aspect: str = "portrait",
        batch: int = 1,
        recent_dialogue: str = "",
        use_runtime_state: bool = True,
        user_attachment_urls: Sequence[str] = (),
    ) -> list[bytes]:
        self.last_provider_id = self.provider_id
        self.last_model_id = self._preset
        self.last_usage = None
        self.last_cost_amount_usd = None
        # Anything that can be read becomes a real image input; anything that
        # cannot (no storage wired, an unreadable object) stays a URL in the
        # prompt exactly as before, so a deployment that never had reference
        # art keeps sending byte-identical requests.
        inline_images, unresolved_urls = await self._inline_image_inputs(
            user_attachment_urls,
        )
        prompt = _build_prompt(
            character=character,
            positive=positive,
            recent_dialogue=recent_dialogue,
            use_runtime_state=use_runtime_state,
            user_attachment_urls=unresolved_urls,
            attached_image_count=len(inline_images),
        )
        if not prompt.strip():
            raise ImageGenerationError("image prompt is empty")
        payload: dict[str, object] = {
            "model": self._preset,
            "prompt": prompt,
            "size": ASPECT_TO_SIZE.get(aspect, ASPECT_TO_SIZE["portrait"]),
            "n": max(1, min(int(batch), 4)),
        }
        if inline_images:
            # ``image`` as a list of data URIs is the OpenAI images-API name
            # for "draw from these", and the Gateway deep-copies the request
            # body onto the upstream call, so the field arrives untouched at
            # whichever preset is configured — no Gateway change, and the same
            # seam whether that preset is gpt-image or Gemini.
            payload["image"] = inline_images
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                endpoint = f"{self._base_url}/v1/images/generations"
                response = await client.post(
                    endpoint,
                    headers=await self._headers(character),
                    json=payload,
                )
                if inline_images and _image_input_may_be_at_fault(response):
                    # Whether the configured preset accepts image inputs at
                    # all is an upstream property this adapter cannot know
                    # ahead of time (D5 left it to deployment verification).
                    # Finding out by failing the whole draw would mean the
                    # player sees nothing; finding out by dropping the lock
                    # means they see their character, drawn free-hand.
                    _LOGGER.warning(
                        "likeness lock degraded: cloud image gateway rejected "
                        "the inline image input (status=%s); retrying once "
                        "without it, so this picture is drawn unanchored",
                        response.status_code,
                    )
                    payload.pop("image", None)
                    # A prompt that still announces attached references would
                    # be describing inputs this retry just removed.
                    payload["prompt"] = _build_prompt(
                        character=character,
                        positive=positive,
                        recent_dialogue=recent_dialogue,
                        use_runtime_state=use_runtime_state,
                        user_attachment_urls=unresolved_urls,
                        attached_image_count=0,
                    )
                    response = await client.post(
                        endpoint,
                        headers=await self._headers(character),
                        json=payload,
                    )
                data = _json_or_raise(response, "image")
                # Only a call the Gateway actually waived is paid for by the
                # covering action charge, and only once the bytes are in hand:
                # a job the Gateway billed itself, or an answer whose artifact
                # never downloads, has bought the player nothing and must stay
                # refundable (see ``CoveredCallReceipt``).
                receipt = covered_call_receipt(response.headers)
                self._capture_usage_metadata(data)
                images = await _image_bytes_from_response(
                    data,
                    client=client,
                    base_url=self._base_url,
                )
                # Bytes in memory are not yet what the player bought — the
                # picture only exists once it is in object storage. The caller
                # that persists them confirms this receipt; a storage failure
                # therefore refunds instead of billing for an invisible image.
                receipt.defer_delivery()
                return images
        except httpx.TimeoutException as exc:
            raise ImageTimeoutError("cloud image gateway timed out") from exc
        except ImageGenerationError:
            raise
        except Exception as exc:
            raise ImageGenerationError(str(exc)) from exc

    async def _inline_image_inputs(
        self,
        urls: Sequence[str],
    ) -> tuple[list[str], tuple[str, ...]]:
        """Split ``urls`` into inline image inputs and leftover URLs.

        A reference image that cannot be read is logged and dropped rather
        than raised: the picture still gets drawn, just without the likeness
        lock. The opposite choice would turn one missing object in storage
        into a total outage of every generation surface for that character.

        The same fail-soft covers the request's byte budget (FC1). Inputs
        are consumed in order and stop at the ceiling, which is why the
        reference art leading the sequence matters twice over: it decides
        the face, and it is the one input guaranteed a place in the body.
        """
        if not urls:
            return [], ()
        inline: list[str] = []
        unresolved: list[str] = []
        remaining = self._inline_payload_budget_bytes
        for url in urls:
            data_uri = await self._as_data_uri(url)
            if data_uri is None:
                unresolved.append(url)
                continue
            if len(data_uri) > remaining:
                # Deliberately not added to ``unresolved``: these URLs are
                # either this deployment's own (unfetchable from upstream)
                # or a data URI whose whole problem is its length. Naming
                # them in the prompt would trade a size failure for noise.
                _LOGGER.warning(
                    "dropping an inline image input: it needs %d bytes of a "
                    "request budget with %d left; drawing without it",
                    len(data_uri), remaining,
                )
                continue
            remaining -= len(data_uri)
            inline.append(data_uri)
        return inline, tuple(unresolved)

    async def _as_data_uri(self, url: str) -> str | None:
        if not isinstance(url, str) or not url:
            return None
        if url.startswith("data:"):
            return url
        if self._object_storage is None:
            return None
        try:
            object_key = self._object_storage.object_key_from_url(url)
            if not object_key:
                return None
            content = await self._object_storage.get_bytes(
                object_key=object_key,
            )
        except Exception:  # noqa: BLE001 — a lost object must not kill the draw
            _LOGGER.warning(
                "reference image could not be read for image generation; "
                "drawing without it",
                exc_info=True,
            )
            return None
        if not content:
            return None
        fitted = fit_reference_image(
            content,
            media_type=_image_media_type(object_key),
            max_bytes=self._max_reference_image_bytes,
        )
        if fitted is None:
            return None
        encoded = base64.b64encode(fitted.content).decode("ascii")
        return f"data:{fitted.media_type};base64,{encoded}"

    def _capture_usage_metadata(self, data: Mapping) -> None:
        self.last_usage = ImageTokenUsage.from_mapping(data.get("usage"))
        model = data.get("model")
        if isinstance(model, str) and model:
            self.last_model_id = model
        yuralume = data.get("yuralume")
        if not isinstance(yuralume, Mapping):
            return
        provider = yuralume.get("provider")
        if isinstance(provider, str) and provider:
            self.last_provider_id = provider
        provider_model = yuralume.get("provider_model")
        if isinstance(provider_model, str) and provider_model:
            self.last_model_id = provider_model
        cost = yuralume.get("cost_estimate")
        if isinstance(cost, Mapping):
            amount = cost.get("total_usd")
            if isinstance(amount, int | float):
                self.last_cost_amount_usd = float(amount)

    async def _headers(self, character) -> dict[str, str]:
        identity = await self._identity_resolver.resolve_context(
            CloudResourceContext.for_character(character),
        )
        request_id = f"img-{uuid4().hex}"
        self.last_request_id = request_id
        return {
            "Authorization": f"Bearer {self._deployment_token}",
            "X-Yuralume-Deployment": self._deployment_id,
            "X-Yuralume-Audience": self._audience,
            "X-Request-Id": request_id,
            "X-Yuralume-Tenant": identity.tenant_id,
            "X-Yuralume-Account": identity.account_id,
            "X-Yuralume-Feature": self._feature_key,
            "X-Yuralume-Character": identity.character_ref,
            # EC7: origin attribution, present only for a managed character.
            **character_origin_headers(identity),
            TRIGGER_HEADER_NAME: generation_trigger_header_value(),
            # ``image_portrait`` / ``image_chat_tool`` are action-priced
            # entries, so their Gateway call must name the charge that already
            # covers it or the player pays twice. Empty outside a scope.
            **interaction_headers(),
        }


#: Gateway error codes that name the request body itself as the problem.
#: ``provider_error`` is the Gateway's relabelling of *any* upstream 4xx
#: (see its ``OpenAiCompatibleImageProvider``), which is what an images API
#: with no ``image`` parameter answers; ``payload_too_large`` is the
#: Gateway's own inbound ceiling.
_IMAGE_INPUT_FAULT_CODES = frozenset({"provider_error", "payload_too_large"})


def _image_input_may_be_at_fault(response: httpx.Response) -> bool:
    """Is this failure plausibly caused by the inline ``image`` field?

    The distinction that matters is *whose* decision the failure was. A
    deliberate control-plane refusal — no credits, entitlement revoked — is
    about the account, and will refuse the retry just as flatly while
    costing a second round trip and logging a degradation that never
    happened. Everything else in the 4xx family, plus the two codes that
    name the body outright, is the request being rejected as *shaped*, and
    the inline image is the only part of that shape EC6 added.
    """
    status = response.status_code
    if status < 400:
        return False
    body_text = response.text
    code = _gateway_error_code(body_text)
    if code in _IMAGE_INPUT_FAULT_CODES or status == 413:
        return True
    if classify_refusal(status, body_text) is not None:
        return False
    return status < 500


def _gateway_error_code(body_text: str) -> str:
    try:
        payload = json.loads(body_text)
    except (json.JSONDecodeError, TypeError):
        return ""
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(error, Mapping):
        return ""
    code = error.get("code")
    return code if isinstance(code, str) else ""


def _image_media_type(object_key: str) -> str:
    """Media type for a stored image, read off its key.

    Official art keys carry the source extension (EC4) and player uploads
    keep theirs, so the key is the cheapest honest answer — cheaper than a
    second storage round trip on a foreground generation path.
    """
    guessed, _ = mimetypes.guess_type(object_key)
    if guessed and guessed.startswith("image/"):
        return guessed
    return "image/png"


def _build_prompt(
    *,
    character,
    positive: str,
    recent_dialogue: str,
    use_runtime_state: bool,
    user_attachment_urls: Sequence[str],
    attached_image_count: int = 0,
) -> str:
    parts = [
        f"Character: {character.name}",
        f"Appearance: {getattr(character, 'appearance', '')}",
        *render_character_visual_identity_lines(character),
        *render_character_visual_subject_lines(character),
    ]
    if use_runtime_state:
        state = getattr(character, "state", None)
        if state is not None:
            parts.append(f"Current emotion: {getattr(state, 'emotion', '')}")
            intent = getattr(state, "current_intent", None)
            if intent:
                parts.append(f"Current intent: {intent}")
    if positive.strip():
        parts.append(f"Scene: {positive.strip()}")
    if recent_dialogue.strip():
        parts.append(f"Recent dialogue context: {recent_dialogue.strip()}")
    if attached_image_count:
        # The images ride in the request body; this line is what tells the
        # model they are the subject rather than decoration.
        parts.append(
            f"Reference images attached: {attached_image_count}. "
            "Treat the attached image(s) as the visual source of truth for "
            "this character's likeness and style, and render the requested "
            "scene with that same character.",
        )
    if user_attachment_urls:
        parts.append(
            "User visual references: "
            + ", ".join(str(url) for url in user_attachment_urls),
        )
    return "\n".join(part for part in parts if part.strip())


async def _image_bytes_from_response(
    data: Mapping,
    *,
    client: httpx.AsyncClient,
    base_url: str,
) -> list[bytes]:
    out: list[bytes] = []
    items = data.get("data")
    if not isinstance(items, list):
        raise ImageNoOutputError("cloud image gateway returned no data array")
    for item in items:
        if not isinstance(item, Mapping):
            continue
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            out.append(base64.b64decode(b64))
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            out.append(await _download_bytes(client=client, url=url, base_url=base_url))
    if not out:
        raise ImageNoOutputError("cloud image gateway produced no images")
    return out


async def _download_bytes(
    *,
    client: httpx.AsyncClient,
    url: str,
    base_url: str,
) -> bytes:
    resolved = url if url.startswith(("http://", "https://")) else urljoin(
        f"{base_url}/",
        url.lstrip("/"),
    )
    response = await client.get(resolved)
    if response.status_code >= 400:
        log_http_error_response(
            _LOGGER,
            response,
            operation="cloud image artifact download",
        )
        raise ImageGenerationError(
            f"image artifact download failed: {response.status_code}",
        )
    return response.content


def _json_or_raise(response: httpx.Response, label: str) -> Mapping:
    if response.status_code >= 400:
        operation = f"cloud {label} gateway"
        body_text = response.text
        # A deliberate control-plane refusal (no credits, entitlement revoked)
        # is not a fault: log it quietly and keep the machine-readable code on
        # the cause chain so the HTTP boundary can answer the player properly.
        refusal = refusal_from_response(response, body_text)
        if refusal is not None:
            log_expected_refusal(
                _LOGGER,
                response,
                operation=operation,
                code=refusal.code,
                message=refusal.reason,
            )
            raise ImageGenerationError(
                f"{label} gateway refused ({refusal.code})",
            ) from refusal
        log_http_error_response(
            _LOGGER,
            response,
            operation=operation,
            body_text=body_text,
        )
        raise ImageGenerationError(
            f"{label} gateway error {response.status_code}: {body_text}",
        )
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ImageGenerationError(f"{label} gateway returned non-object JSON")
    return payload
