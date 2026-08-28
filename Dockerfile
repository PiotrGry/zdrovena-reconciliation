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

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY zdrovena/ zdrovena/

ENV PATH="/app/.venv/bin:$PATH" \
    APP_ENV=prod
EXPOSE 8000

# Non-root user — principle of least privilege
RUN useradd -r -s /bin/false app && chown -R app:app /app
USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "zdrovena.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
