# Custom TTS Server Specification

> Spec version: 1.0 · Status: Draft · Applies to: Yuralume Core self-host

## Overview

Yuralume Core does not talk to speech backends (GPT-SoVITS, XTTS, native provider SDKs) directly. Instead it speaks a small, normalized HTTP contract to a **Custom TTS Server** — an HTTP service you run yourself. Any voice engine whose quirks you hide behind this contract becomes usable. Engine-specific detail (reference audio, prompt transcripts, model weights, per-voice tuning) is absorbed **inside your server** (e.g. one voice profile per `voice_id`), never inside Core. Core only ever stores a stable `voice_id` per character — it never scans reference audio, weights, or workflows.

Core does **not** ship a tuned reference server. What it ships is: (1) this contract, and (2) a **minimal starter** below that you can copy and extend. Per-voice tuning (which reference clip, which weights, which transcript) is deliberately left to you — it is voice-specific and cannot be generalized. (If you'd rather not run a voice engine yourself, direct OpenAI TTS BYOK is built in as the `openai` provider, and a hosted voice line is on the roadmap.)

## How Core calls the server

Core reads a `base_url`, optional `api_key`, and a default voice from **Admin → Provider Keys → Custom TTS Server** and issues two calls:

- Voice catalog: `GET {base_url}/voices`
- Synthesize: `POST {base_url}/tts/synthesize`

`base_url` is used verbatim: Core appends `/voices` and `/tts/synthesize` to it. **If your server serves routes under a `/v1` prefix, `base_url` must include it** (e.g. `http://host:8100/v1`).

Request headers (both endpoints):

```
Authorization: Bearer <api_key>   (only sent when an api_key is configured)
X-Request-Id: tts-<hex>
Content-Type: application/json     (synthesize only)
```

`api_key` is optional and passthrough — a server on a trusted LAN may run without one. When it is left blank in Admin, no `Authorization` header is sent; when set, it is always sent as a bearer token.

## Voice catalog request

`GET {base_url}/voices` takes no body. It powers the character voice picker in the app.

## Voice catalog response

```json
{
  "voices": [
    { "id": "mira", "label": "Mira (calm)", "prompt_lang": "ja", "is_complete": true }
  ]
}
```

Each item in `voices[]`:

- `id` — **required**, non-empty. The stable voice id Core stores against a character and later echoes back as `voice_id` on synth. Items with a blank/missing `id` are skipped.
- `label` — human-readable name shown in the picker. Defaults to `id` if omitted.
- `prompt_lang` — optional language tag of the voice's reference sample (e.g. `zh`, `ja`, `en`). May be empty.
- `is_complete` — optional boolean, defaults `true`. Set `false` to list a voice that is not yet fully set up (Core surfaces it as not-ready).

Any non-2xx response makes Core treat the catalog as unavailable and the app shows **no** selectable voices — so keep `/voices` cheap and always 2xx once the server is up.

## Synthesize request body

```json
{
  "text": "今日はいい天気ですね。",
  "voice_id": "mira",
  "feature_key": "chat",
  "options": {
    "text_lang": "ja",
    "prompt_lang": "ja",
    "speed_factor": 1.0
  }
}
```

- `text` — the line to speak. A single chat bubble's text (Core caps it at 4000 chars before calling).
- `voice_id` — the catalog `id` the character picked, or the Admin **Default voice** when a character has none. Core never calls with an empty `voice_id`; if neither is set it fails locally with 503 before reaching you.
- `feature_key` — the calling surface. Currently always `"chat"`; treat it as an opaque hint and ignore it unless you want per-surface routing.
- `options.text_lang` — language of `text` (default `"zh"`). Your server maps this to the engine's language mode.
- `options.prompt_lang` — language of the voice's reference prompt (default `"zh"`).
- `options.speed_factor` — playback speed multiplier (default `1.0`).

## Synthesize response

**The response body is raw audio bytes — not JSON.** This is the key difference from the Custom Media Gateway, which returns a JSON envelope with URLs. Core reads the whole body as the clip and uses the `Content-Type` header as the media type (defaulting to `audio/wav` when absent). Return WAV/MP3/OGG bytes with a matching `Content-Type`:

```
HTTP/1.1 200 OK
Content-Type: audio/wav

<binary audio bytes>
```

Core hashes and caches the returned clip on disk, so repeat plays and refreshes of the same bubble never call your server again.

## Error model

| Your response | Core raises | HTTP to the browser | UX |
|---|---|---|---|
| `404` | `TTSUnavailable` | `503` | Voice treated as unavailable; play button greys out |
| any other non-2xx | `TTSError` | `502` | "synth failed, retry might help" |
| connection refused / DNS fail | `TTSUnavailable` | `503` | TTS treated as not configured |
| timeout | `TTSError` | `502` | retryable |

There is no structured error body Core parses — the status code alone drives the mapping. Timeouts are governed by the `timeout_seconds` field configured in Admin (default 90s).

## Two operational gotchas

1. **`base_url` must include your route prefix.** Core appends `/voices` and `/tts/synthesize` literally. If your server serves `/v1/tts/synthesize`, its Admin `base_url` must be `http://<host>:<port>/v1`. A missing `/v1` yields 404 → Core treats the voice as unavailable (503).
2. **Voice ids are load-bearing and must stay stable.** Core persists the picked `voice_id` on the character. If your `/voices` renames or drops an id a character already uses, that character loses its voice until it is re-picked. Assign durable ids (not array indices or per-restart UUIDs).

## Registering in Admin → Provider Keys

1. Open **Admin → Provider Keys**, add a **Custom TTS Server** connection.
2. `base_url` — your server root **including any `/v1` prefix** (e.g. `http://127.0.0.1:8100`).
3. `voice_id` (**Default voice**) — the id used when a character hasn't picked one; optional but recommended so voice works before anyone opens the picker.
4. `api_key` — optional; your server decides whether to enforce it. Left blank = no `Authorization` header.
5. `timeout_seconds` — optional; default 90.
6. Enable the connection. Core syncs it into the active TTS backend automatically, and the character voice picker reads your `/voices`.

## Minimal reference server (starter, not tuned)

> **This is a starter, not a tuned server.** It exposes a fixed voice catalog and wraps **one hardcoded GPT-SoVITS backend** (`api_v2.py`, `GET /tts`), returning raw WAV bytes Core can play. **Per-voice tuning is up to you** — which reference clip, which transcript, which GPT/SoVITS weights, and any weight-switching between voices are engine-specific and intentionally not part of this skeleton. Grow the `VOICES` map (and add weight switching) yourself, or use built-in OpenAI TTS / the hosted voice line.
>
> Assumes a local GPT-SoVITS `api_v2.py` at `http://127.0.0.1:9880`. No auth, no `/v1` — so register it in Admin with `base_url = http://<host>:8100`.

```python
# tts_gateway.py — minimal Custom TTS Server starter. Not tuned.
# pip install fastapi uvicorn httpx
import httpx
from fastapi import FastAPI, Response
from pydantic import BaseModel

SOVITS = "http://127.0.0.1:9880"  # GPT-SoVITS api_v2.py

# The one place to hide backend quirks: map a stable voice_id the player
# picks to whatever THIS engine needs (reference audio + its transcript).
# A real server also switches GPT/SoVITS weights per voice here.
VOICES = {
    "mira": {
        "label": "Mira (calm)",
        "prompt_lang": "ja",
        "ref_audio_path": "/gpt_sovits/refs/mira.wav",
        "prompt_text": "こんにちは、今日はいい天気ですね。",
    },
}

app = FastAPI()


class SynthOptions(BaseModel):
    text_lang: str = "zh"
    prompt_lang: str = "zh"
    speed_factor: float = 1.0


class SynthRequest(BaseModel):
    text: str
    voice_id: str
    feature_key: str = "chat"
    options: SynthOptions = SynthOptions()


@app.get("/voices")
def voices():
    return {
        "voices": [
            {
                "id": vid,
                "label": v["label"],
                "prompt_lang": v["prompt_lang"],
                "is_complete": True,
            }
            for vid, v in VOICES.items()
        ]
    }


@app.post("/tts/synthesize")
async def synthesize(req: SynthRequest):
    voice = VOICES.get(req.voice_id)
    if voice is None:
        # 404 → Core treats this voice as unavailable (greys the play button)
        return Response(status_code=404)
    params = {
        "text": req.text,
        "text_lang": req.options.text_lang,
        "ref_audio_path": voice["ref_audio_path"],
        "prompt_text": voice["prompt_text"],
        "prompt_lang": voice["prompt_lang"] or req.options.prompt_lang,
        "speed_factor": req.options.speed_factor,
        "media_type": "wav",
        "streaming_mode": "false",
    }
    async with httpx.AsyncClient(timeout=90) as http:
        upstream = await http.get(f"{SOVITS}/tts", params=params)
    if upstream.status_code != 200:
        # any non-2xx → Core raises TTSError → 502 (retry might help)
        return Response(status_code=502)
    # raw audio bytes in the body — NOT JSON. Content-Type sets the media type.
    return Response(content=upstream.content, media_type="audio/wav")


# run: uvicorn tts_gateway:app --host 0.0.0.0 --port 8100
# register in Admin with base_url = http://<host>:8100
```

That is the entire contract surface for TTS. To go further you extend *this* service — add voices to the catalog, map each to its reference clip and weights, switch weights per voice, wrap a different engine — none of which Core needs to know about.
