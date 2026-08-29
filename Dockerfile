# ── Builder: resolve from uv.lock, not from a fresh pip resolution ───────────
FROM python:3.12-slim AS builder

# uv reads uv.lock; pip does not. Before this, every image build resolved
# dependencies afresh and the lockfile was never even copied into the build
# context, so what shipped was not what CI tested (issue #278).
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies before sources — this layer caches until pyproject/uv.lock change.
# --locked, not --frozen: a lockfile that no longer matches pyproject.toml must
# fail the build rather than silently install something else.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --no-dev \
        --extra api --extra cloud --extra ksef

# Sources last — changes here do not invalidate the dependency layer. The second
# sync installs the project itself, which is what puts the `zdrovena` console
# entrypoint used by Container App Jobs on the path.
COPY zdrovena/ zdrovena/
RUN uv sync --locked --no-editable --no-dev \
        --extra api --extra cloud --extra ksef

# ── Final: the environment only, no uv, no build tooling ─────────────────────
FROM python:3.12-slim

# The user is created BEFORE anything is copied, so ownership is set by COPY
# --chown as each layer is written. A trailing `chown -R app:app /app` instead
# rewrites every file it touches into a new layer: it cost 107 MB of a 318 MB
# image, a second full copy of the virtualenv (issue #238).
RUN useradd -r -s /bin/false app
WORKDIR /app
RUN chown app:app /app

# No `COPY zdrovena/` here on purpose. `uv sync --no-editable` in the builder
# installed the package into the virtualenv, and a copy at /app/zdrovena would
# SHADOW it — WORKDIR is on sys.path, so the shipped-and-verified artifact
# would not be the code that runs.
COPY --from=builder --chown=app:app /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    APP_ENV=prod
EXPOSE 8000

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "zdrovena.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
