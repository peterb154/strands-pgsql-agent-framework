# strands-pg-db: postgres 17 with pgvector + postgis + pg_trgm.
#
# pgvector/pgvector:pg17 is multi-arch (amd64 + arm64) and already has pgvector
# built. We add PostGIS from the PGDG apt repo. pg_trgm ships with postgres.
# pgai is optional and left out of the base image; add it downstream if you
# want trigger-based embedding sync.
#
# Tags: ghcr.io/<owner>/strands-pg-db:<version>

FROM pgvector/pgvector:pg17

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        postgresql-17-postgis-3 \
        postgresql-17-postgis-3-scripts \
 && rm -rf /var/lib/apt/lists/*

# CREATE EXTENSION statements live in migrations/001_init.sql so the same path
# works for fresh + existing databases.
