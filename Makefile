ifeq ($(OS),Windows_NT)
SHELL := C:/Program Files/Git/bin/bash.exe
.SHELLFLAGS := -c
endif

COMPOSE_PROJECT_NAME ?= kokoro-link
export COMPOSE_PROJECT_NAME

.PHONY: dev dev-backend dev-frontend test evals build-frontend kill \
        db-up db-down db-reset db-wait db-migrate

# One-click: backend + frontend in parallel, Ctrl+C kills both
dev: kill db-wait db-migrate
	@echo "Starting Yuralume..."
	@echo "  Backend:  http://127.0.0.1:8002"
	@echo "  Frontend: http://127.0.0.1:5174"
	@echo ""
	@trap 'kill 0' INT TERM EXIT; \
	( KOKORO_LOG_LEVEL=info uv run python -m uvicorn kokoro_link.api.app:create_app --factory --reload --host 0.0.0.0 --port 8002 --log-level info 2>&1 ) & \
	( cd frontend && npx vite --strictPort --host 127.0.0.1 2>&1 ) & \
	wait

dev-backend: db-wait db-migrate
	KOKORO_LOG_LEVEL=info uv run python -m uvicorn kokoro_link.api.app:create_app --factory --reload --host 127.0.0.1 --port 8002 --log-level info

dev-frontend:
	cd frontend && npx vite --strictPort --host 127.0.0.1

test:
	uv run python -m pytest

# LLM evals — opt-in regression net for "character feels like a human".
# Requires KOKORO_EVALS_SYSTEM_ENDPOINT and KOKORO_EVALS_JUDGE_ENDPOINT
# (point at LM Studio / equivalent OpenAI-compatible server). Without
# those env vars set, fixtures auto-skip with a clear message.
evals:
	uv run python -m pytest -m evals -v

build-frontend:
	cd frontend && npm run build

# Kill stale processes on dev ports
kill:
	@for port in 8002 5174; do \
		for pid in $$(netstat -ano 2>/dev/null | grep ":$$port " | grep LISTENING | awk '{print $$5}' | sort -u); do \
			taskkill //PID $$pid //F 2>/dev/null || true; \
		done; \
	done

# ===== Database (PostgreSQL via docker compose) =====

# Start the Postgres container in the background.
db-up:
	docker compose up -d postgres

# Stop the container but keep the data volume.
db-down:
	docker compose down

# Nuclear option: destroy the data volume too.
db-reset:
	docker compose down -v
	docker compose up -d postgres
	@$(MAKE) db-wait
	@$(MAKE) db-migrate

# Block until Postgres accepts connections — used internally by `dev`.
db-wait: db-up
	@echo "Waiting for Postgres..."
	@for i in $$(seq 1 60); do \
		docker compose exec -T postgres pg_isready -U kokoro -d kokoro_link >/dev/null 2>&1 && echo "  ready" && exit 0; \
		sleep 1; \
	done; \
	echo "  postgres did not become ready in 60s"; exit 1

# Run any pending Alembic migrations.
db-migrate:
	uv run python -m alembic upgrade head
