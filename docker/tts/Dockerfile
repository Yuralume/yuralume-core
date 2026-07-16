# Inference-only GPT-SoVITS image for Yuralume.
#
# Size optimisation: we don't ship training, WebUI, or dataset tooling
# — only what ``api_v2.py`` needs at runtime. Base weights / character
# weights / refs all come in via volume mount so the image stays
# stable across character additions (no rebuild required).
#
# GPU passthrough: the runtime expects ``--gpus all`` (docker run) or
# ``deploy.resources.reservations.devices`` (compose). CUDA 12.1 to
# match the upstream pytorch pin; bump together with PyTorch if you
# move forward.

FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime

# System deps:
#   ffmpeg, libsndfile1 → librosa / soundfile WAV/MP3 IO
#   git → fetching the GPT-SoVITS source
#   curl → healthcheck
#   build-essential → some optional Python deps compile native code
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    git \
    curl \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pin a known-working commit. Bump when upstream ships a needed fix —
# don't track main, the API surface drifts.
ARG GPT_SOVITS_REF=main
RUN git clone --depth 1 --branch ${GPT_SOVITS_REF} \
        https://github.com/RVC-Boss/GPT-SoVITS.git /tmp/repo \
 && cp -a /tmp/repo/. /app/ \
 && rm -rf /tmp/repo

# Inference-only requirements. Upstream ships requirements.txt that
# pulls in training / WebUI / Gradio dependencies we don't need; we
# install the union to stay compatible without auditing transitively
# (CUDA layer is the dominant size, not the wheel diff).
#
# IMPORTANT: filter ``torch`` / ``torchaudio`` / ``torchvision`` out
# before install — the base image already ships a matched-pair set
# (torch 2.4.0 + torchaudio 2.4.0 + CUDA 12.1). GPT-SoVITS's pinned
# torch is newer than its torchaudio pin, so ``pip install -r`` left
# us with torch 2.11 + torchaudio 2.4 → ABI mismatch on libtorchaudio
# at import time. Pinning to the base image keeps the pair coherent.
RUN sed -i -E '/^(torch|torchaudio|torchvision)([><=!~ ]|$)/d' requirements.txt \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir --force-reinstall --no-deps \
        torch==2.11.0 torchaudio==2.11.0 torchvision==0.26.0 \
 && pip install --no-cache-dir torchcodec

# Pre-download NLTK corpora used by the BERT feature extractor.
# Without these, the first synth request crashes inside ``g2p_en``
# with "Resource 'averaged_perceptron_tagger_eng' not found". Doing it
# here means a deployment without internet at runtime still works,
# and more importantly the first request doesn't 502 while nltk
# auto-fetch is in flight.
RUN python -m nltk.downloader -d /usr/share/nltk_data \
        averaged_perceptron_tagger_eng \
        cmudict
# Why 2.11 instead of the base image's 2.4: GPT-SoVITS' transitive
# deps (accelerate / lightning / transformers) require torch ≥ 2.5,
# and modern GPUs (Blackwell sm_120 / RTX 50-series) need pre-built
# kernels that only ship from torch 2.7+. The three-way pin keeps
# torch / torchaudio / torchvision C++ ABI in sync — mismatch
# manifests as ``undefined symbol`` (torchaudio) or
# ``operator torchvision::nms does not exist`` (transformers loading
# vision models). torchvision 0.26.0 pairs with torch 2.11.0.

# Strip docs / webui only — DO NOT delete ``tools/``, it's a runtime
# package that ``api_v2.py`` imports from (``tools.i18n.i18n``).
RUN rm -rf docs/ webui.py go-webui.* \
        2>/dev/null || true

# Mount points the entrypoint expects to exist (volume mounts will
# provide actual content at runtime). Pre-creating means a missing
# mount produces a clear "empty" rather than ENOENT.
RUN mkdir -p \
    /app/GPT_SoVITS/pretrained_models \
    /app/GPT_weights_v2 \
    /app/GPT_weights_v4 \
    /app/SoVITS_weights_v2 \
    /app/SoVITS_weights_v4 \
    /app/refs

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 9880

HEALTHCHECK --interval=20s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fs http://localhost:9880/openapi.json >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
