#!/usr/bin/env bash
# One-shot downloader for the GPT-SoVITS base weights.
#
# Run inside the container: ``docker compose run --rm yuralume-tts /app/download_pretrained.sh``.
# Files land in /app/GPT_SoVITS/pretrained_models which is volume-mounted,
# so the host side caches the result — re-running is a no-op.

set -euo pipefail

DEST="/app/GPT_SoVITS/pretrained_models"
mkdir -p "${DEST}"

BASE="https://huggingface.co/lj1995/GPT-SoVITS/resolve/main"

declare -a FILES=(
    "s1v3.ckpt"
    "s2Gv4.pth"
)

for f in "${FILES[@]}"; do
    if [[ -f "${DEST}/${f}" ]]; then
        echo "[download] already have ${f}"
        continue
    fi
    echo "[download] fetching ${f} ..."
    curl -L --fail -o "${DEST}/${f}" "${BASE}/${f}"
done

echo "[download] base weights ready at ${DEST}"
