# strands-pg-agent: base image for any strands_pg-powered agent.
#
# Downstream agents stamp on top of this with `FROM ghcr.io/<owner>/strands-pg-agent`
# + `COPY prompts/ tools/ app.py`. No domain code or prompts in the base.
#
# Tags: ghcr.io/<owner>/strands-pg-agent:<version>

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libpq5 \
 && rm -rf /var/lib/apt/lists/*

# uv for fast installs; pinned to a known-good minor.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# Install strands-pg from the build context.
COPY pyproject.toml README.md /app/strands-pg/
COPY src/ /app/strands-pg/src/
COPY migrations/ /app/migrations/

RUN uv pip install --system --no-cache /app/strands-pg[bedrock]

# Entrypoint: apply migrations, then run the agent's app.
COPY images/agent/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV STRANDS_PG_MIGRATIONS_DIR=/app/migrations \
    PORT=8000

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
