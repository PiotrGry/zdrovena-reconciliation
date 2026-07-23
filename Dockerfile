FROM python:3.12-slim

WORKDIR /app

# Install deps BEFORE copying source — pip layer is cached until pyproject.toml changes
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir ".[api,cloud,ksef]"

# Copy source last — changes here don't invalidate the pip layer
COPY zdrovena/ zdrovena/
# The dependency layer above is built before the package sources exist, so it
# installs the console-script metadata but cannot install the `zdrovena`
# package itself. Uvicorn happens to import from WORKDIR, while the
# `/usr/local/bin/zdrovena` entrypoint used by Container App Jobs does not.
# Install the already-resolved local project without touching dependencies.
RUN pip install --no-cache-dir --no-deps .

ENV APP_ENV=prod
EXPOSE 8000

# Non-root user — principle of least privilege
RUN useradd -r -s /bin/false app && chown -R app:app /app
USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "zdrovena.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
