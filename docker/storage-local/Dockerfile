FROM python:3.13-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

ENV YURALUME_STORAGE_ROOT=/data
EXPOSE 9000

CMD ["uv", "run", "uvicorn", "kokoro_link.storage_service.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "9000"]
