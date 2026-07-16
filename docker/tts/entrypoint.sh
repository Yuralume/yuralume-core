#!/usr/bin/env bash
# Pre-flight + run for the Yuralume TTS container.
#
# Validates that the operator mounted the base pretrained weights —
# without them api_v2.py crashes inside model __init__ with an
# unhelpful traceback. Better to fail loud here with a fix-it hint.

set -euo pipefail

PRETRAINED_DIR="/app/GPT_SoVITS/pretrained_models"
# v4 SoVITS base lives in a subfolder per the official layout; v1 / v3
# bases sit at top level. Check the canonical paths so we don't 503
# on a perfectly fine install just because a check is too narrow.
REQUIRED_FILES=(
    "s1v3.ckpt"
    "gsv-v4-pretrained/s2Gv4.pth"
)

echo "[yuralume-tts] checking pretrained models in ${PRETRAINED_DIR}"
missing=()
for f in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "${PRETRAINED_DIR}/${f}" ]]; then
        missing+=("${f}")
    fi
done

if (( ${#missing[@]} > 0 )); then
    cat >&2 <<EOF
[yuralume-tts] ❌ 缺少基礎模型：${missing[*]}

請把它們放到容器掛載的 ${PRETRAINED_DIR} （host 端的 ./tts/pretrained/）：
  - s1v3.ckpt    — GPT base model
  - s2Gv4.pth    — SoVITS-G base model
從 https://huggingface.co/lj1995/GPT-SoVITS/tree/main 下載即可。

或者，第一次跑 download.sh 自動拉：
  docker compose run --rm yuralume-tts /app/download_pretrained.sh
EOF
    exit 2
fi

# Asset summary — helps users sanity check their volume mounts at boot.
gpt_count=$(find /app/GPT_weights_v2 /app/GPT_weights_v4 -maxdepth 2 -type f -name '*.ckpt' 2>/dev/null | wc -l)
sovits_count=$(find /app/SoVITS_weights_v2 /app/SoVITS_weights_v4 -maxdepth 2 -type f -name '*.pth' 2>/dev/null | wc -l)
ref_count=$(find /app/refs -type f -name '*.wav' 2>/dev/null | wc -l)

echo "[yuralume-tts] 已掛載資產："
echo "  GPT 角色權重 (.ckpt) : ${gpt_count}"
echo "  SoVITS 角色權重 (.pth): ${sovits_count}"
echo "  ref 音檔 (.wav)       : ${ref_count}"

if (( ref_count == 0 && gpt_count == 0 && sovits_count == 0 )); then
    echo "[yuralume-tts] ⚠ 沒掛到任何角色資產 — server 仍會起來，但每次合成都需要透過 set_*_weights 指定路徑。"
fi

ARGS=(-a 0.0.0.0 -p 9880)

# Pre-load default weights if env vars set — saves the first-request
# 10-sec cold load. Both optional; if the operator wants to switch
# per-character, the Yuralume adapter handles it via set_*_weights
# at synth time anyway.
if [[ -n "${KOKORO_TTS_DEFAULT_GPT:-}" ]]; then
    ARGS+=(-g "${KOKORO_TTS_DEFAULT_GPT}")
fi
if [[ -n "${KOKORO_TTS_DEFAULT_SOVITS:-}" ]]; then
    ARGS+=(-s "${KOKORO_TTS_DEFAULT_SOVITS}")
fi

echo "[yuralume-tts] 啟動 api_v2.py ${ARGS[*]}"
exec python api_v2.py "${ARGS[@]}"
