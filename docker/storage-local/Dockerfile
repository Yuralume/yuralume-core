FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

WORKDIR /app

RUN pip install --no-cache-dir uv==0.11.32

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

ENV YURALUME_STORAGE_ROOT=/data
EXPOSE 9000

CMD ["uv", "run", "uvicorn", "kokoro_link.storage_service.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "9000"]
