FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
# Deliberately copy only the storage runtime closure. Hosted release tooling
# hashes the same paths and can therefore reuse this image for app-only Core
# releases without smuggling unrelated application code into the artifact.
COPY src/kokoro_link/__init__.py ./src/kokoro_link/__init__.py
COPY src/kokoro_link/contracts/__init__.py ./src/kokoro_link/contracts/__init__.py
COPY src/kokoro_link/contracts/object_storage.py ./src/kokoro_link/contracts/object_storage.py
COPY src/kokoro_link/infrastructure/__init__.py ./src/kokoro_link/infrastructure/__init__.py
COPY src/kokoro_link/infrastructure/storage/__init__.py ./src/kokoro_link/infrastructure/storage/__init__.py
COPY src/kokoro_link/infrastructure/storage/keys.py ./src/kokoro_link/infrastructure/storage/keys.py
COPY src/kokoro_link/storage_service ./src/kokoro_link/storage_service

RUN uv sync --frozen --no-dev && uv cache clean

ENV YURALUME_STORAGE_ROOT=/data
EXPOSE 9000

CMD ["uv", "run", "uvicorn", "kokoro_link.storage_service.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "9000"]
