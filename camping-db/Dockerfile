# camping-db agent image. Build from the framework repo root so strands_pg
# source is in context. Once strands-pg-agent is published to ghcr, swap the
# first stage for `FROM ghcr.io/<owner>/strands-pg-agent:<version>` and drop
# the local pip install.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# 1) Install the framework from source (replace with ghcr pull once published).
COPY pyproject.toml README.md /tmp/strands-pg/
COPY src/ /tmp/strands-pg/src/
RUN uv pip install --system --no-cache /tmp/strands-pg[bedrock]

# 2) Framework-owned migrations first (sessions, memories, prompts).
COPY migrations/ /app/migrations/

# 3) Layer on camping-db migrations and domain code.
COPY camping-db/migrations/ /app/migrations/
COPY camping-db/app.py /app/app.py
COPY camping-db/prompts/ /app/prompts/
COPY camping-db/identities/ /app/identities/
COPY camping-db/tools/ /app/tools/
COPY camping-db/scripts/ /app/scripts/

# 4) Shared entrypoint (waits for PG + runs migrations).
COPY images/agent/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV STRANDS_PG_MIGRATIONS_DIR=/app/migrations \
    PORT=8000

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
