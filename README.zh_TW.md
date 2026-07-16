<div align="center">
  <img src="frontend/public/logo.png" alt="Yuralume" width="180" />

  # Yuralume

  **自架 AI 角色陪伴平台 — 讓每個角色有自己的日子。**
  <br/>有行程、有跨平台共享的記憶、有自己的社群動態、會主動傳訊息給你。

  [![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-blue.svg)](LICENSE)
  [![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)
  [![Vue 3](https://img.shields.io/badge/Vue-3-42b883.svg)](https://vuejs.org/)
  [![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-336791.svg)](https://www.postgresql.org/)
  [![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#目前狀態與藍圖)

  [English](README.md) · **繁體中文** · [日本語](README.ja.md)

  [yuralume.com](https://yuralume.com) · [線上 Demo](https://yuralume.com/#demo) · [自架指南](https://yuralume.com/selfhost/zh.html) · [Discord](https://discord.gg/BP2bpFDgR)
</div>

---

## 為什麼有 Yuralume

主流 AI 對話產品把角色當「被動應答的網頁」：你問才答、你關掉就不存在、跨裝置從零開始，所有對話送進別人雲端。

Yuralume 反過來做：角色 **有自己的日子** — 每天有 LLM 規劃的行程、會記得你昨天說的話、會在自己的社群動態貼文、會在你不在的時候主動傳訊息。而所有行為決策由 LLM 對角色當下情境的語意理解推進，**禁止關鍵字硬規則、禁止 `if/else` 特判個案**。

資料預設留在你自己的機器，第三方雲端 API 僅在你顯式啟用且設了 key 時呼叫。

## 對誰

- **長期陪伴型重度使用者** — 想跟一兩個 AI 角色長期相處、在意對話隱私與真實感、不介意在自己機器跑服務。
- **互動敘事 / 角色研究的開發者** — 想要 LLM-driven NPC 的腳手架（行程 / 記憶 / 主動訊息 / 跨平台同步），但不想從零自己搭。
- **世界觀演示的內容創作者** — 想把靜態角色設定變成「能演示給觀眾看的活物」。

**不適合**：想要開箱即用 SaaS 的休閒使用者、想做 B2B 客服 / 工具型 agent 的場景、預期 deterministic 流程的任務型 bot。產品紅線明確排除這些用途。

## 三個必看典型場景

1. **早上忙不到回，下午自動補回** — 早上 9 點丟訊息給角色，他當下「正在開會」 → 短回「先回，會議結束再好好回你」並 defer。中午過後會議結束、行程空出來，scheduler 觸發主動補回，把早上的對話正式接回去。
2. **跨裝置接續同一個關係** — 早上在 web 聊到一半離開、晚上在 Telegram、Discord 或 WhatsApp 跟同一個角色說晚安，他依然記得早上的脈絡 — 不是幾個 bot 在同步，而是只有一份記憶池。
3. **角色不在線時自己過日子** — 整週沒打開 app，回來看角色的 LumeGram 有幾則貼文、收件匣有兩則「想到你」訊息（每則都通過了三層 intention 把關）。

<div align="center">
<img src="assets/schedule_proactive_tg_demo.webp" alt="角色照著自己的行程過日子，然後主動傳來一則 Telegram 訊息" width="760" />
<br/><sub>行程、劇情事件、然後一則主動傳來的 Telegram 訊息 — 實機錄製，1.5× 速度播放。</sub>
</div>

## 核心能力

- **串流對話** — 每輪附情緒狀態；web 可拖入圖，TG / LINE / Discord / WhatsApp 可直接傳圖；每角色可選 TTS 語音播放。
- **有邊界的同場互動** — web Stage 可以有同場感，但 Scene Access 會先綜合角色場景、近期對話與你設定的「現在狀態」判斷是否有合理相遇理由；不合理時引導成手機訊息、約見面起手或補述處境後重判，不讓使用者像瞬移一樣闖進角色私密場景。
- **結構化長期記憶** — 五層人際模型（identity / life / emotional / interaction / trust）、pgvector 語意檢索、夜間 dream 整合。每個角色對使用者建立 **自己** 的認知；互動熱度是 per-character 統計，明確填寫的起始關係才是關係主述。
- **角色之間的社交知識** — 建立角色關係時可 seed 某角色已知另一位角色的身分、常出沒地與互動背景；encounter / chat 會把新觀察寫成 directional relationship memory，背景整理成精簡社交名冊再注入聊天，不暴露 raw 好感 / 信任數字。
- **每日行程 + 情緒尾韻** — 每天 LLM 規劃當日行程，活動結束會進入今天已完成時間線、留下情緒尾韻並寫入 episodic memory；共同活動只有在使用者答應後才會變成共同回憶。
- **三層主動訊息把關** — cheap heuristic → LLM intention judge → LLM decider，加上每通道 opt-in 與每日上限。該說的時候說、不該說的時候閉嘴。
- **跨通道同一個人** — 把角色綁到 Telegram、LINE、Discord bot 或部署內建 WhatsApp gateway；每個介面共用同一份記憶 / 狀態 / 行程。
- **LumeGram 角色社群動態** — 每角色一條 IG 風時間流，scheduler 從六種訊號（行程 / 劇情 beat / 記憶 / 外界事件 / 沉默 / 狀態變化）自動發文，並尊重睡眠等高 busy 行程時段；留言回覆走 tick 不即時。
- **真實世界事實注入** — 節日（`holidays`）、天氣（Open-Meteo 免 key）、外界 RSS 新聞以「事實段」形式進入 prompt，**由 LLM 自行判斷要不要反應**，不寫死「下雨一定提到」。
- **故事工具** — 角色故事弧、多角色融合短篇小說、分歧劇場（VN 風選擇分支）；已演出的 arc beat 會保留成歷程，完成 arc 會留下里程碑記憶。
- **模型群組 × per-feature × per-character LLM 路由** — Admin 路由設定會寫入全站預設，先用群組配置常見模型層級，再針對聊天、記憶抽取、純文字聊天 / 創角草稿模型的圖片識別等例外單獨 pin；必要時可針對單一角色覆寫。BYOK provider key 從 Admin UI 設定、加密存放。
- **生成用量帳本** — Admin Observability 可追蹤聊天、背景 / 輔助 LLM、圖片、影片、TTS 的生成用量，不存 prompt 或生成內容；可依功能 / provider / model 比較成本、快取命中、estimated / actual 標記，以及 hosted routing 時的 Cloud Gateway request id。
- **self-host NSFW mode 基礎** — 使用者可手動開啟 per-user 暫時模式，期間所有 LLM / 圖片呼叫路由到 Admin 指定社群模型 / profile，閒置 TTL 自動過期；寫入 turn 會帶 `content_mode` 與 optional `safe_summary`，長期記憶以 born-safe 的關係 / 情緒層級保存。後端已補 frontier prompt summary 替換 / fail-closed、pending follow-up safe summary、不可替換標記 queued text 的 Rule B 社群路由、TTS 停用與 Core eval 排除骨架；Admin 在 Models 統一設定 NSFW LLM / image 目標，玩家頁保留側欄開關，啟用後以全頁氣氛效果取代聊天上方 badge，Core observability 可回報模式使用量與抽樣 NSFW turn ratio；cloud mode 目前鎖定。
- **角色圖像舞台（幻燈片）** — 主畫面以每個角色生成的圖像做淡入淡出輪播；桌面橫向 / 手機直向 RWD、iOS / Android 安全區域感知。（Live2D 試過後放棄 — 太吃資源、無法即時生成、單一角色變化性低、製作成本高。）

## 實際看看

<div align="center">
<img src="assets/create_char_demo.webp" alt="在網頁 UI 從零建立一個新角色" width="760" />
<br/><sub>角色建立流程 — 從空白表單到一個活起來的角色，6× 速度播放。</sub>
<br/><br/>
<img src="assets/gram_memories_demo.webp" alt="瀏覽角色的 LumeGram 動態與回憶錄" width="760" />
<br/><sub>LumeGram — 每個角色自己的 IG 風動態，以及他們寫下關於你的回憶錄。</sub>
</div>

更多完整解析度（含聲音）的片段見 [yuralume.com](https://yuralume.com/#demo)。

## 快速開始

### 一鍵安裝（使用預建 image）

最快的路徑 — 拉取已發布的 Docker image、直接把整套跑起來：

**macOS / Linux**

```bash
curl -fsSL https://yuralume.com/install.sh | bash
```

**Windows（PowerShell）**

```powershell
irm https://yuralume.com/install.ps1 | iex
```

需要 [Docker](https://www.docker.com/products/docker-desktop/)；app 會開在 `http://127.0.0.1:8012`。發布的 image 就是由本 repo 的 [CI](.github/workflows/publish-images.yml) 建出來的 — 你從原始碼 build 出來的，就是我們發布的。想先看腳本內容再執行、或想手動一步一步來？見[自架指南](https://yuralume.com/selfhost/zh.html)。

<div align="center">
<img src="assets/self-host_install_demo_timelapse.webp" alt="一鍵安裝縮時 — 從一行指令到可以聊天的角色" width="760" />
<br/><sub>整個安裝過程，從一行指令到可以聊天的角色 — 3 分鐘壓縮成約 24 秒。
<a href="https://yuralume.com/self-host_install_demo.mp4">看未加速原片</a>（無剪輯）。</sub>
</div>

想開發或從原始碼 build，往下看：

### 前置需求

- Python 3.13 + [uv](https://docs.astral.sh/uv/)
- Node.js 22+ 與 npm
- Docker Desktop（PostgreSQL + 整合測試）
- Windows 需 Git for Windows（提供 `make`）

### 本地開發

```bash
uv sync                          # 後端依賴
cd frontend && npm install && cd ..

make dev                         # 啟動 PostgreSQL、跑 migrations，再並行啟動後端（:8002）+ 前端（:5174）
```

按 `Ctrl+C` 停掉兩個。Windows 可雙擊 `run-dev.cmd`。
`make` 預設固定 `COMPOSE_PROJECT_NAME=kokoro-link`，讓 repo 目錄改名為 `Yuralume-Core` 後仍能沿用既有本機 PostgreSQL container 與 `kokoro_postgres_data` volume。

啟動後打開 `http://127.0.0.1:5174`，到 **Admin → Provider Keys** 至少設定一個 LLM provider。內建的 `fake` provider 讓 app 可以順利跑起來做 smoke test，但不會產生真實對話。

### 容器化自架（Docker Compose）

```powershell
Copy-Item .env.container.example .env.container
docker compose -f docker-compose.container.yml --profile storage-local up -d --build
```

PostgreSQL 開在 `5554`、本地 object storage 在 `9012`、內建 WhatsApp sidecar 在 `32190`、完整 app 在 `http://127.0.0.1:8012`。新媒體資料會存成 `/v1/public/...` 這類 app-relative ref；瀏覽器用目前開啟的 app origin 解析，Telegram 發送生成圖片時會直接讀 object storage bytes 再 multipart 上傳，不需要公網圖片 URL；仍以 URL 推圖的平台則優先使用 Admin **通道站台設定 → 公網 Base URL**，留空時才 fallback 到 `APP_BASE_URL`。Browser Web Push 是選填能力：要讓主動訊息與 LumeGram 回覆跳系統通知時設定 VAPID keys；未設定則維持既有 SSE / 紅點路徑。WhatsApp 通道會自動使用容器 sidecar；玩家只要建立 WhatsApp 通道，掃帳號區塊顯示的 QR，不需要填 sidecar URL、session id、API token、Meta Business 憑證或公網 webhook URL。`migrate` service 會在 `app` 啟動前跑完 `alembic upgrade head`。可在 `.env.container` 用 `YURALUME_IMAGE_TAG=v0.1.0` pin 住指定 build；執行中的 app 會在 `GET /api/v1/system/version` 回 package version、image tag、commit 與 build time，`GET /api/v1/auth/config` 也會帶同一份資訊，Player 側欄與 Admin topbar 會顯示簡短 `Core v...` 標籤。

想用自己本機的 ComfyUI 出圖？Core 不會直接跟 ComfyUI 對話——它對外只講一份精簡的正規化 HTTP 合約，稱作 **Custom Media Gateway**，由你自己實作並執行。完整規格與最小 starter FastAPI 參考 server 見 [`docs/CUSTOM_MEDIA_GATEWAY_SPEC.md`](docs/CUSTOM_MEDIA_GATEWAY_SPEC.md)（英文），也可以在後台 **管理後台 → 開發文件** 直接查閱。DIY 使用者可依此公開合約自行實作 gateway；若不想自己調校 per-model workflow，未來雲端媒體訂閱線會承接這個便利選項。

想接自己的語音引擎（GPT-SoVITS、XTTS…）？Core 對 TTS 同樣只講一份精簡 HTTP 合約，稱作 **Custom TTS Server**——`GET /voices` + `POST /tts/synthesize`。完整規格與 wrap GPT-SoVITS 的最小 starter 參考 server 見 [`docs/CUSTOM_TTS_SERVER_SPEC.md`](docs/CUSTOM_TTS_SERVER_SPEC.md)（英文），也可在 **管理後台 → 開發文件** 直接查閱。若不想自架，直接改用內建 OpenAI TTS provider 走 BYOK 即可。

自架 prompt overlay 可把檔案放在 `./prompts/tuned`，維持與 `src/kokoro_link/data/prompts/` 相同的相對路徑，並在 `.env.container` 設定 `YURALUME_PROMPT_PACK_DIR=/app/prompts/tuned` 後重啟 `app` service。compose 會把該 host 目錄唯讀掛進 app container。啟動時 app log 會出現 `Prompt pack overlay loaded` 與 overlay template 數量；若目錄是空的或不存在，會以 warning 明確提示。

要估算本地 OpenAI-compatible LLM 能承受多少角色，先用容量探測腳本分兩層測。`raw-vllm` 直接壓 vLLM OpenAI endpoint；`core-chat` 會透過既有角色送真實聊天，因此會寫入聊天與 TurnRecord：

```bash
uv run python scripts/llm_capacity_probe.py raw-vllm --endpoint http://127.0.0.1:8001/v1 --disable-reasoning --concurrency 1,2,4,8 --requests-per-step 16
uv run python scripts/llm_capacity_probe.py core-chat --core-url http://127.0.0.1:8002 --email admin@example.com --password ... --characters 8 --concurrency 1,2,4,8 --requests-per-step 16
```

## 設定

`.env.example` 是 SoT，常會動的變數：

| 變數 | 用途 |
|---|---|
| `DATABASE_URL` | PostgreSQL 連線（asyncpg）。預設指向 docker-compose dev db。 |
| `CONFIG_ENCRYPTION_KEY` | **必填**，才能在 Admin UI 存 provider key。用長隨機字串。 |
| `AUTH_ENABLED` | `false` 為單人模式（預設）。多使用者請設 `true` + `JWT_SECRET`。 |
| `USER_PRIMARY_LANGUAGE` | 本地單一使用者的初始介面 + 內容語言（預設 `zh-TW`，可選 `en-US`、`ja-JP`）。單人模式下會在首次開機 seed 預設 operator，讓介面與角色回覆語言一開即正確；自架安裝腳本會用你安裝當下選的語言寫入。多使用者模式則由每位使用者在 `/auth/setup` 各自挑選。 |
| `USER_TIMEZONE` / `KOKORO_USER_TIMEZONE` | 本地單一使用者的介面時區（IANA，如 `Asia/Taipei`），決定民用日期時間、行程與「今天」邊界；DB 與 server instant 仍為 UTC。預設 `UTC`，單人模式下同樣在首次開機 seed 預設 operator；自架安裝腳本會用主機時區自動帶入。 |
| `YURALUME_CLOUD_ENABLED` / `YURALUME_CLOUD_*` | Hosted cloud 模式。啟用後本地 setup / user / provider 管理 surface 會鎖定，`/auth/config` 回 `mode: "cloud"`，`/auth/login` 代理到 Yuralume Cloud User service，LLM / 圖片 / 影片 / TTS 走 Yuralume Cloud Gateway；核心仍需要 `JWT_SECRET` 簽自己的短效 session token。 |
| Hosted demo account runtime profile | Cloud login 會把 `cloud_tenant_tier` 存進本地 operator projection。自架 / local 使用者永遠解析到 default runtime profile；hosted demo 使用者解析到 demo profile，目前會限制每帳號同時 1 個角色、透過 account runtime event ledger 限制每滾動 24 小時建立 1 次角色、1 次對話生圖與 1 篇自動 LumeGram feed post，停用角色 AI 圖像候選 / portrait 生成、feed 影片生成與 TTS 合成，降低背景 proactive tick 評估頻率，並由 scheduler 驅動 character TTL reaper。reaper 會透過 `CharacterService.delete_character()` 清理過期 demo 角色，帳號無剩餘角色時呼叫 Cloud User demo-session release hook 釋放 slot。 |
| `VITE_YURALUME_DEMO_DISCORD_CLIENT_ID` / `VITE_YURALUME_DEMO_GOOGLE_CLIENT_ID` | Hosted demo OAuth 的 SPA client id。這些是 Vite build-time 值，必須在 build 前用 shell env、`docker compose --env-file .env.container`，或 GitHub repository variables 傳入；只在容器 runtime 補 env 不會改掉已打包的前端，`/demo/oauth/{provider}/start` 會因空 client id 顯示 `Demo unavailable`。 |
| `VITE_YURALUME_DEMO_*_URL` | 選填 hosted demo 前端導流連結。`VITE_YURALUME_DEMO_TIER0_URL`、`VITE_YURALUME_DEMO_WAITLIST_URL`、`VITE_YURALUME_DEMO_DISCORD_URL`、`VITE_YURALUME_DEMO_SELF_HOST_URL` 會控制 demo OAuth 滿位、限流或不可用時顯示的 CTA。 |
| `WEB_PUSH_VAPID_PUBLIC_KEY` / `WEB_PUSH_VAPID_PRIVATE_KEY` / `WEB_PUSH_VAPID_SUBJECT` | 選填 Browser Web Push 憑證。設定後玩家可在「設定 → 個人」訂閱此瀏覽器的系統通知；未設定時 push API 回 `configured=false`，執行期推播 fail-soft。舊 `KOKORO_WEB_PUSH_*` alias 仍會讀取。 |
| `STORAGE_URL` / `STORAGE_KEY` | Object Storage endpoint，自架可走附帶的 `storage-local`。 |
| `APP_BASE_URL` / `STORAGE_PUBLIC_URL` | 瀏覽器面向的 app origin fallback。DB 媒體 ref 預設是 `/v1/public/...`；Telegram 生成圖片發送直接讀 object storage，不需要公網圖片 URL；仍以 URL 推圖的平台優先使用 Admin **通道站台設定 → 公網 Base URL**，留空才用 `APP_BASE_URL`。`STORAGE_PUBLIC_URL` 只保留給相容外部 storage/CDN URL 反查。 |
| `CALENDAR_REGION`、`WEATHER_LATITUDE/LONGITUDE` | 選填，啟用真實世界事實注入。 |
| `YURALUME_PROMPT_PACK_DIR` | 選填，外部 prompt pack overlay 目錄；同路徑 `.txt` 會覆蓋 `src/kokoro_link/data/prompts/`，每筆 `TurnRecord` 會記錄對應 `prompt_pack_hash` 供 eval 歸因。 |
| `KOKORO_USAGE_PRICE_CATALOG_PATH` | 選填，generation usage ledger 使用的本機 JSON 價格目錄；內建 `usage-prices.openai.json` 覆蓋 OpenAI Standard LLM token 價格，以及 provider 回傳 image usage tokens 時的 GPT Image token-detail 價格。未設定時仍會記錄用量，但成本為 zero / unknown estimated。 |
| `PERSONA_CURIOSITY_ENABLED` / `PERSONA_CURIOSITY_PROACTIVE_ENABLED` | 選填，控制 Conversational Persona Discovery rollout；只讓 LLM curiosity planner 對 chat / proactive prompt 提供低壓探索提示，不新增玩家個資表單，也不繞過既有 persona extraction 管線。 |
| `NSFW_MODE_TTL_SECONDS` / `KOKORO_NSFW_MODE_TTL_SECONDS` | 選填，self-host NSFW mode 閒置 TTL，預設 `1800` 秒。模式由 user 手動開啟，使用 Admin 統一指定的社群 LLM / image 目標；hosted cloud mode 目前鎖定。 |
| `TAVILY_API_KEY` | 選填，啟用 `web_search` 工具。 |

要啟用瀏覽器 Web Push，先在 Core 主機產生一組 VAPID key：

```powershell
.venv\Scripts\python.exe -m py_vapid --gen --json
```

把輸出的 `publicKey` 填到 `WEB_PUSH_VAPID_PUBLIC_KEY`，`privateKey` 填到 `WEB_PUSH_VAPID_PRIVATE_KEY`，`WEB_PUSH_VAPID_SUBJECT` 填聯絡 URI，例如 `mailto:admin@example.com`。private key 不要提交到 Git，也不要放進前端程式。正式環境的 browser push / service worker 建議使用實際 HTTPS `APP_BASE_URL`。

**LLM / embedding / image / video / TTS 的 provider key 不放在 `.env`。** 從 **Admin → Provider Keys** 在 runtime 設定，加密存資料庫，GET API 不回明文。

TurnRecord 也作為 eval feedback 接縫：admin 可見的 assistant chat bubble 與 Admin → Observability 可把 `operator_feedback`（`out_of_character` / `felt_human`）掛到單筆 `TurnRecord`，並可用 `/admin/observability/turns?feedback_kind=...` 查回，供閉源側轉成 fixture 草稿。

時間敏感 runtime 路徑在需要 deterministic 測試時走可注入的 `ClockPort`：prompt 現在時間渲染、proactive tick/dispatch、quiet hours、persona dream、self-reflection、disposition drift 與 post-turn prompt 日期都可接虛擬時間。新增 domain/application 檔案由 `scripts/check_clock_guard.py` 擋直接 `datetime.now()`，除非明確列入存量豁免清單。

舊版 `KOKORO_*` 環境變數仍會被讀作 fallback，但新部署優先用 `.env.example` 的命名。

## 架構

```text
src/kokoro_link/
  api/            # FastAPI 路由
  application/    # Use case、DTO、service
  bootstrap/      # Container、設定、啟動 wiring
  contracts/      # Port 介面（與 provider 無關）
  domain/         # Entity、value object
  infrastructure/ # Repository、LLM adapter、persistence
frontend/         # Vue 3 + Vite + Ant Design Vue
alembic/          # 資料庫 migrations
docker/           # 本機基礎設施 Dockerfile
tests/{unit,integration}/
```

系統嚴格走 **port–adapter**：LLM provider、embedder、生圖、生影片、TTS、訊息通道全部在 `contracts/` 後面。換 provider 是 registry 設定，不是重構。

## 訊息通道

每個角色可以綁到 Telegram、LINE、Discord 或 WhatsApp 的某個 chat；那邊的訊息會走跟網頁 UI 一樣的 `ChatService`，所以記憶 / 情緒狀態 / 行程在所有介面一致。

- 玩家在角色 **設定 → 外部訊息通道** 管理自己的通道帳號；Admin 在 **管理後台 → 通道站台設定** 管理全站 Telegram 接收模式與 webhook 公網 Base URL。Discord 與 WhatsApp 固定 Gateway 類接收模式，不提供 mode selector。
- 一個 `(platform, character)` 一個 bot 帳號；一個帳號底下可以有多條 chat 綁定。
- Telegram 以全站一致的 polling 或 webhook 模式收訊：本機 / 私有部署適合 polling，正式公網部署可改 webhook。Telegram 生成圖片會從 Core object storage 直接 multipart 上傳，outbound 發圖不需要公網圖片 URL；只有 webhook 模式收訊仍需要公網 webhook Base URL。LINE 仍固定 webhook，且推圖走 URL 型訊息，使用 Admin 公網 Base URL 並以 `APP_BASE_URL` fallback；Discord 走 Gateway WebSocket；WhatsApp 走 WhatsApp Web / Baileys-compatible sidecar，不需要 Meta Business API 申請或公網 webhook URL。
- `allowed_sender_refs` allowlist 防止陌生人誤闖被當成「使用者」記下來。

## 目前狀態與藍圖

Yuralume 目前是 **alpha**。核心陪伴迴圈 — 聊天 / 記憶 / 行程 / 主動訊息 / LumeGram / 跨通道 / 真實世界事實注入 / 多模態 — 都已上線；接下來的重點是 **擬人化深度**（自我反思、自傳式敘事、脆弱資料保護）而非廣度。白話版更新紀錄見 [`docs/changelog/`](docs/changelog/README.md)（中 / 英 / 日）。

## 常見問題

**這是開源嗎？**
是 source-available（[BUSL-1.1](LICENSE)），不是 OSI 定義的 open source。非商業自架 / 研究 / 評估免費，商業 production 使用需另談授權。各版本公開四年後轉 Apache 2.0。

**沒有 GPU 也能跑嗎？**
可以。LLM 走外部 provider（雲端或本地 LM Studio），主畫面的角色舞台只是純 CSS 輪播既有的角色圖像。生圖 / 生影片 / TTS 都是選配且可插拔。

**多使用者可以共用一台機器嗎？**
可以，設 `AUTH_ENABLED=true` 和 `JWT_SECRET`。預設是單人模式。Per-character 隔離在 `(character_id, operator_id)` 層級，多使用者部署不會跨使用者洩漏 persona 資料。

**可以拿來做客服 bot 嗎？**
不建議。產品紅線禁止關鍵字硬規則與 deterministic rule 分支，跟任務型 agent 想要的高確定性流程衝突。

**為什麼 Python 套件名還叫 `kokoro_link`？**
2026 年 5 月對外改名 Yuralume（前身為 Kokoro-Link），但深度 rename（imports / DB / `.env`）風險太高、收益太小，所以只動對外名稱。

## 貢獻

這是個人專案，但歡迎在 license 範圍內 fork 或送 PR。動手前請先理解三條工程紅線：**LLM-first**（語意決策一律走 LLM，禁止關鍵字硬規則與 `if/else` 特判個案）、**port–adapter**（provider 一律待在 `contracts/` port 後面）、**per-character isolation**（角色與使用者之間不互漏資料）。Bug 回報請走 [GitHub Issues](../../issues)。

## 社群

一個人做的專案，來自台灣。

[Discord](https://discord.gg/BP2bpFDgR) · [X / Twitter](https://x.com/Yuralume) · [Ko-fi](https://ko-fi.com/yuralume) · [yuralume.com](https://yuralume.com)

## 致敬

站在這些巨人肩膀上：**FastAPI**、**SQLAlchemy 2**、**Alembic**、**asyncpg**、**pgvector**、**Pydantic**、**uv**、**Vue 3**、**Vite**、**Ant Design Vue**、**Open-Meteo**、**holidays**，以及 Anthropic / OpenAI / Mistral / LM Studio 各社群。

## 授權

[Business Source License 1.1](LICENSE)。各版本公開四年後轉 Apache 2.0。

---

<div align="center">
  <sub>Yuralume 前身為 Kokoro Link · LumeGram 前身為 Kokoro-Gram</sub>
</div>
