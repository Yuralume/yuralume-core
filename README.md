<div align="center">
  <img src="frontend/public/logo.png" alt="Yuralume" width="180" />

  # Yuralume

  **A self-host AI companion platform where each character has their own life.**
  <br/>Daily schedule. Persistent memory. A social feed. The freedom to message you first.

  [![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-blue.svg)](LICENSE)
  [![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)
  [![Vue 3](https://img.shields.io/badge/Vue-3-42b883.svg)](https://vuejs.org/)
  [![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-336791.svg)](https://www.postgresql.org/)
  [![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#status)

  **English** · [繁體中文](README.zh_TW.md) · [日本語](README.ja.md)

  [yuralume.com](https://yuralume.com) · [Live demo](https://yuralume.com/#demo) · [Self-host guide](https://yuralume.com/selfhost/) · [Discord](https://discord.gg/BP2bpFDgR)
</div>

---

## Why Yuralume exists

Most AI chat products treat characters as a passive webpage: you ask, they answer; you close the tab, they cease to exist. Your data lives in someone else's cloud, and every new device starts from zero.

Yuralume inverts that. Characters here **have a day of their own** — a schedule planned by the LLM, memories that survive across sessions and channels, a social feed they post to on their own, and the discretion to message you first when something genuinely calls for it. Every semantic decision routes through the LLM; we forbid keyword shortcuts and `if/else` patches for individual cases.

Everything stays on your machine. Third-party APIs are called only when *you* enable them and supply a key.

## Who this is for

- **Long-term companion users** — you want one or two AI characters you can live with, you care about conversational privacy and behavioural realism, and you don't mind running a service on your own hardware.
- **Interactive-narrative and character-research developers** — you want LLM-driven NPC scaffolding (schedule / memory / proactive / cross-platform sync) without rebuilding it from scratch.
- **World-building content creators** — turn a static character sheet into something that visibly lives through a day.

**Not for you** if you want a turnkey SaaS, a B2B customer-service bot, or a deterministic task-runner agent — Yuralume's design red lines specifically rule those out.

## A glimpse of what feels different

Three scenarios that capture the texture:

1. **Morning busy, afternoon catch-up.** You message a character at 9am while they're "in a meeting"; they reply briefly and *defer*. After lunch, when their schedule clears, the scheduler triggers an unprompted follow-up that picks the conversation back up properly.
2. **Same person across devices.** You chat on the web in the morning, then say good-night to the same character on Telegram, Discord, or WhatsApp. They remember the morning's thread — not because of synced bots, but because there is one memory pool.
3. **They lived while you were away.** A week without opening the app: when you come back, their LumeGram feed has new posts and your inbox has two "thinking of you" messages — each one having survived a three-layer intention gate.

<div align="center">
<img src="assets/schedule_proactive_tg_demo.webp" alt="A character following her own daily schedule, then sending a proactive Telegram message" width="760" />
<br/><sub>Schedules, story events, and a proactive Telegram message — real product capture, played at 1.5× speed.</sub>
</div>

## Features

- **Streaming chat** with per-turn emotion / state, multimodal user input (drag images on web, send images on Telegram / LINE / Discord / WhatsApp), and optional TTS playback per character.
- **Guided character creation** with AI draft name candidates, optional 16-type personality references, explicit initial relationship setup, and relationship-aware chat / schedule / proactive behavior.
- **Scene-aware same-space chat** — the web Stage can feel co-present, but Scene Access checks the character scene against your recent dialogue and optional current status first; otherwise the UI steers you to phone-style messaging, a meeting opener, or a quick status note before retrying.
- **Structured long-term memory** — five-layer persona model (identity / life / emotional / interaction / trust), pgvector semantic retrieval, dream-time consolidation. Each character builds their *own* picture of you; interaction heat is per-character, while explicit initial relationship setup remains the relationship anchor.
- **Character-to-character social knowledge** — relationship setup can seed what one character already knows about another; encounters and chat extract peer facts into directional relationship memories, consolidate them into a compact peer roster, and feed that roster back into chat without exposing raw relationship scores.
- **Daily schedule with aftermath** — characters get an LLM-planned day; finished activities show up in today's prompt timeline, leave emotional residue, and become episodic memories. Shared activities only become shared memories after the user has agreed.
- **Three-gate proactive messaging** — cheap heuristic gate → LLM intention judge → LLM decider, with per-channel opt-in and a daily cap. They message you when it matters; they stay quiet when it doesn't.
- **Cross-channel one identity** — bind a character to Telegram, LINE, Discord, or the bundled WhatsApp gateway; the same memory / state / schedule runs on every surface.
- **LumeGram social feed** — each character has their own IG-style timeline; the scheduler decides when to post from six signal sources (schedule / story beat / memory / world event / silence / state shift), while respecting high-busy schedule slots such as sleep. Comments are answered on a tick, not in real time.
- **Real-world fact injection** — holidays (via `holidays`), weather (via Open-Meteo, no API key), and curated RSS news flow into the prompt as *facts only*; user profiles can store an editable coarse location that can be seeded from the user's login IP, and the LLM decides how to react, no hard-coded behaviours.
- **Story tooling** — character story arcs, multi-character fusion short stories, and a branching-drama (VN-style) generator. Realized arc beats stay visible as history, and completed arcs leave milestone memories.
- **Feature-group, per-feature, and per-character LLM routing** — Admin routing writes site-wide defaults, configure common model tiers by group, pin individual exceptions such as chat, memory extraction, or image recognition for text-only chat / character-draft models, then override per character when needed. BYOK provider keys are configured from Admin UI and encrypted at rest.
- **Generation usage ledger** — Admin Observability tracks chat, background/auxiliary LLM, image, video, and TTS generation usage without storing prompts or generated content. Operators can compare cost by feature/provider/model, **by character** (per-character cost/usage rollup), cache hits, estimated versus actual quantities, and Cloud Gateway request ids when hosted routing is used. A built-in custom-price calculator lets operators type each model's API price (per 1M tokens) and recompute cost live from the aggregated token counts — a browser-side estimate that never changes actual billing and needs no edits to the price JSON file.
- **Self-host NSFW mode foundation** — a manual, per-user temporary mode can route all LLM and image calls to Admin-configured community targets with idle TTL expiry. Written turns are marked with `content_mode` and carry optional `safe_summary`; frontier prompt boundaries replace marked raw history with the summary or drop it fail-closed. Long-term memories are born-safe tagged/summarised at the relationship level, pending follow-ups preserve safe summaries, Rule B routes unreplaceable marked queued text back to the configured community target, and TTS/eval backend guards avoid marked-content leakage. Admin configures the NSFW LLM/image target in Models; the player UI exposes a sidebar switch and active-mode atmosphere instead of chat-header controls. Core observability reports mode usage and sampled NSFW turn ratios. Hosted cloud mode keeps the API locked.
- **Character image stage** — a fade-rotating slideshow of each character's generated images backs the main view; responsive UI (desktop landscape / mobile portrait), iOS / Android safe-area aware. (Live2D was tried and dropped — too heavy, not generatable on the fly, low variety per character, high authoring cost.)

## See it in action

<div align="center">
<img src="assets/create_char_demo.webp" alt="Creating a new character from scratch in the web UI" width="760" />
<br/><sub>Character creation flow — from a blank form to a living character, played at 6× speed.</sub>
<br/><br/>
<img src="assets/gram_memories_demo.webp" alt="Browsing a character's LumeGram feed and memoirs" width="760" />
<br/><sub>LumeGram — each character's own IG-style feed, plus the memoirs they keep about you.</sub>
</div>

More clips (full resolution, with audio) on [yuralume.com](https://yuralume.com/#demo).

## Quick start

### One-line install (prebuilt images)

The fastest path — pulls the published Docker images and brings the whole stack up:

**macOS / Linux**

```bash
curl -fsSL https://yuralume.com/install.sh | bash
```

**Windows (PowerShell)**

```powershell
irm https://yuralume.com/install.ps1 | iex
```

Requires [Docker](https://www.docker.com/products/docker-desktop/); the app comes up at `http://127.0.0.1:8012`. The published images are built from this repository by [CI](.github/workflows/publish-images.yml) — what you build from source is what we ship. Prefer to read before you run, or want the manual step-by-step? See the [self-host guide](https://yuralume.com/selfhost/).

<div align="center">
<img src="assets/self-host_install_demo_timelapse.webp" alt="Timelapse of the one-line install, from one command to a character you can talk to" width="760" />
<br/><sub>The whole install, one command to a character you can talk to — 3 minutes compressed to ~24 s.
<a href="https://yuralume.com/self-host_install_demo.mp4">Watch it in real time</a> (no speed-ups, no edits).</sub>
</div>

To develop or build from source instead:

### Prerequisites

- Python 3.13 + [uv](https://docs.astral.sh/uv/)
- Node.js 22+ and npm
- Docker Desktop (PostgreSQL + integration tests)
- Git for Windows (provides `make`) on Windows

### Local development

```bash
uv sync                          # backend deps
cd frontend && npm install && cd ..

make dev                         # starts PostgreSQL, runs migrations, then backend (:8002) + frontend (:5174)
```

Press `Ctrl+C` to stop both. On Windows you can also double-click `run-dev.cmd`.
`make` pins `COMPOSE_PROJECT_NAME=kokoro-link` by default so existing local PostgreSQL containers and the `kokoro_postgres_data` volume keep working after the repo directory was renamed to `Yuralume-Core`.

After the app is up, open `http://127.0.0.1:5174`, go to **Admin → Provider Keys** and configure at least an LLM provider. The bundled `fake` provider keeps the app runnable for smoke tests but won't produce real conversation.

### Self-host with Docker Compose

**With source checkout** (developers):

```powershell
Copy-Item .env.container.example .env.container
docker compose -f docker-compose.container.yml --profile storage-local up -d --build
```

`--build` compiles the four images locally; drop the flag to pull the published images from ghcr.io instead.

**Without cloning** (end users) — only two files are needed:

```bash
mkdir yuralume && cd yuralume
curl -O https://raw.githubusercontent.com/Yuralume/yuralume-core/main/docker-compose.container.yml
curl -O https://raw.githubusercontent.com/Yuralume/yuralume-core/main/.env.container.example
cp .env.container.example .env.container
# (edit .env.container — at minimum set APP_BASE_URL, CONFIG_ENCRYPTION_KEY and STORAGE_KEY)
docker compose -f docker-compose.container.yml --profile storage-local pull
docker compose -f docker-compose.container.yml --profile storage-local up -d
```

Both flows bring up PostgreSQL on `5554`, the local object-storage service on `9012`, the bundled WhatsApp sidecar on `32190`, and the full app on `http://127.0.0.1:8012`. New media rows store app-relative refs such as `/v1/public/...`; browsers resolve them through the app origin, Telegram sends generated images by reading object storage directly and uploading multipart, and URL-based external platforms use the Admin **Channel settings → Public Base URL** with `APP_BASE_URL` as fallback. A VPS/reverse-proxy deploy normally only needs the app domain public; `STORAGE_PUBLIC_URL` is only for compatibility when an external storage/CDN service returns URLs that the app must reverse-map. Browser Web Push is optional: configure VAPID keys when you want OS-level notifications for proactive messages and LumeGram replies, or leave them unset to keep the existing SSE/red-dot path only. WhatsApp accounts use the sidecar automatically: players create the WhatsApp channel and scan the QR shown in the account section, without entering a sidecar URL, session id, API token, Meta Business credentials, or public webhook URL. The `migrate` service runs `alembic upgrade head` before the app starts. Pin a specific build with `YURALUME_IMAGE_TAG=v0.1.0` in `.env.container`; default is `latest`. The running app exposes its package version plus image tag / commit / build time at `GET /api/v1/system/version`, includes the same payload in `GET /api/v1/auth/config`, and shows a compact `Core v...` label in the Player sidebar and Admin topbar. Images are published to `ghcr.io/yuralume/yuralume-core/{app,storage-local,postgres,whatsapp-sidecar}` for both `linux/amd64` and `linux/arm64`; pushes to `master` publish `latest` plus a `sha-*` tag, and manual workflow runs can publish a chosen tag.

Want to generate images with your own local ComfyUI? Core doesn't talk to ComfyUI directly — it speaks a small normalized HTTP contract to a **Custom Media Gateway** you run yourself. See [`docs/CUSTOM_MEDIA_GATEWAY_SPEC.md`](docs/CUSTOM_MEDIA_GATEWAY_SPEC.md) for the full spec plus a minimal starter FastAPI reference server, or open it in-app from **Admin → Developer docs**. DIY self-hosters can implement the gateway themselves against the published contract; if you'd rather not tune per-model workflows yourself, a hosted media line is on the roadmap.

Want your own voice engine (GPT-SoVITS, XTTS, …)? Core speaks the same kind of small HTTP contract to a **Custom TTS Server** you run yourself — `GET /voices` + `POST /tts/synthesize`. See [`docs/CUSTOM_TTS_SERVER_SPEC.md`](docs/CUSTOM_TTS_SERVER_SPEC.md) for the full spec plus a minimal starter that wraps GPT-SoVITS, or open it in-app from **Admin → Developer docs**. Prefer BYOK without running anything? Configure the built-in OpenAI TTS provider instead.

For self-host prompt overlays, place files under `./prompts/tuned` using the
same relative paths as `src/kokoro_link/data/prompts/`, then set
`YURALUME_PROMPT_PACK_DIR=/app/prompts/tuned` in `.env.container` and restart
the `app` service. The compose file mounts that host directory read-only into
the app container. On startup, app logs include `Prompt pack overlay loaded`
with the overlay template count, or a warning when the configured directory is
empty or missing.

If you put the app behind a reverse proxy, raise the proxy response timeout above the longest provider call you allow in **Admin → Provider Keys**. Hosted image/video/TTS providers can take several minutes; nginx's common 60s defaults will surface as browser-facing `504 Gateway Timeout` even if the app is still waiting for the provider. For nginx, set values such as `proxy_read_timeout 300s; proxy_send_timeout 300s;` (or higher if your image/video timeout is higher) on the app location/server block.

Once the stack is healthy, verify the deployment with the smoke script:

```bash
uv run python scripts/self_host_smoke.py            # baseline (no provider keys)
uv run python scripts/self_host_smoke.py --openai-key sk-...   # BYOK round-trip too
```

app + storage `/health`, SPA fallback, `/auth/config`, `/system/version`, admin provider catalogue, runtime provider list, and (with `--openai-key`) creates a labelled LLM + embedding provider, verifies hot-swap into `/system/providers`, and deletes them on exit. Exit code 0 = pass, 1 = any step failed.

To size a local OpenAI-compatible LLM route before assigning many characters to it, use the capacity probe. The raw mode measures the vLLM endpoint directly; the Core mode sends real chat turns through existing characters and therefore writes chat/turn records:

```bash
uv run python scripts/llm_capacity_probe.py raw-vllm --endpoint http://127.0.0.1:8001/v1 --disable-reasoning --concurrency 1,2,4,8 --requests-per-step 16
uv run python scripts/llm_capacity_probe.py core-chat --core-url http://127.0.0.1:8002 --email admin@example.com --password ... --characters 8 --concurrency 1,2,4,8 --requests-per-step 16
```

## Configuration

`.env.example` is the source of truth. The variables you'll touch most often:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection (asyncpg). Defaults to the docker-compose dev db. |
| `CONFIG_ENCRYPTION_KEY` | **Required** before saving provider keys. Long random string. |
| `AUTH_ENABLED` | `false` for single-user mode (default). Set `true` + `JWT_SECRET` for multi-user. |
| `USER_PRIMARY_LANGUAGE` | Initial interface + content language for the single local user (`zh-TW` default, `en-US`, `ja-JP`). In single-user mode it seeds the default operator at first boot so the UI and character replies come up in this language; the self-host installer sets it from your install-time choice. In multi-user mode each user picks their own at `/auth/setup`. |
| `YURALUME_CLOUD_ENABLED` / `YURALUME_CLOUD_*` | Hosted cloud mode. Local setup/user/provider management is locked, auth federates to Cloud User, and LLM/image/video/TTS route through Cloud Gateway. A persisted tenant subscription guard blocks character creation, chat/assist/media/TTS/proactive and provider identity calls after lapse, including old JWT and account-scoped draft paths. |
| YURALUME_CLOUD_USER_INTERNAL_CREDENTIAL | Versioned Core-to-User credential (key|caller|audience|scopes|secret) for hosted-play, demo release and runtime-config calls; required when Cloud mode is enabled. |
| KOKORO_CLOUD_INTERNAL_CREDENTIALS | Versioned Cloud User-to-Core credential rotation set for freeze/tier routes (key|caller|audience|scopes|secret), fail-closed when empty. |
| KOKORO_CLOUD_INTERNAL_TOKENS | R1a legacy bearer allow-list for Cloud-to-Core freeze/tier migration. Remove after legacy-hit is zero. |
| Hosted demo account runtime profile | Cloud login now persists `cloud_tenant_tier` on the local operator projection. Self-host/local users always resolve to the default runtime profile. Hosted demo users resolve to the demo profile, which currently enforces one active character per account, one character creation per rolling 24 hours, one chat image per rolling 24 hours, one automatic LumeGram feed post per rolling 24 hours through the account runtime event ledger, disabled character AI image candidate/portrait generation, disabled feed video generation, disabled TTS synthesis, throttled background proactive tick evaluation, and a scheduler-driven character TTL reaper. The reaper deletes expired demo characters through `CharacterService.delete_character()` and calls the Cloud User demo-session release hook when the account has no remaining characters. |
| `VITE_YURALUME_DEMO_DISCORD_CLIENT_ID` / `VITE_YURALUME_DEMO_GOOGLE_CLIENT_ID` | Hosted demo OAuth client IDs for the SPA start routes. These are Vite build-time values; export them in the shell, pass `--env-file .env.container`, or set GitHub repository variables before image publishing. Otherwise `/demo/oauth/{provider}/start` bakes empty IDs and shows `Demo unavailable` before redirecting to Discord/Google. |
| `VITE_YURALUME_DEMO_*_URL` | Optional hosted demo frontend conversion links. `VITE_YURALUME_DEMO_TIER0_URL`, `VITE_YURALUME_DEMO_WAITLIST_URL`, `VITE_YURALUME_DEMO_DISCORD_URL`, and `VITE_YURALUME_DEMO_SELF_HOST_URL` control CTAs shown when demo OAuth is full, rate-limited, or unavailable. |
| `WEB_PUSH_VAPID_PUBLIC_KEY` / `WEB_PUSH_VAPID_PRIVATE_KEY` / `WEB_PUSH_VAPID_SUBJECT` | Optional browser Web Push credentials. When configured, Player Settings can subscribe this browser for OS notifications; when omitted, push APIs return `configured=false` and runtime delivery fails soft. Legacy `KOKORO_WEB_PUSH_*` aliases are still read. |
| `STORAGE_URL` / `STORAGE_KEY` | Object Storage endpoint. Self-host uses the bundled `storage-local`. |
| `APP_BASE_URL` / `STORAGE_PUBLIC_URL` | Browser-facing origin fallback. DB media refs default to app-relative `/v1/public/...`; Telegram generated-image sends read object storage directly and do not need a public image URL, while URL-based external platforms use Admin **Channel settings → Public Base URL** first, then `APP_BASE_URL`. `STORAGE_PUBLIC_URL` is only for reverse-mapping compatible external storage/CDN URLs. |
| `USER_TIMEZONE` / `KOKORO_USER_TIMEZONE` | Interface timezone (IANA, e.g. `Asia/Taipei`) for civil dates and visible clock times. Defaults to `UTC`; DB and server instants stay UTC. In single-user mode it seeds the default operator at first boot (same as `USER_PRIMARY_LANGUAGE`); the self-host installer sets it from your host timezone. |
| `CALENDAR_REGION`, `WEATHER_LATITUDE/LONGITUDE` | Fallback real-world fact injection when a user has no stored location. |
| `GEOIP_ENABLED` / `KOKORO_GEOIP_*` | Optional IP geolocation seed for a user's editable location profile during login when no location is stored yet. Private/loopback IPs are ignored. |
| `YURALUME_PROMPT_PACK_DIR` | Optional prompt pack overlay directory. Matching `.txt` files override `src/kokoro_link/data/prompts/`; each `TurnRecord` stores the resulting `prompt_pack_hash` for eval attribution. |
| `KOKORO_USAGE_PRICE_CATALOG_PATH` | Optional local JSON price catalog for the generation usage ledger. The bundled `usage-prices.openai.json` covers OpenAI Standard LLM token pricing and GPT Image token-detail pricing when the provider returns image usage tokens; when omitted, usage is still recorded with zero/unknown estimated cost. |
| `PERSONA_CURIOSITY_ENABLED` / `PERSONA_CURIOSITY_PROACTIVE_ENABLED` | Optional rollout flags for conversational persona discovery. They let the LLM curiosity planner contribute low-pressure discovery hints to chat / proactive prompts without adding player-facing profile forms or bypassing the persona extraction pipeline. |
| `KOKORO_PROMPT_MATERIAL_DIGEST_ENABLED` / `KOKORO_NOVELTY_GATE_ENABLED` / `KOKORO_REGISTER_PROFILE_ENABLED` / `KOKORO_NOVELTY_GATE_MAX_RETRIES` | Prompt-quality flags. Defaults enable material digest, register profiling, and the reply quality gate: chat compresses recent poetic material into factual bullets, profiles the turn's register, and only buffers high-risk replies for LLM quality review before sending. Low-risk streaming remains incremental. |
| `NSFW_MODE_TTL_SECONDS` / `KOKORO_NSFW_MODE_TTL_SECONDS` | Optional self-host NSFW mode idle TTL, default `1800` seconds. The mode is manual per user, uses an Admin-configured community LLM/image target, and is locked in hosted cloud mode. |
| `TAVILY_API_KEY` | Optional; enables the `web_search` tool. |

To enable browser Web Push, generate a VAPID key pair on the Core host:

```powershell
.venv\Scripts\python.exe -m py_vapid --gen --json
```

Copy the generated `publicKey` to `WEB_PUSH_VAPID_PUBLIC_KEY`, copy `privateKey` to `WEB_PUSH_VAPID_PRIVATE_KEY`, and set `WEB_PUSH_VAPID_SUBJECT` to a contact URI such as `mailto:admin@example.com`. Keep the private key out of Git and frontend code. Production browser push/service workers should be served from the real HTTPS `APP_BASE_URL`.

When the app is focused, Yuralume keeps updates in-app and suppresses OS notifications. When a Stage tab is open but hidden, the SSE path still shows a local Notifications API fallback even if the browser also has a Web Push subscription; the service worker and local fallback use a stable notification tag so the same event can be merged by the browser instead of silently disappearing.

**LLM / embedding / image / video / TTS provider keys do not live in `.env`.** They are configured at runtime from **Admin → Provider Keys**, encrypted in the database, and never returned in plain text from the API.

Turn records are also the eval feedback seam: admin-visible assistant chat bubbles and Admin → Observability can attach `operator_feedback` (`out_of_character` / `felt_human`) to a `TurnRecord`, and `/admin/observability/turns?feedback_kind=...` can query those tagged turns for downstream fixture mining.

Time-sensitive runtime paths use an injectable `ClockPort` where virtual-time tests need determinism: prompt time rendering, proactive tick/dispatch, quiet hours, persona dream, self-reflection, disposition drift, and post-turn prompt dates. New domain/application files are guarded by `scripts/check_clock_guard.py` so fresh `datetime.now()` usage goes through the clock seam unless explicitly allowlisted.

User timezone is fixed when the account is created. If an upgraded install backfilled an existing user to the wrong timezone, run a dry run first, then apply the one-time repair:

```bash
uv run python -m kokoro_link.cli.repair_user_timezone --email alice@example.com --timezone Asia/Taipei
uv run python -m kokoro_link.cli.repair_user_timezone --email alice@example.com --timezone Asia/Taipei --apply
```

Legacy `KOKORO_*` environment variables are still read as fallback, but new deployments should use the names in `.env.example`.

## Architecture

```text
src/kokoro_link/
  api/            # FastAPI routes
  application/    # Use cases, DTOs, services
  bootstrap/      # Container, settings, startup wiring
  contracts/      # Port interfaces (provider-agnostic)
  domain/         # Entities, value objects
  infrastructure/ # Repositories, LLM adapters, persistence
frontend/         # Vue 3 + Vite + Ant Design Vue
alembic/          # Database migrations
docker/           # Local infra Dockerfiles
tests/{unit,integration}/
```

The system follows **port–adapter** strictly: LLM providers, embedders, image generators, video generators, TTS services, and messaging channels all live behind ports in `contracts/`. Swapping a provider is a registry change, not a refactor.

## Channels

Each character can be bound to a Telegram, LINE, Discord, or WhatsApp chat; messages there flow through the same `ChatService` as the web UI, so memory / emotional state / schedule remain consistent across surfaces.

- Players configure their own channel accounts from character **Settings → External message channels**. Admin configures the site-wide Telegram delivery mode and webhook Public Base URL from **Admin → Channel settings**. Discord and WhatsApp are fixed to Gateway-style receivers and do not expose a mode selector.
- One bot account per `(platform, character)`; multiple chat bindings per account.
- Telegram can receive inbound messages through a site-wide polling or webhook mode: polling fits local/private deployments, webhook fits public deployments. Telegram generated images are uploaded as multipart from Core object storage, so outbound photos do not need a public image URL; webhook mode still needs the public webhook base URL. LINE remains webhook-only and URL-based for image pushes, using the Admin public base URL with `APP_BASE_URL` as fallback. Discord receives through Gateway WebSocket. WhatsApp receives through a WhatsApp Web / Baileys-compatible sidecar, so it does not require Meta Business API approval or a public webhook URL.
- `allowed_sender_refs` allowlist prevents strangers from being recorded as "the user".

## Roadmap and status

Yuralume is in **alpha**. The core companion loop — chat / memory / schedule / proactive / LumeGram / cross-channel / real-world fact injection / multimodal — is in place; the road ahead focuses on humanisation depth (self-reflection, autobiographical narrative, vulnerability protection) over breadth. Plain-language release notes live in [`docs/changelog/`](docs/changelog/README.md) (EN / 中文 / 日本語).

## FAQ

**Is it open source?**
Source-available under [BUSL-1.1](LICENSE), not OSI open source. Free for non-commercial self-host, research, and evaluation. Commercial production use requires a separate licence. Each version converts to Apache 2.0 four years after first publication.

**Can I run it without a GPU?**
Yes. LLM is via an external provider (cloud or local LM Studio). The character image stage is just a CSS-driven slideshow over already-generated images. Image / video / TTS providers are optional and pluggable.

**Can multiple users share one instance?**
Yes — set `AUTH_ENABLED=true` and `JWT_SECRET`. The default is single-user mode. Per-character isolation is enforced at the `(character_id, operator_id)` level, so multi-user installs don't leak persona data between users.

**Can I use this as a customer-service bot?**
No. The design red line forbids keyword shortcuts and deterministic rule branches, which is the opposite of what task-driven agents need.

**Why does the Python package still say `kokoro_link`?**
The public name changed in May 2026 (Yuralume was previously named Kokoro-Link), but a deep import / DB / `.env` rename was judged too risky for too little gain, so only the outward-facing name changed.

## Contributing

This is a personal project, but contributions and forks are welcome within the licence terms. The engineering red lines to respect: **LLM-first** (semantic decisions route through the LLM — no keyword shortcuts or `if/else` patches for individual cases), **port–adapter** (providers stay behind `contracts/` ports), and **per-character isolation** (nothing leaks between characters or users). Bug reports go to [GitHub Issues](../../issues).

## Community

Built by one developer, from Taiwan.

[Discord](https://discord.gg/BP2bpFDgR) · [X / Twitter](https://x.com/Yuralume) · [Ko-fi](https://ko-fi.com/yuralume) · [yuralume.com](https://yuralume.com)

## Acknowledgements

Built on the shoulders of: **FastAPI**, **SQLAlchemy 2**, **Alembic**, **asyncpg**, **pgvector**, **Pydantic**, **uv**, **Vue 3**, **Vite**, **Ant Design Vue**, **Open-Meteo**, **holidays**, and the Anthropic / OpenAI / Mistral / LM Studio communities.

## License

[Business Source License 1.1](LICENSE). Each version converts to Apache 2.0 four years after its first public distribution.

---

<div align="center">
  <sub>Yuralume was previously named Kokoro Link · LumeGram was previously named Kokoro-Gram</sub>
</div>
