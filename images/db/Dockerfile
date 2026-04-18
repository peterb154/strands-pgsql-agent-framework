# strands-pg-db: postgres with pgvector + postgis + pg_trgm.
#
# pg_trgm ships built-in with postgres. We install pgvector and postgis on top
# of the postgis image (which already bundles PostGIS + dev chain). pgai is
# optional and left out of the base image; add it in a downstream Dockerfile
# if you want trigger-based embedding sync.
#
# Tags: ghcr.io/<owner>/strands-pg-db:<version>

FROM postgis/postgis:17-3.5

ARG PGVECTOR_VERSION=v0.8.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        postgresql-server-dev-17 \
        ca-certificates \
 && git clone --branch ${PGVECTOR_VERSION} --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector \
 && make -C /tmp/pgvector \
 && make -C /tmp/pgvector install \
 && rm -rf /tmp/pgvector \
 && apt-get purge -y --auto-remove build-essential git postgresql-server-dev-17 \
 && rm -rf /var/lib/apt/lists/*

# CREATE EXTENSION statements still live in migrations/001_init.sql — kept out
# of /docker-entrypoint-initdb.d/ on purpose so the same migrations path works
# for fresh + existing databases.
