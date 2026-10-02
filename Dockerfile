# Postgame — one container serving both the site and the API.
#
# Decision 0.9 puts the frontend and the API on one origin, so both go in here: `api/` runs
# the server and `postgame/` is the static root it serves from. That is why the build context
# is the repository root rather than `api/`.
#
# Build and run:
#
#   docker build -t postgame .
#   docker run --env-file api/.env.production -p 8132:8132 postgame
#
# Release command (run before the new container takes traffic):
#
#   docker run --rm --env PG_MIGRATE_DATABASE_URL=... postgame alembic upgrade head
#
# Notes on the choices here, since a Dockerfile is mostly choices:
#
#   * two stages, so the compiler toolchain needed to build any wheel that has no binary
#     distribution never reaches the running image;
#   * install from `requirements.lock` with `--no-deps`, not from `pyproject.toml`. The
#     lock is the whole transitive closure at exact versions, and `--no-deps` means pip
#     cannot quietly resolve something that is not in it. Two builds of one commit then
#     install the same versions, which is the entire point of committing a lock;
#   * a non-root user. Nothing here needs to write to the filesystem, and a container
#     process that cannot modify its own code is one fewer thing an upload bug can become;
#   * the application is not `pip install`ed. It is copied and run from its directory, so
#     there is no editable install, no `.egg-info`, and no second copy of the source.

# ---------------------------------------------------------------------------- build stage
FROM python:3.13-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# build-essential for any dependency without a wheel for this platform. It stays in this
# stage. libpq is deliberately absent: `psycopg[binary]` is in the lock and carries its own.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY api/requirements.lock /tmp/requirements.lock
# --no-deps: the lock is the closure, so pip has nothing left to decide. If an entry is
# missing the build fails here rather than resolving something nobody reviewed.
RUN pip install --no-deps -r /tmp/requirements.lock

# ----------------------------------------------------------------------------- run stage
FROM python:3.13-slim AS run

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH" \
    PG_HOST=0.0.0.0 \
    PG_PORT=8132 \
    PG_FRONTEND_DIR=/app/postgame

COPY --from=build /venv /venv

# A fixed uid so a mounted volume has predictable ownership, and no login shell.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin postgame

WORKDIR /app

# Ownership is root and the process runs as `postgame`, so the application cannot rewrite
# its own code. `.dockerignore` is what keeps `.env`, `.venv/`, the planning documents and
# `scratchpad/` out of the image; the static server's extension allowlist is the second
# layer, and neither is relied on alone.
COPY api/app       /app/api/app
COPY api/migrations /app/api/migrations
COPY api/alembic.ini /app/api/alembic.ini
COPY api/tools     /app/api/tools
COPY postgame      /app/postgame

USER postgame
WORKDIR /app/api

EXPOSE 8132

# `python -m app.server`, not a bare `uvicorn` command line: server.py is where the hardened
# options live — `server_header=False`, the h11 header-size cap, the keep-alive and
# concurrency limits, and the refusal to trust forwarded headers without a proxy list.
# Invoking uvicorn directly would silently drop all of them.
CMD ["python", "-m", "app.server"]

# The platform's own health check should be preferred where there is one; this is for a
# plain `docker run`. Written in Python because the slim image has no curl, and pointed at
# $PORT so it still works where the platform assigns one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT',os.environ.get('PG_PORT','8132'))+'/api/health',timeout=4).read()"]
