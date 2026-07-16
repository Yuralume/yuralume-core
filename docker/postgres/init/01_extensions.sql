-- Enable pgvector ahead of Phase B (semantic memory retrieval).
-- Running at init time means Alembic migrations that reference the
-- `vector` type will just work without extra setup steps.
CREATE EXTENSION IF NOT EXISTS vector;
