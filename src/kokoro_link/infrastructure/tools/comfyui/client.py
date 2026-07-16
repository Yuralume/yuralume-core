"""Async ComfyUI client.

Ported from ``ComfyGenPicture.clients.comfyui`` with two changes:

1. **httpx.AsyncClient** instead of urllib — keeps the chat event
   loop happy during the 10-60s an Illustrious generation takes.
2. **Poll /history** instead of the websocket-based wait_for_completion.
   One fewer dependency; the difference in latency-to-return is
   bounded by ``poll_interval`` (default 1 s) which is well below the
   generation time itself.

The client is stateless — no per-request credentials or session
warm-up are needed; ComfyUI runs locally with no auth by default.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

import httpx

_LOGGER = logging.getLogger(__name__)


class ComfyUiError(Exception):
    """Raised when ComfyUI returns an unexpected response."""


class ComfyUiTimeout(ComfyUiError):
    """Raised when a generation doesn't finish within the timeout."""


class AsyncComfyUiClient:
    def __init__(
        self,
        server: str,
        *,
        http_timeout: float = 10.0,
        poll_interval: float = 1.0,
        generation_timeout: float = 180.0,
    ) -> None:
        # Accept either ``host:port`` or full ``http://host:port``.
        if not server.startswith(("http://", "https://")):
            server = f"http://{server}"
        self._base_url = server.rstrip("/")
        self._client_id = str(uuid.uuid4())
        self._http_timeout = http_timeout
        self._poll_interval = poll_interval
        self._generation_timeout = generation_timeout

    async def queue_prompt(self, prompt: dict) -> str:
        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            resp = await client.post(
                f"{self._base_url}/prompt",
                json={"prompt": prompt, "client_id": self._client_id},
            )
            resp.raise_for_status()
            data = resp.json()
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise ComfyUiError(f"queue_prompt missing prompt_id: {data}")
        return prompt_id

    async def list_checkpoints(self) -> list[str]:
        """Return the checkpoint filenames ComfyUI advertises.

        Reads ``/object_info/CheckpointLoaderSimple`` and pulls the enum
        of ``ckpt_name`` values (the same list the ComfyUI web UI shows
        in its checkpoint dropdown). Any transport / shape error raises
        ``ComfyUiError`` so the caller can degrade the admin field to a
        plain text input rather than blocking the whole provider form.
        """
        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            resp = await client.get(
                f"{self._base_url}/object_info/CheckpointLoaderSimple",
            )
            resp.raise_for_status()
            data = resp.json()
        try:
            info = data["CheckpointLoaderSimple"]["input"]["required"]
            names = info["ckpt_name"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ComfyUiError(
                f"object_info missing ckpt_name enum: {exc}",
            ) from exc
        if not isinstance(names, list):
            raise ComfyUiError("ckpt_name enum is not a list")
        return [str(name) for name in names]

    async def wait_for_completion(self, prompt_id: str) -> dict:
        """Poll ``/history/{id}`` until the prompt appears — done.

        Returns the history entry (so the caller can walk node outputs
        without a second HTTP round-trip).
        """
        deadline = asyncio.get_event_loop().time() + self._generation_timeout
        async with httpx.AsyncClient(timeout=self._http_timeout) as client:
            while True:
                try:
                    resp = await client.get(
                        f"{self._base_url}/history/{prompt_id}",
                    )
                except httpx.RequestError as exc:
                    _LOGGER.warning("comfyui poll failed: %s", exc)
                else:
                    if resp.status_code == 200:
                        data = resp.json()
                        entry = data.get(prompt_id)
                        if entry is not None:
                            return entry
                if asyncio.get_event_loop().time() > deadline:
                    raise ComfyUiTimeout(
                        f"prompt {prompt_id} did not finish in "
                        f"{self._generation_timeout:.0f}s",
                    )
                await asyncio.sleep(self._poll_interval)

    async def download_image(
        self,
        *,
        filename: str,
        subfolder: str,
        folder_type: str,
    ) -> bytes:
        async with httpx.AsyncClient(timeout=self._http_timeout * 3) as client:
            resp = await client.get(
                f"{self._base_url}/view",
                params={
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": folder_type,
                },
            )
            resp.raise_for_status()
            return resp.content

    async def save_images(
        self,
        prompt_id: str,
        output_dir: Path,
        *,
        history_entry: dict | None = None,
    ) -> list[Path]:
        """Write every image from the completed prompt to disk.

        ``history_entry`` may be passed in when the caller already has
        it (from ``wait_for_completion``); otherwise we fetch once
        more.
        """
        if history_entry is None:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                resp = await client.get(
                    f"{self._base_url}/history/{prompt_id}",
                )
                resp.raise_for_status()
                history_entry = resp.json().get(prompt_id) or {}

        output_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        outputs = history_entry.get("outputs", {})
        for node_output in outputs.values():
            for image in node_output.get("images", []) or []:
                content = await self.download_image(
                    filename=image["filename"],
                    subfolder=image.get("subfolder", ""),
                    folder_type=image.get("type", "output"),
                )
                safe_name = str(image["filename"]).replace("/", "_").replace(
                    "\\", "_",
                )
                target = output_dir / safe_name
                target.write_bytes(content)
                saved.append(target)
        return saved
