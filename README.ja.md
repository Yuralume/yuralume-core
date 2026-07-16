<div align="center">
  <img src="frontend/public/logo.png" alt="Yuralume" width="180" />

  # Yuralume

  **セルフホスト型 AI コンパニオンプラットフォーム — キャラクター一人ひとりに、自分の日々がある。**
  <br/>毎日のスケジュール。プラットフォームを跨いで続く記憶。自分のソーシャルフィード。あなたに先にメッセージを送る自由。

  [![License: BUSL-1.1](https://img.shields.io/badge/License-BUSL--1.1-blue.svg)](LICENSE)
  [![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)
  [![Vue 3](https://img.shields.io/badge/Vue-3-42b883.svg)](https://vuejs.org/)
  [![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-336791.svg)](https://www.postgresql.org/)
  [![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#現状とロードマップ)

  [English](README.md) · [繁體中文](README.zh_TW.md) · **日本語**

  [yuralume.com](https://yuralume.com) · [ライブデモ](https://yuralume.com/#demo) · [セルフホストガイド](https://yuralume.com/selfhost/ja.html) · [Discord](https://discord.gg/BP2bpFDgR)
</div>

---

## なぜ Yuralume を作ったのか

主流の AI チャットプロダクトは、キャラクターを「受け身の Web ページ」として扱います。聞けば答える、タブを閉じれば存在しなくなる、デバイスが変わればゼロから、そして会話はすべて誰かのクラウドへ。

Yuralume はその逆を行きます。ここのキャラクターには **自分の一日** があります — LLM が計画するスケジュール、セッションとチャンネルを越えて残る記憶、自分から投稿するソーシャルフィード、そして本当に伝えるべきことがあるときだけあなたに先にメッセージを送る分別。すべての意味的な判断は LLM を通して行われ、キーワードのハードルールや個別ケースの `if/else` パッチは禁止しています。

データはデフォルトであなたのマシンに留まります。サードパーティの API は、あなたが明示的に有効化してキーを設定したときにだけ呼ばれます。

## 誰のためのものか

- **長期的なコンパニオンを求めるユーザー** — 一人か二人の AI キャラクターと長く暮らしたい。会話のプライバシーと振る舞いのリアリティを大事にしていて、自分のハードウェアでサービスを動かすことを厭わない。
- **インタラクティブナラティブ / キャラクター研究の開発者** — LLM 駆動 NPC の足場（スケジュール / 記憶 / 自発メッセージ / クロスプラットフォーム同期）が欲しいが、ゼロから作り直したくはない。
- **世界観を見せたいコンテンツクリエイター** — 静的なキャラクターシートを「一日を生きる姿を見せられる存在」に変えたい。

**向いていない方**：ターンキーの SaaS が欲しい方、B2B カスタマーサポート bot、決定論的なタスクランナー agent — これらはプロダクトの設計レッドラインが明確に除外している用途です。

## 体感が変わる、3 つの典型シーン

1. **朝は忙しくて返せない、午後に自分から続きを。** 朝 9 時にメッセージを送ると、キャラクターは「会議中」— 短く返して *保留* します。昼過ぎにスケジュールが空くと、スケジューラーが自発フォローアップをトリガーし、朝の会話をきちんと拾い直します。
2. **デバイスを跨いでも同じ一人。** 朝は Web で話し、夜は Telegram・Discord・WhatsApp で同じキャラクターにおやすみを言う。朝のスレッドを覚えているのは、bot が同期しているからではなく、記憶プールが一つしかないからです。
3. **あなたがいない間も、生きていた。** 一週間アプリを開かなくても、戻ってくればフィードには新しい投稿、受信箱には「ふと思い出して」のメッセージが二通 — どれも三層の intention ゲートを通過したものだけ。

<div align="center">
<img src="assets/schedule_proactive_tg_demo.webp" alt="自分のスケジュールで一日を過ごし、自発的に Telegram メッセージを送るキャラクター" width="760" />
<br/><sub>スケジュール、ストーリーイベント、そして自発的に届く Telegram メッセージ — 実機キャプチャ、1.5 倍速。</sub>
</div>

## 主な機能

- **ストリーミングチャット** — ターンごとの感情 / 状態付き。マルチモーダル入力（Web はドラッグ&ドロップ、Telegram / LINE / Discord / WhatsApp は画像送信）、キャラクターごとに選べる TTS 再生。
- **ガイド付きキャラクター作成** — AI による名前候補、16 タイプの性格リファレンス（任意）、初期関係の明示的なセットアップ、関係を反映したチャット / スケジュール / 自発行動。
- **境界のある同空間チャット** — Web の Stage は「同じ場所にいる」感覚を出せますが、Scene Access がキャラクターの現在のシーンと直近の対話・任意の現在ステータスを先に照合します。不合理な場合はスマホ風メッセージ、待ち合わせの提案、状況の補足へ誘導します。
- **構造化された長期記憶** — 五層のペルソナモデル（identity / life / emotional / interaction / trust）、pgvector による意味検索、夜間の dream 統合。キャラクターはそれぞれ *自分自身の* あなた像を構築します。
- **キャラクター同士のソーシャル知識** — 関係セットアップで「相手について既に知っていること」を seed でき、encounter / chat から方向付きの関係記憶を抽出、コンパクトな人物名簿に統合してチャットへ還元します（生のスコアは露出しません）。
- **余韻の残る毎日のスケジュール** — LLM が一日を計画し、終わった活動は当日のタイムラインに現れ、感情の余韻を残し、エピソード記憶になります。共有活動は、あなたが同意した場合にのみ共有記憶になります。
- **三重ゲートの自発メッセージ** — 軽量ヒューリスティック → LLM intention judge → LLM decider、チャンネルごとのオプトインと一日の上限付き。意味があるときだけ話しかけ、ないときは黙っています。
- **チャンネルを跨いで一つのアイデンティティ** — キャラクターを Telegram・LINE・Discord・同梱の WhatsApp ゲートウェイに紐付け。どのサーフェスでも同じ記憶 / 状態 / スケジュールが動きます。
- **LumeGram ソーシャルフィード** — キャラクターごとの IG 風タイムライン。スケジューラーが六つのシグナル（スケジュール / ストーリービート / 記憶 / 世界の出来事 / 沈黙 / 状態変化）から投稿タイミングを決め、睡眠などの高負荷スロットを尊重します。コメント返信は tick 駆動でリアルタイムではありません。
- **実世界ファクトの注入** — 祝日（`holidays`）、天気（Open-Meteo、キー不要）、キュレーション済み RSS ニュースが *事実* としてプロンプトに流れ、反応するかどうかは LLM が判断します。ハードコードされた振る舞いはありません。
- **ストーリーツール** — キャラクターのストーリーアーク、複数キャラクターの fusion 短編、分岐ドラマ（VN 風）ジェネレーター。演じられたアークのビートは履歴として残り、完了したアークはマイルストーン記憶を残します。
- **機能グループ × 機能単位 × キャラクター単位の LLM ルーティング** — Admin のルーティング設定がサイト全体のデフォルトを書き、グループごとにモデル階層を設定し、チャット・記憶抽出・画像認識などの例外を個別に pin、必要ならキャラクター単位で上書き。BYOK のプロバイダキーは Admin UI から設定し、暗号化して保存されます。
- **生成使用量台帳** — Admin Observability がチャット、バックグラウンド / 補助 LLM、画像、動画、TTS の使用量を追跡します（プロンプトや生成コンテンツは保存しません）。機能 / プロバイダ / モデル別、**キャラクター別** のコスト比較、キャッシュヒット、estimated / actual、hosted ルーティング時の Cloud Gateway request id まで確認できます。
- **セルフホスト NSFW モードの土台** — ユーザーごとの手動・一時モード。期間中の LLM / 画像呼び出しは Admin 指定のコミュニティ向けターゲットにルーティングされ、アイドル TTL で自動失効します。書き込まれたターンは `content_mode` と任意の `safe_summary` を持ち、長期記憶は born-safe で保存されます。hosted cloud モードでは API はロックされたままです。
- **キャラクター画像ステージ** — 生成済み画像のフェード切り替えスライドショーがメインビューの背景に。デスクトップ横向き / モバイル縦向きのレスポンシブ、iOS / Android のセーフエリア対応。

## 実際の動き

<div align="center">
<img src="assets/create_char_demo.webp" alt="Web UI でゼロから新しいキャラクターを作成" width="760" />
<br/><sub>キャラクター作成フロー — 空のフォームから、生きて動き出すキャラクターまで。6 倍速。</sub>
<br/><br/>
<img src="assets/gram_memories_demo.webp" alt="キャラクターの LumeGram フィードと回想録を閲覧" width="760" />
<br/><sub>LumeGram — キャラクターごとの IG 風フィードと、あなたについて綴られる回想録。</sub>
</div>

フル解像度（音声つき）のクリップは [yuralume.com](https://yuralume.com/#demo) へ。

## クイックスタート

### 一行インストール（ビルド済み image）

一番速い道 — 公開済みの Docker image を取得して、スタック全体を立ち上げます：

**macOS / Linux**

```bash
curl -fsSL https://yuralume.com/install.sh | bash
```

**Windows（PowerShell）**

```powershell
irm https://yuralume.com/install.ps1 | iex
```

[Docker](https://www.docker.com/products/docker-desktop/) が必要です。アプリは `http://127.0.0.1:8012` で立ち上がります。公開 image はこのリポジトリの [CI](.github/workflows/publish-images.yml) からビルドされています — ソースからビルドしたものが、そのまま配布物です。実行前にスクリプトを読みたい、または手動で一歩ずつ進めたい場合は[セルフホストガイド](https://yuralume.com/selfhost/ja.html)へ。

<div align="center">
<img src="assets/self-host_install_demo_timelapse.webp" alt="一行インストールのタイムラプス — コマンド一つから話せるキャラクターまで" width="760" />
<br/><sub>インストールの全工程 — 一行のコマンドから話せるキャラクターまで。3 分を約 24 秒に圧縮。
<a href="https://yuralume.com/self-host_install_demo.mp4">等速で見る</a>（カットなし）。</sub>
</div>

開発やソースからのビルドは以下へ：

### 前提条件

- Python 3.13 + [uv](https://docs.astral.sh/uv/)
- Node.js 22+ と npm
- Docker Desktop（PostgreSQL + 統合テスト）
- Windows は Git for Windows（`make` を提供）

### ローカル開発

```bash
uv sync                          # バックエンド依存
cd frontend && npm install && cd ..

make dev                         # PostgreSQL 起動、migration 実行、バックエンド（:8002）+ フロントエンド（:5174）
```

`Ctrl+C` で両方停止します。Windows では `run-dev.cmd` をダブルクリックでも起動できます。
`make` はデフォルトで `COMPOSE_PROJECT_NAME=kokoro-link` を固定し、リポジトリを `Yuralume-Core` にリネームした後も既存のローカル PostgreSQL コンテナと `kokoro_postgres_data` ボリュームを使い続けられるようにしています。

起動後は `http://127.0.0.1:5174` を開き、**Admin → Provider Keys** で少なくとも一つの LLM プロバイダを設定してください。同梱の `fake` プロバイダはスモークテスト用にアプリを動かせますが、実際の会話は生成しません。

### Docker Compose でのセルフホスト

**ソースをチェックアウトして**（開発者向け）：

```powershell
Copy-Item .env.container.example .env.container
docker compose -f docker-compose.container.yml --profile storage-local up -d --build
```

`--build` は四つの image をローカルでビルドします。フラグを外すと ghcr.io から公開 image を pull します。

**クローンせずに**（エンドユーザー向け）— 必要なのは二つのファイルだけ：

```bash
mkdir yuralume && cd yuralume
curl -O https://raw.githubusercontent.com/Yuralume/yuralume-core/main/docker-compose.container.yml
curl -O https://raw.githubusercontent.com/Yuralume/yuralume-core/main/.env.container.example
cp .env.container.example .env.container
# （.env.container を編集 — 最低限 APP_BASE_URL、CONFIG_ENCRYPTION_KEY、STORAGE_KEY を設定）
docker compose -f docker-compose.container.yml --profile storage-local pull
docker compose -f docker-compose.container.yml --profile storage-local up -d
```

どちらのフローでも PostgreSQL は `5554`、ローカル object storage は `9012`、同梱 WhatsApp sidecar は `32190`、アプリ全体は `http://127.0.0.1:8012` で立ち上がります。`migrate` サービスがアプリ起動前に `alembic upgrade head` を実行します。`.env.container` の `YURALUME_IMAGE_TAG=v0.1.0` で特定ビルドに固定できます（デフォルトは `latest`）。稼働中のアプリは `GET /api/v1/system/version` でバージョン / image タグ / commit / build 時刻を返します。image は `ghcr.io/yuralume/yuralume-core/{app,storage-local,postgres,whatsapp-sidecar}` に `linux/amd64` と `linux/arm64` の両アーキテクチャで公開されています。

自分のローカル ComfyUI で画像を生成したい場合：Core は ComfyUI と直接は話しません — あなた自身が実装・運用する **Custom Media Gateway** に対して、小さく正規化された HTTP コントラクトを話します。完全な仕様と最小の FastAPI スターターは [`docs/CUSTOM_MEDIA_GATEWAY_SPEC.md`](docs/CUSTOM_MEDIA_GATEWAY_SPEC.md)（英語）、またはアプリ内 **Admin → Developer docs** から。

自分の音声エンジン（GPT-SoVITS、XTTS…）を繋ぎたい場合：TTS も同様に小さな HTTP コントラクト（`GET /voices` + `POST /tts/synthesize`）を話す **Custom TTS Server** 方式です。仕様と GPT-SoVITS をラップする最小スターターは [`docs/CUSTOM_TTS_SERVER_SPEC.md`](docs/CUSTOM_TTS_SERVER_SPEC.md)（英語）、またはアプリ内 **Admin → Developer docs** から。自前で立てたくない場合は内蔵の OpenAI TTS プロバイダを BYOK で使えます。

プロンプトのオーバーレイは `./prompts/tuned` 配下に `src/kokoro_link/data/prompts/` と同じ相対パスでファイルを置き、`.env.container` に `YURALUME_PROMPT_PACK_DIR=/app/prompts/tuned` を設定して `app` サービスを再起動します。起動時のログに `Prompt pack overlay loaded` とテンプレート数が出ます。

リバースプロキシ配下に置く場合は、**Admin → Provider Keys** で許可している最長のプロバイダ呼び出しよりプロキシのレスポンスタイムアウトを長くしてください。hosted の画像 / 動画 / TTS プロバイダは数分かかることがあり、nginx の一般的な 60 秒デフォルトはブラウザ側の `504 Gateway Timeout` として現れます（例：`proxy_read_timeout 300s; proxy_send_timeout 300s;`）。

スタックが健全になったら、スモークスクリプトでデプロイを検証できます：

```bash
uv run python scripts/self_host_smoke.py            # ベースライン（プロバイダキーなし）
uv run python scripts/self_host_smoke.py --openai-key sk-...   # BYOK のラウンドトリップも
```

## 設定

`.env.example` が SoT（信頼できる唯一の情報源）です。よく触る変数：

| 変数 | 用途 |
|---|---|
| `DATABASE_URL` | PostgreSQL 接続（asyncpg）。デフォルトは docker-compose の dev db。 |
| `CONFIG_ENCRYPTION_KEY` | **必須**。これがないと Admin UI でプロバイダキーを保存できません。長いランダム文字列を。 |
| `AUTH_ENABLED` | `false` でシングルユーザーモード（デフォルト）。マルチユーザーは `true` + `JWT_SECRET`。 |
| `USER_PRIMARY_LANGUAGE` | ローカル単一ユーザーの初期 UI + コンテンツ言語（デフォルト `zh-TW`、`en-US`・`ja-JP` 可）。シングルユーザーモードでは初回起動時にデフォルト operator に seed され、UI とキャラクターの返答言語が最初から正しくなります。セルフホストのインストーラーはインストール時の選択で設定します。マルチユーザーモードでは各ユーザーが `/auth/setup` で選びます。 |
| `USER_TIMEZONE` / `KOKORO_USER_TIMEZONE` | ローカル単一ユーザーの UI タイムゾーン（IANA、例 `Asia/Tokyo`）。民間日付・スケジュール・「今日」の境界を決めます。DB とサーバーの instant は UTC のまま。デフォルト `UTC`。 |
| `YURALUME_CLOUD_ENABLED` / `YURALUME_CLOUD_*` | Hosted cloud モード。有効化するとローカルの setup / user / provider 管理はロックされ、認証は Yuralume Cloud User service へ、LLM / 画像 / 動画 / TTS は Cloud Gateway 経由になります。 |
| `VITE_YURALUME_DEMO_DISCORD_CLIENT_ID` / `VITE_YURALUME_DEMO_GOOGLE_CLIENT_ID` | Hosted demo OAuth の SPA client id。Vite の build 時値なので、shell env・`docker compose --env-file .env.container`・GitHub repository variables のいずれかで build 前に渡す必要があります。 |
| `VITE_YURALUME_DEMO_*_URL` | 任意。hosted demo フロントエンドの導線リンク（Tier0 / waitlist / Discord / self-host CTA）。 |
| `WEB_PUSH_VAPID_PUBLIC_KEY` / `WEB_PUSH_VAPID_PRIVATE_KEY` / `WEB_PUSH_VAPID_SUBJECT` | 任意。Browser Web Push の資格情報。設定するとプレイヤー設定からこのブラウザの OS 通知を購読できます。未設定でも push API は `configured=false` を返し、実行時配信は fail-soft。 |
| `STORAGE_URL` / `STORAGE_KEY` | Object Storage エンドポイント。セルフホストは同梱の `storage-local` を使えます。 |
| `APP_BASE_URL` / `STORAGE_PUBLIC_URL` | ブラウザ向け origin の fallback。メディア参照はデフォルトで app 相対の `/v1/public/...`。URL でプッシュするプラットフォームは Admin **Channel settings → Public Base URL** を優先し、空のとき `APP_BASE_URL` を使います。 |
| `CALENDAR_REGION`、`WEATHER_LATITUDE/LONGITUDE` | 任意。ユーザーに保存済みロケーションがない場合の実世界ファクト注入の fallback。 |
| `YURALUME_PROMPT_PACK_DIR` | 任意。プロンプトパックのオーバーレイディレクトリ。同一相対パスの `.txt` が `src/kokoro_link/data/prompts/` を上書きし、各 `TurnRecord` に `prompt_pack_hash` が記録されます。 |
| `KOKORO_USAGE_PRICE_CATALOG_PATH` | 任意。生成使用量台帳のローカル JSON 価格カタログ。同梱の `usage-prices.openai.json` は OpenAI Standard の LLM token 価格をカバー。未設定でも使用量は記録され、コストは zero / unknown estimated になります。 |
| `NSFW_MODE_TTL_SECONDS` / `KOKORO_NSFW_MODE_TTL_SECONDS` | 任意。セルフホスト NSFW モードのアイドル TTL、デフォルト `1800` 秒。モードはユーザーが手動で有効化し、Admin 指定のコミュニティ LLM / 画像ターゲットを使用。hosted cloud モードではロックされます。 |
| `TAVILY_API_KEY` | 任意。`web_search` ツールを有効化。 |

**LLM / embedding / 画像 / 動画 / TTS のプロバイダキーは `.env` には置きません。** **Admin → Provider Keys** から実行時に設定し、データベースに暗号化保存され、API から平文で返ることはありません。

旧 `KOKORO_*` 環境変数は fallback として読まれますが、新しいデプロイでは `.env.example` の命名を使ってください。

## アーキテクチャ

```text
src/kokoro_link/
  api/            # FastAPI ルート
  application/    # ユースケース、DTO、サービス
  bootstrap/      # コンテナ、設定、起動時の wiring
  contracts/      # Port インターフェース（プロバイダ非依存）
  domain/         # エンティティ、値オブジェクト
  infrastructure/ # リポジトリ、LLM アダプタ、永続化
frontend/         # Vue 3 + Vite + Ant Design Vue
alembic/          # データベース migration
docker/           # ローカルインフラの Dockerfile
tests/{unit,integration}/
```

システムは **port–adapter** を厳格に守ります：LLM プロバイダ、embedder、画像生成、動画生成、TTS、メッセージチャンネルはすべて `contracts/` の port の後ろにいます。プロバイダの差し替えはレジストリの変更であって、リファクタリングではありません。

## メッセージチャンネル

各キャラクターは Telegram・LINE・Discord・WhatsApp のチャットに紐付けられます。そこでのメッセージは Web UI と同じ `ChatService` を通るため、記憶 / 感情状態 / スケジュールはすべてのサーフェスで一貫します。

- プレイヤーはキャラクターの **設定 → 外部メッセージチャンネル** から自分のチャンネルアカウントを管理します。Admin は **Admin → Channel settings** からサイト全体の Telegram 受信モードと webhook の Public Base URL を管理します。
- `(platform, character)` ごとに一つの bot アカウント。アカウントの下に複数のチャット紐付けが可能。
- Telegram はサイト全体の polling / webhook モードで受信。生成画像は Core の object storage から multipart で直接アップロードされるため、公開画像 URL は不要です。LINE は webhook 固定、Discord は Gateway WebSocket、WhatsApp は WhatsApp Web / Baileys 互換の sidecar 経由で、Meta Business API の承認や公開 webhook URL は不要です。
- `allowed_sender_refs` の allowlist が、見知らぬ人が「ユーザー」として記録されるのを防ぎます。

## 現状とロードマップ

Yuralume は **alpha** です。コアのコンパニオンループ — チャット / 記憶 / スケジュール / 自発メッセージ / LumeGram / クロスチャンネル / 実世界ファクト注入 / マルチモーダル — は揃っています。この先の重点は、広さよりも **人間らしさの深さ**（自己内省、自伝的ナラティブ、脆弱性データの保護）です。わかりやすい言葉のリリースノートは [`docs/changelog/`](docs/changelog/README.md)（日 / 英 / 中）にあります。

## よくある質問

**これはオープンソースですか？**
[BUSL-1.1](LICENSE) の source-available であり、OSI 定義のオープンソースではありません。非商用のセルフホスト・研究・評価は無料。商用のプロダクション利用には別途ライセンスが必要です。各バージョンは初公開から四年後に Apache 2.0 に転換します。

**GPU なしで動きますか？**
はい。LLM は外部プロバイダ（クラウドまたはローカルの LM Studio）経由です。キャラクター画像ステージは生成済み画像の CSS スライドショーにすぎません。画像 / 動画 / TTS プロバイダはすべて任意でプラガブルです。

**複数ユーザーで一つのインスタンスを共有できますか？**
はい — `AUTH_ENABLED=true` と `JWT_SECRET` を設定してください。デフォルトはシングルユーザーモードです。キャラクター単位の分離は `(character_id, operator_id)` レベルで強制され、マルチユーザー環境でもユーザー間でペルソナデータは漏れません。

**カスタマーサポート bot として使えますか？**
いいえ。設計のレッドラインがキーワードのハードルールと決定論的な分岐を禁じており、タスク駆動 agent が求めるものと正反対です。

**なぜ Python パッケージ名はまだ `kokoro_link` なのですか？**
2026 年 5 月に対外名を Yuralume に変更しました（旧名 Kokoro-Link）が、import / DB / `.env` の深いリネームはリスクが高い割に得るものが少ないと判断し、対外名だけを変えました。

## コントリビュート

個人プロジェクトですが、ライセンスの範囲内での fork や PR を歓迎します。着手前に理解してほしい三つのエンジニアリング・レッドライン：**LLM-first**（意味的な判断は必ず LLM を通す — キーワードのハードルールや個別ケースの `if/else` パッチ禁止）、**port–adapter**（プロバイダは `contracts/` の port の後ろに）、**per-character isolation**（キャラクターとユーザーの間でデータを漏らさない）。バグ報告は [GitHub Issues](../../issues) へ。

## コミュニティ

台湾から、一人の開発者が作っています。

[Discord](https://discord.gg/BP2bpFDgR) · [X / Twitter](https://x.com/Yuralume) · [Ko-fi](https://ko-fi.com/yuralume) · [yuralume.com](https://yuralume.com)

## 謝辞

これらの巨人の肩の上に：**FastAPI**、**SQLAlchemy 2**、**Alembic**、**asyncpg**、**pgvector**、**Pydantic**、**uv**、**Vue 3**、**Vite**、**Ant Design Vue**、**Open-Meteo**、**holidays**、そして Anthropic / OpenAI / Mistral / LM Studio の各コミュニティ。

## ライセンス

[Business Source License 1.1](LICENSE)。各バージョンは初公開から四年後に Apache 2.0 に転換します。

---

<div align="center">
  <sub>Yuralume の旧名は Kokoro Link · LumeGram の旧名は Kokoro-Gram</sub>
</div>
