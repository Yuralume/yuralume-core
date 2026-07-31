FROM golang:1.26-bookworm@sha256:1ecb7edf62a0408027bd5729dfd6b1b8766e578e8df93995b225dfd0944eb651 AS gosu-builder

RUN CGO_ENABLED=0 go install github.com/tianon/gosu@1.19

FROM pgvector/pgvector:pg16@sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb

# The upstream image's gosu binary was built with an older Go standard
# library. Rebuild the same privilege-drop helper from its tagged source using
# a patched Go toolchain, then keep the official entrypoint behavior unchanged.
COPY --from=gosu-builder /go/bin/gosu /usr/local/bin/gosu
RUN gosu --version

# Bake the extension init script in so deployments that pulled the
# image (instead of cloning the repo) don't need a bind-mount of the
# host's docker/postgres/init/ directory. Files in this directory are
# executed once, on first container start, before the database accepts
# any other connections — Alembic migrations that reference vector(N)
# can rely on the extension being present.
COPY docker/postgres/init/ /docker-entrypoint-initdb.d/
