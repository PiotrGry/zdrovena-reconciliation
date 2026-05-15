FROM python:3.12-slim

WORKDIR /app

# Copy packaging metadata first — better layer caching
COPY pyproject.toml README.md ./

# Copy source
COPY zdrovena/ zdrovena/

# Install api + cloud + ksef extras (no dev/test dependencies)
RUN pip install --no-cache-dir -e ".[api,cloud,ksef]"

ENV APP_ENV=prod
EXPOSE 8000

CMD ["uvicorn", "zdrovena.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
