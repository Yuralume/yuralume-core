FROM node:22-bookworm-slim AS frontend-builder

WORKDIR /app

# Demo OAuth client ids + conversion links are Vite build-time constants
# (import.meta.env.VITE_*). They must exist during `npm run build`, otherwise the
# shipped SPA bundle bakes empty client ids and the live demo OAuth start page
# fails with "Demo unavailable" before it can redirect to Discord/Google.
# Pass real values via --build-arg (or compose build.args) for a hosted demo;
# leaving them empty keeps self-host builds unchanged.
ARG VITE_YURALUME_DEMO_DISCORD_CLIENT_ID=
ARG VITE_YURALUME_DEMO_GOOGLE_CLIENT_ID=
ARG VITE_YURALUME_DEMO_TIER0_URL=
ARG VITE_YURALUME_DEMO_WAITLIST_URL=
ARG VITE_YURALUME_DEMO_DISCORD_URL=
ARG VITE_YURALUME_DEMO_SELF_HOST_URL=
ENV VITE_YURALUME_DEMO_DISCORD_CLIENT_ID=${VITE_YURALUME_DEMO_DISCORD_CLIENT_ID} \
    VITE_YURALUME_DEMO_GOOGLE_CLIENT_ID=${VITE_YURALUME_DEMO_GOOGLE_CLIENT_ID} \
    VITE_YURALUME_DEMO_TIER0_URL=${VITE_YURALUME_DEMO_TIER0_URL} \
    VITE_YURALUME_DEMO_WAITLIST_URL=${VITE_YURALUME_DEMO_WAITLIST_URL} \
    VITE_YURALUME_DEMO_DISCORD_URL=${VITE_YURALUME_DEMO_DISCORD_URL} \
    VITE_YURALUME_DEMO_SELF_HOST_URL=${VITE_YURALUME_DEMO_SELF_HOST_URL}

COPY frontend/package*.json ./frontend/
RUN cd frontend && npm ci

COPY frontend ./frontend
# Admin → Developer Docs `?raw`-imports specific repo docs straight into the SPA
# bundle (see frontend/src/utils/devDocs.ts). They live outside frontend/, so the
# builder stage needs each imported doc present or `npm run build` fails with
# UNRESOLVED_IMPORT. Copy only the docs the admin page embeds — do NOT `COPY docs`
# wholesale (it's 1.4M of internal dev plans). This list is the allowlist of which
# repo docs get baked into the browser bundle; add a line whenever devDocs.ts gains
# a doc.
COPY docs/CUSTOM_MEDIA_GATEWAY_SPEC.md ./docs/CUSTOM_MEDIA_GATEWAY_SPEC.md
COPY docs/CUSTOM_TTS_SERVER_SPEC.md ./docs/CUSTOM_TTS_SERVER_SPEC.md
RUN cd frontend && npm run build


FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS app

WORKDIR /app

ARG YURALUME_BUILD_TAG=
ARG YURALUME_BUILD_SHA=
ARG YURALUME_BUILD_TIME=

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}" \
    KOKORO_UPLOADS_DIR=/tmp/yuralume-uploads \
    YURALUME_BUILD_TAG="${YURALUME_BUILD_TAG}" \
    YURALUME_BUILD_SHA="${YURALUME_BUILD_SHA}" \
    YURALUME_BUILD_TIME="${YURALUME_BUILD_TIME}"

COPY pyproject.toml uv.lock README.md alembic.ini usage-prices.openai.json ./
COPY alembic ./alembic
COPY src ./src
COPY --from=frontend-builder /app/src/kokoro_link/frontend/dist ./src/kokoro_link/frontend/dist

RUN uv sync --frozen --no-dev \
    && mkdir -p /tmp/yuralume-uploads \
    && useradd --create-home --uid 10001 yuralume \
    && chown -R yuralume:yuralume /app /tmp/yuralume-uploads

USER yuralume

EXPOSE 8002

CMD ["uvicorn", "kokoro_link.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8002"]
