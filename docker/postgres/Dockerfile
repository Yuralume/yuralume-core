FROM pgvector/pgvector:pg16

# Bake the extension init script in so deployments that pulled the
# image (instead of cloning the repo) don't need a bind-mount of the
# host's docker/postgres/init/ directory. Files in this directory are
# executed once, on first container start, before the database accepts
# any other connections — Alembic migrations that reference vector(N)
# can rely on the extension being present.
COPY docker/postgres/init/ /docker-entrypoint-initdb.d/
