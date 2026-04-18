# Example agent image. Build from the repo root so the strands_pg source is in
# context:
#
#   docker build -f example/Dockerfile -t strands-pg-example .
#
# In production you'd swap the local build for
#   FROM ghcr.io/<owner>/strands-pg-agent:<version>

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# Install strands-pg from the build context.
COPY pyproject.toml README.md /app/strands-pg/
COPY src/ /app/strands-pg/src/
RUN uv pip install --system --no-cache /app/strands-pg[bedrock]

# Migrations + example app code.
COPY migrations/ /app/migrations/
COPY example/app.py /app/app.py
COPY example/prompts/ /app/prompts/
COPY images/agent/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV STRANDS_PG_MIGRATIONS_DIR=/app/migrations \
    PORT=8000

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
