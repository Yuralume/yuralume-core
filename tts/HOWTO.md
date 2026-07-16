# Yuralume · TTS 角色資產

這個目錄是「角色語音檔案直接丟這裡」的入口。docker-compose 起 `yuralume-tts`
service 時會把整個 `tts/` 掛進容器，新增角色不用重 build image，也不用重啟
backend。

## 目錄結構

```
tts/
├── pretrained/                          # GPT-SoVITS 基礎模型（必要）
│   ├── s1v3.ckpt
│   └── s2Gv4.pth
├── GPT_weights_v4/                      # 各角色 GPT 模型
│   ├── kokkoro.ckpt
│   └── hakua.ckpt
├── SoVITS_weights_v4/                   # 各角色 SoVITS 模型
│   ├── kokkoro.pth
│   └── hakua.pth
└── refs/                                # ref 音檔 + 逐字稿
    ├── kokkoro/
    │   ├── ref.wav
    │   └── ref.txt
    └── hakua/
        ├── ref2.wav
        └── ref2.txt
```

## 第一次啟動

1. **抓基礎模型**（一次性）：
   ```powershell
   docker compose --profile tts run --rm yuralume-tts /app/download_pretrained.sh
   ```
   完成後 `tts/pretrained/` 會有 `s1v3.ckpt` + `s2Gv4.pth`。

2. **起 server**：
   ```powershell
   docker compose --profile tts up -d yuralume-tts
   docker compose logs -f yuralume-tts    # 確認看到 "Uvicorn running on 0.0.0.0:9880"
   ```

3. **設定 `.env`**：
   ```
   KOKORO_TTS_BASE_URL=http://127.0.0.1:9880
   KOKORO_TTS_INSTALL_DIR=./tts
   ```

   重啟 Yuralume backend，角色設定面板的下拉選單就會出現你掛進去的檔案。

## 新增一個角色（最常見的事）

把四個檔案放到對應位置（檔名隨意，建議用角色英文 id 一致命名）：

| 放這裡 | 是什麼 |
|---|---|
| `tts/GPT_weights_v4/<char>.ckpt` | 該角色的 GPT 模型（社群下載 / 自訓） |
| `tts/SoVITS_weights_v4/<char>.pth` | 該角色的 SoVITS 模型 |
| `tts/refs/<char>/ref.wav` | 3–10 秒乾淨人聲（韻律參考） |
| `tts/refs/<char>/ref.txt` | 跟 ref.wav 一字不差的逐字稿（UTF-8） |

完成後到 Yuralume 角色設定 → 「角色語音（TTS）」→ 按 ↻ 重新掃描 →
**「聲音」一個下拉就搞定**（ref / GPT / SoVITS / 逐字稿自動配成一組） → 試聽 → 儲存。

> 配對邏輯：以 ref 的子目錄名為 token（例 `refs/hakua/...wav` → 找 GPT/SoVITS
> 檔名含 `hakua` 的）。檔名相同基底，自動配對；不同子目錄不會混。
> 同角色多段 ref（例 `hakua_ref1/2/3.wav`）會在下拉產出三個 row，挑情緒
> 最對的那段就行。

> 不需要重啟容器或 backend，scanner 直接讀檔案系統。

## 為什麼分四個檔

| 層 | 提供 | 來源 |
|---|---|---|
| `.ckpt` (GPT) | 角色語速、口氣 token 預測 | 社群訓練 / 自訓 |
| `.pth` (SoVITS) | 角色音色 / 聲帶特徵 | 同上，跟 ckpt 是配對的 |
| `.wav` (ref) | 這次合成的情緒 / 停頓 | 從遊戲 / 動畫剪一小段乾淨人聲 |
| `.txt` (sidecar) | ref.wav 的逐字稿，模型對齊韻律用 | 自己聽完打字 |

只缺 ckpt + pth 也能跑（zero-shot voice clone），但相似度差一截 — 詳見
`docs/TODO.md` 的「換角色的三條路」段。

## 不想用 docker

完全可以。註解掉 `docker-compose.yml` 的 `yuralume-tts` service，本機自己跑
Windows 整合包的 `api_v2.py`，把 `.env` 的 `KOKORO_TTS_INSTALL_DIR` 指到
`C:\Users\User\Desktop\GPT-SoVITS` 即可。後端設定面板的下拉選單一樣會掃。
