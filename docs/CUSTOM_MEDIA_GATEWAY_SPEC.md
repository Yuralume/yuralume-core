# Custom Media Gateway Specification

> Spec version: 1.0 · Status: Draft · Applies to: Yuralume Core self-host

## Overview

Yuralume Core does not talk to image/video backends (ComfyUI, Automatic1111, native provider SDKs) directly. Instead it speaks a small, normalized HTTP contract to a **Custom Media Gateway** — an HTTP service you run yourself. Any backend whose quirks you hide behind this contract becomes usable. Model-format variability (different ComfyUI workflows, checkpoints, samplers) is absorbed **inside your gateway** (e.g. one workflow JSON per model, plus any prompt rewriting your models need), never inside Core.

Core does **not** ship a tuned reference gateway. What it ships is: (1) this contract, and (2) a **minimal starter** below that you can copy and extend. Per-model workflow tuning and prompt rewriting are deliberately left to you — they are model-specific and cannot be generalized. (If you'd rather not build and tune this yourself, a hosted media line is on the roadmap.)

## How Core calls the gateway

Core reads a `base_url`, `api_key`, and `default_model` from **Admin → Provider Keys → Custom Media Gateway** and issues:

- Image: `POST {base_url}/images/generations`
- Video: `POST {base_url}/videos/generations`

`base_url` is used verbatim: Core appends `/images/generations` to it. **If your gateway serves routes under a `/v1` prefix, `base_url` must include it** (e.g. `http://host:9894/v1`).

Request headers (both endpoints):

```
Authorization: Bearer <api_key>
X-Request-Id: img-<hex>   (or vid-<hex> for video)
Content-Type: application/json
```

`api_key` is passthrough; a gateway on a trusted LAN may ignore it, but the header is always sent.

## Image request body

```json
{
  "model": "yuralume-anime",
  "prompt": "Character: Mira\nAppearance: ...\nCurrent emotion: ...\nScene: ...\nRecent dialogue context: ...",
  "size": "1024x1536",
  "n": 1
}
```

- `model` — passthrough of the configured `image_model` (or `default_model`).
- `prompt` — a multi-line English string. Core flattens character identity, appearance, current emotion/intent, scene, and recent dialogue into this one field. Treat it as free text.
- `size` — one of `1024x1536` (portrait), `1536x1024` (landscape), `1024x1024` (square). Your gateway maps these to native dimensions.
- `n` — integer 1–4.

## Image response body

```json
{
  "data": [
    { "url": "/v1/artifacts/abc123" }
  ]
}
```

Each item in `data[]` must carry **either**:

- `b64_json` — base64-encoded image bytes (decoded directly), **or**
- `url` — absolute (`http(s)://…`) or relative. Relative URLs are resolved against `base_url` and fetched with a plain **GET carrying no Authorization header** (see gotchas). PNG/JPEG bytes expected.

Return at least one item, or Core raises `ImageNoOutputError`.

## Video request body

```json
{
  "model": "yuralume-motion",
  "prompt": "Character: ...\nScene: ...",
  "aspect_ratio": "9:16",
  "duration_seconds": 5
}
```

- `aspect_ratio` — one of `9:16`, `16:9`, `1:1`.
- `duration_seconds` — integer (Core derives it from requested frame count).

## Video response body

Same shape as image; `data` may also be named `artifacts`, and byte fields may be `b64_json`, `b64`, or `url`. Core returns the first usable item.

## Error model

Any non-2xx response is treated as failure: Core logs the response body and raises `ImageGenerationError` / `ImageTimeoutError` / `ImageNoOutputError` (video: the `Video*` equivalents). There is no structured error contract Core parses — a non-2xx status is sufficient. Timeouts are governed by the `timeout_seconds` field configured in Admin (default 180s image, 1800s video).

## Two operational gotchas

1. **`base_url` must include your route prefix.** Core appends `/images/generations` literally. If your gateway serves `/v1/images/generations`, its Admin `base_url` must be `http://<host>:<port>/v1`. A missing `/v1` yields 404 → generation error.
2. **Artifact downloads are unauthenticated.** When Core follows a returned `url`, it sends **no** `Authorization` header. Therefore artifact URLs must be publicly fetchable **or** carry a capability token in the URL itself. A gateway that leaves `/artifacts/{id}` unauthenticated is acceptable on a trusted LAN, unsafe to expose publicly without added protection.

## Registering in Admin → Provider Keys

1. Open **Admin → Provider Keys**, add a **Custom Media Gateway** connection.
2. `base_url` — your gateway root **including any `/v1` prefix** (e.g. `http://127.0.0.1:9894/v1`).
3. `default_model` — the model id your gateway accepts (e.g. `yuralume-anime`).
4. `api_key` — any string; your gateway decides whether to enforce it.
5. `timeout_seconds` — optional; default 180 (image) / 1800 (video).
6. Enable the connection. Core syncs it into the active image/video profile automatically.

## Minimal reference server (starter, not tuned)

> **This is a starter, not a tuned gateway.** It implements just the image endpoint against **one hardcoded ComfyUI workflow** and returns a URL Core can fetch. **Model-specific workflow tuning and prompt rewriting are up to you** — different checkpoints (SDXL tag lists, z-image bilingual prompts, ideogram-style JSON, …) need different workflows and different prompt handling, and that logic is intentionally not part of this skeleton. Add per-model workflow selection and a prompt rewriter yourself, or subscribe to the hosted media line.
>
> Assumes a local ComfyUI at `http://127.0.0.1:8188` and a `workflow.json` you exported from ComfyUI's "Save (API Format)". No auth, no `/v1` — so register it in Admin with `base_url = http://<host>:8000`.

```python
# gateway.py — minimal Custom Media Gateway starter. Not tuned.
# pip install fastapi uvicorn httpx
import json, uuid, asyncio
from pathlib import Path
import httpx
from fastapi import FastAPI
from fastapi.responses import Response
from pydantic import BaseModel

COMFY = "http://127.0.0.1:8188"
WORKFLOW = json.loads(Path("workflow.json").read_text())  # one hardcoded workflow
PROMPT_NODE = "6"   # the id of your CLIPTextEncode (positive) node in workflow.json

app = FastAPI()
_artifacts: dict[str, bytes] = {}  # in-memory; fine for a LAN starter


class ImageRequest(BaseModel):
    model: str
    prompt: str
    size: str = "1024x1024"
    n: int = 1


@app.post("/images/generations")
async def images_generations(req: ImageRequest):
    # NOTE (starter): prompt goes in verbatim. A real gateway rewrites the
    # prompt per model (SDXL tags / z-image bilingual / ideogram JSON / ...).
    wf = json.loads(json.dumps(WORKFLOW))            # deep copy
    wf[PROMPT_NODE]["inputs"]["text"] = req.prompt   # inject Core's prompt

    async with httpx.AsyncClient(timeout=180) as http:
        queued = await http.post(f"{COMFY}/prompt", json={"prompt": wf})
        prompt_id = queued.json()["prompt_id"]

        # poll history until this prompt finishes
        while True:
            hist = (await http.get(f"{COMFY}/history/{prompt_id}")).json()
            if prompt_id in hist:
                break
            await asyncio.sleep(1.0)

        # pull the first output image ComfyUI produced
        outputs = hist[prompt_id]["outputs"]
        img = next(o["images"][0] for o in outputs.values() if o.get("images"))
        raw = await http.get(
            f"{COMFY}/view",
            params={"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output")},
        )

    art_id = uuid.uuid4().hex
    _artifacts[art_id] = raw.content
    # relative url; Core resolves it against base_url and GETs it WITHOUT auth
    return {"data": [{"url": f"/artifacts/{art_id}"}]}


@app.get("/artifacts/{art_id}")
def artifact(art_id: str):
    # unauthenticated on purpose — trusted LAN only (see gotcha 2)
    return Response(content=_artifacts.get(art_id, b""), media_type="image/png")


# run: uvicorn gateway:app --host 0.0.0.0 --port 8000
```

That is the entire contract surface for images. To go further you extend *this* service — pick a workflow per `model`, rewrite the prompt for that model, add the `/videos/generations` endpoint the same way — none of which Core needs to know about.
