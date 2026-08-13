"""Hosted cloud gateway video adapter.

Speaks two protocols against the same Gateway, deliberately kept apart:

* ``generate()`` — the legacy **synchronous** ``POST /v1/videos/generations``
  call. One await, up to 30 minutes. Untouched by CV1.
* :class:`~kokoro_link.contracts.async_video_job.AsyncVideoJobPort` — the
  **asynchronous** ``/v1/videos/jobs`` trio, which returns a ticket in
  milliseconds and lets the caller poll. This is the only adapter that
  implements it, which is precisely what keeps the new start-frame
  pipeline off every self-host deployment.

Async support is additionally **opt-in per instance** (``jobs_enabled``),
so a hosted deployment whose Gateway has not shipped the jobs endpoints
yet keeps working on the synchronous path instead of failing every post.

Wire contract: the Media Jobs Service spec, §3 (request / response
shapes) and §9.1 (error codes and what Core does about each).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any, NoReturn
from urllib.parse import urljoin
from uuid import uuid4

import httpx

from kokoro_link.contracts.async_video_job import (
    DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS,
    MAX_FIRST_FRAME_BYTES,
    MAX_VIDEO_JOB_TIMEOUT_SECONDS,
    MIN_VIDEO_JOB_TIMEOUT_SECONDS,
    VIDEO_JOB_CODE_NOT_FOUND,
    VIDEO_JOB_CODE_UNAVAILABLE,
    VIDEO_JOB_PIPELINE_I2V,
    VIDEO_JOB_STATUS_DEAD,
    VideoFirstFrame,
    VideoJobArtifact,
    VideoJobArtifactError,
    VideoJobRef,
    VideoJobRejectedError,
    VideoJobStatus,
)
from kokoro_link.contracts.cloud_gateway import (
    CloudGatewayIdentityResolverPort,
    CloudResourceContext,
    character_origin_headers,
)
from kokoro_link.contracts.generation_trigger import (
    TRIGGER_HEADER_NAME,
    generation_trigger_header_value,
)
from kokoro_link.contracts.video_provider import (
    VideoGenerationError,
    VideoNoOutputError,
    VideoTimeoutError,
)
from kokoro_link.infrastructure.http_error_logging import log_expected_refusal
from kokoro_link.infrastructure.llm.cloud_refusal import refusal_from_response
from kokoro_link.infrastructure.prompt.character_identity import (
    render_character_visual_identity_lines,
)
from kokoro_link.infrastructure.prompt.visual_subject import (
    render_character_visual_subject_lines,
)

ASPECT_TO_RATIO: dict[str, str] = {
    "portrait": "9:16",
    "landscape": "16:9",
    "square": "1:1",
}

_LOGGER = logging.getLogger(__name__)


class CloudGatewayVideoProvider:
    def __init__(
        self,
        *,
        base_url: str,
        deployment_token: str,
        preset: str,
        feature_key: str,
        identity_resolver: CloudGatewayIdentityResolverPort,
        deployment_id: str = "hosted-primary",
        audience: str = "yuralume-gateway",
        timeout_seconds: float = 1800.0,
        jobs_enabled: bool = False,
        job_pipeline: str = VIDEO_JOB_PIPELINE_I2V,
        job_http_timeout_seconds: float = 60.0,
        artifact_download_timeout_seconds: float = 300.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("cloud gateway video base_url is required")
        if not deployment_token.strip():
            raise ValueError("cloud gateway deployment_token is required")
        if not deployment_id.strip():
            raise ValueError("cloud gateway deployment_id is required")
        if not audience.strip():
            raise ValueError("cloud gateway audience is required")
        if not preset.strip():
            raise ValueError("cloud gateway video preset is required")
        self._base_url = base_url.rstrip("/")
        self._deployment_token = deployment_token
        self._deployment_id = deployment_id.strip()
        self._audience = audience.strip()
        self._preset = preset
        self._feature_key = feature_key.strip() or "video"
        self._identity_resolver = identity_resolver
        self._timeout = timeout_seconds
        self._jobs_enabled = bool(jobs_enabled)
        self._job_pipeline = (job_pipeline or "").strip()
        self._job_http_timeout = job_http_timeout_seconds
        self._artifact_download_timeout = artifact_download_timeout_seconds
        self.last_request_id = ""
        self.last_duration_seconds: float | None = None
        """CV0-3: additive signal the feed usage ledger reads instead of a
        hard-coded 81-frame/6-second assumption. Same shape as
        ``last_request_id`` — set after a successful ``generate()`` call."""
        self.last_job_request_id = ""
        """Deliberately *not* ``last_request_id``: the ledger reads that one
        immediately after ``generate()``, and a poll landing in between
        would silently re-attribute a synchronous render."""

    async def generate(
        self,
        *,
        character,
        positive: str,
        aspect: str = "portrait",
        length_frames: int = 81,
        recent_dialogue: str = "",
        use_runtime_state: bool = True,
        first_frame_url: str | None = None,
    ) -> bytes:
        # The synchronous ``/v1/videos/generations`` contract has no i2v
        # field; image-to-video on this adapter travels the async job path
        # below (as bytes, Media Jobs Service spec §2). Accepted here purely
        # for port conformance.
        del first_frame_url
        prompt = _build_prompt(
            character=character,
            positive=positive,
            recent_dialogue=recent_dialogue,
            use_runtime_state=use_runtime_state,
        )
        if not prompt.strip():
            raise VideoGenerationError("video prompt is empty")
        requested_duration_seconds = max(1, round(max(1, int(length_frames)) / 16))
        payload = {
            "model": self._preset,
            "prompt": prompt,
            "aspect_ratio": ASPECT_TO_RATIO.get(
                aspect,
                ASPECT_TO_RATIO["portrait"],
            ),
            "duration_seconds": requested_duration_seconds,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/v1/videos/generations",
                    headers=await self._headers(character),
                    json=payload,
                )
                data = _json_or_raise(response)
                blob = await _video_bytes_from_response(
                    data,
                    client=client,
                    base_url=self._base_url,
                )
                # Prefer the Gateway's own reported duration when present —
                # the upstream renderer may trim/extend the clip — and fall
                # back to what we requested otherwise.
                self.last_duration_seconds = _coerce_positive_seconds(
                    data.get("duration_seconds"),
                    fallback=requested_duration_seconds,
                )
                return blob
        except httpx.TimeoutException as exc:
            raise VideoTimeoutError("cloud video gateway timed out") from exc
        except VideoGenerationError:
            raise
        except Exception as exc:
            raise VideoGenerationError(str(exc)) from exc

    async def _headers(self, character) -> dict[str, str]:
        return await self._identity_headers(
            character, request_id_prefix="vid-", record_last=True,
        )

    async def _identity_headers(
        self,
        character,
        *,
        request_id_prefix: str,
        record_last: bool,
    ) -> dict[str, str]:
        """Resolve identity and build the ``X-Yuralume-*`` header set.

        Ordering matters and is load-bearing: identity resolution happens
        *before* the request id is minted so a resolver failure leaves
        ``last_request_id`` pointing at the previous successful call
        rather than at a request that was never sent.
        """
        identity = await self._identity_resolver.resolve_context(
            CloudResourceContext.for_character(character),
        )
        request_id = f"{request_id_prefix}{uuid4().hex}"
        if record_last:
            self.last_request_id = request_id
        else:
            self.last_job_request_id = request_id
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
        }

    # --- AsyncVideoJobPort ------------------------------------------------

    def supports_async_video_jobs(self) -> bool:
        return self._jobs_enabled and bool(self._job_pipeline)

    async def submit_video_job(
        self,
        *,
        character,
        prompt: str,
        first_frame: VideoFirstFrame,
        duration_seconds: float = 5.0,
        aspect: str = "portrait",
        negative_prompt: str = "",
        seed: int | None = None,
        timeout_seconds: int = DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS,
        client_job_key: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> VideoJobRef:
        self._require_jobs_enabled()
        storyboard = (prompt or "").strip()
        if not storyboard:
            raise VideoGenerationError("video job prompt is empty")
        payload = self._submit_payload(
            prompt=storyboard,
            first_frame=first_frame,
            duration_seconds=duration_seconds,
            aspect=aspect,
            negative_prompt=negative_prompt,
            seed=seed,
            timeout_seconds=timeout_seconds,
            client_job_key=client_job_key,
            metadata=metadata,
        )
        headers = await self._job_headers(character)
        data = await self._job_call(
            "POST",
            f"{self._base_url}/v1/videos/jobs",
            headers=headers,
            json_body=payload,
            operation="video job submit",
        )
        return VideoJobRef(
            status=VideoJobStatus.from_wire(data),
            idempotent_replay=bool(data.get("idempotent_replay", False)),
            queue_depth=_queue_field(data, "depth"),
            queue_capacity=_queue_field(data, "capacity"),
        )

    async def poll_video_job(
        self,
        *,
        character,
        job_id: str,
    ) -> VideoJobStatus:
        return await self._job_status_call(
            "GET", character=character, job_id=job_id,
            operation="video job poll",
        )

    async def cancel_video_job(
        self,
        *,
        character,
        job_id: str,
    ) -> VideoJobStatus:
        return await self._job_status_call(
            "DELETE", character=character, job_id=job_id,
            operation="video job cancel",
        )

    async def download_video_artifact(
        self,
        artifact: VideoJobArtifact,
    ) -> bytes:
        url = (artifact.url or "").strip()
        if not url:
            raise VideoJobArtifactError("video artifact has no download URL")
        try:
            async with httpx.AsyncClient(
                timeout=self._artifact_download_timeout,
            ) as client:
                # The presigned URL may 30x to the object store's real
                # host; a redirect body is XML, not mp4.
                response = await client.get(url, follow_redirects=True)
        except httpx.TimeoutException as exc:
            raise VideoTimeoutError("video artifact download timed out") from exc
        except VideoGenerationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized below
            raise VideoJobArtifactError(str(exc)) from exc
        if not response.is_success:
            raise VideoJobArtifactError(
                f"video artifact download failed: {response.status_code}",
            )
        blob = response.content
        _verify_artifact(blob, artifact)
        return blob

    # --- job plumbing -----------------------------------------------------

    def _require_jobs_enabled(self) -> None:
        if not self.supports_async_video_jobs():
            raise VideoJobRejectedError(
                "cloud gateway video jobs are not enabled for this adapter",
                code=VIDEO_JOB_CODE_UNAVAILABLE,
                retryable=False,
            )

    async def _job_headers(self, character) -> dict[str, str]:
        return await self._identity_headers(
            character, request_id_prefix="vjob-", record_last=False,
        )

    def _submit_payload(
        self,
        *,
        prompt: str,
        first_frame: VideoFirstFrame,
        duration_seconds: float,
        aspect: str,
        negative_prompt: str,
        seed: int | None,
        timeout_seconds: int,
        client_job_key: str | None,
        metadata: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pipeline": self._job_pipeline,
            "prompt": prompt,
            "first_frame": _first_frame_payload(first_frame),
            "duration_seconds": _clamp_duration(duration_seconds),
            "aspect_ratio": ASPECT_TO_RATIO.get(
                aspect,
                ASPECT_TO_RATIO["portrait"],
            ),
            "timeout_seconds": _clamp_job_timeout(timeout_seconds),
        }
        if seed is not None:
            payload["seed"] = int(seed)
        negative = (negative_prompt or "").strip()
        if negative:
            payload["negative_prompt"] = negative
        key = (client_job_key or "").strip()
        if key:
            payload["client_job_key"] = key
        if metadata:
            payload["metadata"] = {
                str(name): str(value) for name, value in metadata.items()
            }
        return payload

    async def _job_status_call(
        self,
        method: str,
        *,
        character,
        job_id: str,
        operation: str,
    ) -> VideoJobStatus:
        self._require_jobs_enabled()
        identifier = (job_id or "").strip()
        if not identifier:
            raise VideoGenerationError("video job id is empty")
        headers = await self._job_headers(character)
        data = await self._job_call(
            method,
            f"{self._base_url}/v1/videos/jobs/{identifier}",
            headers=headers,
            json_body=None,
            operation=operation,
            job_id=identifier,
        )
        return VideoJobStatus.from_wire(data)

    async def _job_call(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None,
        operation: str,
        job_id: str = "",
    ) -> Mapping[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self._job_http_timeout,
            ) as client:
                response = await client.request(
                    method, url, headers=dict(headers), json=json_body,
                )
        except httpx.TimeoutException as exc:
            raise VideoTimeoutError(f"{operation} timed out") from exc
        except VideoGenerationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized below
            raise VideoGenerationError(str(exc)) from exc
        if response.status_code >= 400:
            if response.status_code == 404 and job_id:
                # A vanished / foreign job is indistinguishable from a
                # terminal one for us (§3.2) — surfacing it as a status
                # rather than an exception keeps the branch out of every
                # poll site.
                return _not_found_wire(job_id)
            _raise_job_error(response, operation=operation)
        payload = _json_or_none(response)
        if payload is None:
            raise VideoJobRejectedError(
                f"{operation} returned a non-object body",
                code=VIDEO_JOB_CODE_UNAVAILABLE,
                retryable=True,
                status_code=response.status_code,
            )
        return payload


def _build_prompt(
    *,
    character,
    positive: str,
    recent_dialogue: str,
    use_runtime_state: bool,
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
    return "\n".join(part for part in parts if part.strip())


async def _video_bytes_from_response(
    data: Mapping,
    *,
    client: httpx.AsyncClient,
    base_url: str,
) -> bytes:
    items = data.get("data") or data.get("artifacts")
    if not isinstance(items, list):
        raise VideoNoOutputError("cloud video gateway returned no data array")
    for item in items:
        if not isinstance(item, Mapping):
            continue
        b64 = item.get("b64_json") or item.get("b64")
        if isinstance(b64, str) and b64:
            return base64.b64decode(b64)
        url = item.get("url")
        if isinstance(url, str) and url:
            return await _download_bytes(client=client, url=url, base_url=base_url)
    raise VideoNoOutputError("cloud video gateway produced no video")


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
        raise VideoGenerationError(
            f"video artifact download failed: {response.status_code}",
        )
    return response.content


def _coerce_positive_seconds(value: object, *, fallback: float) -> float:
    """Best-effort numeric coercion for a gateway-reported duration.

    Any missing / non-numeric / non-positive value falls back to what we
    actually requested rather than propagate a malformed billing number.
    """
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return seconds if seconds > 0 else fallback


def _first_frame_payload(first_frame: VideoFirstFrame) -> dict[str, Any]:
    """Serialize the start frame per the Media Jobs Service spec §3.1.

    The size check is local on purpose: an oversized frame would spend a
    12 MiB upload only to earn a ``400``, and the caller's response to
    both is identical (publish the frame as an image post).
    """
    payload: dict[str, Any] = {"content_type": first_frame.content_type}
    if first_frame.data is not None:
        if first_frame.exceeds_wire_budget:
            raise VideoJobRejectedError(
                "video job start frame exceeds the wire budget "
                f"({first_frame.byte_size} bytes > {MAX_FIRST_FRAME_BYTES})",
                code="first_frame_too_large",
                retryable=False,
            )
        payload["data_base64"] = base64.b64encode(first_frame.data).decode()
    else:
        # V0 brokers answer this with ``400 first_frame_source_unsupported``;
        # we still send it so the reserved V1 path needs no adapter change.
        payload["source_url"] = first_frame.source_url
    return payload


def _clamp_duration(seconds: float) -> int:
    try:
        value = round(float(seconds))
    except (TypeError, ValueError):
        value = 5
    return max(1, min(value, 20))


def _clamp_job_timeout(seconds: int) -> int:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        value = DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS
    return max(
        MIN_VIDEO_JOB_TIMEOUT_SECONDS,
        min(value, MAX_VIDEO_JOB_TIMEOUT_SECONDS),
    )


def _queue_field(data: Mapping[str, Any], key: str) -> int | None:
    queue = data.get("queue")
    if not isinstance(queue, Mapping):
        return None
    try:
        return int(queue[key])
    except (KeyError, TypeError, ValueError):
        return None


def _not_found_wire(job_id: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": VIDEO_JOB_STATUS_DEAD,
        "error": {
            "code": VIDEO_JOB_CODE_NOT_FOUND,
            "message": "media job is unknown or belongs to another tenant",
            "retryable": False,
        },
    }


def _verify_artifact(blob: bytes, artifact: VideoJobArtifact) -> None:
    """Refuse a clip that does not match what the broker recorded.

    A truncated download is the dangerous failure here: it would be
    published as a real post. Degrading to the start-frame image instead
    is always the better outcome, so both checks raise.
    """
    if artifact.byte_size is not None and len(blob) != artifact.byte_size:
        raise VideoJobArtifactError(
            "video artifact size mismatch: "
            f"{len(blob)} bytes downloaded, {artifact.byte_size} expected",
        )
    expected = (artifact.sha256 or "").strip().lower()
    if not expected:
        return
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise VideoJobArtifactError(
            "video artifact checksum mismatch",
        )


def _json_or_none(response: httpx.Response) -> Mapping[str, Any] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _raise_job_error(response: httpx.Response, *, operation: str) -> NoReturn:
    """Turn a non-2xx jobs response into a typed rejection.

    Deliberately funnels 4xx-with-``retryable:false`` through the shared
    refusal classifier first, so ``expected_refusal_code()`` keeps working
    across the ``raise ... from`` chain exactly like it does for the
    synchronous media adapters.
    """
    body = response.text
    envelope = _error_envelope(body)
    if envelope is None:
        code, message, retryable = (
            VIDEO_JOB_CODE_UNAVAILABLE,
            f"{operation} failed with {response.status_code}",
            response.status_code >= 500,
        )
    else:
        code, message, retryable = envelope
    refusal = refusal_from_response(response, body)
    error = VideoJobRejectedError(
        f"{operation} refused ({code})" if refusal is not None
        else f"{operation} failed ({code}): {message}",
        code=code,
        retryable=retryable,
        status_code=response.status_code,
        retry_after_seconds=_retry_after_seconds(response),
    )
    if refusal is not None:
        log_expected_refusal(
            _LOGGER,
            response,
            operation=f"cloud {operation}",
            code=refusal.code,
            message=refusal.reason,
        )
        raise error from refusal
    raise error


def _error_envelope(body_text: str) -> tuple[str, str, bool] | None:
    payload = None
    try:
        payload = json.loads(body_text)
    except (TypeError, ValueError):
        return None
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(error, Mapping):
        return None
    code = str(error.get("code") or "").strip()
    if not code:
        return None
    return code, str(error.get("message") or ""), bool(error.get("retryable"))


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _json_or_raise(response: httpx.Response) -> Mapping:
    if response.status_code >= 400:
        refusal = refusal_from_response(response, response.text)
        if refusal is not None:
            log_expected_refusal(
                _LOGGER,
                response,
                operation="cloud video gateway",
                code=refusal.code,
                message=refusal.reason,
            )
            raise VideoGenerationError(
                f"video gateway refused ({refusal.code})",
            ) from refusal
        raise VideoGenerationError(
            f"video gateway error {response.status_code}: {response.text}",
        )
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise VideoGenerationError("video gateway returned non-object JSON")
    return payload
