# strands-pg — Plan

A reusable pattern for building small, purpose-built Strands agents where a single co-located Postgres instance (Docker Compose, per agent) handles everything: session state, semantic memory, knowledge/RAG, job queues, spatial data, telemetry, per-user isolation, and the API layer.

Inspired by the "Postgres for everything" thesis: instead of stitching Redis + vector DB + queue + search + dashboards + API servers, use one boring 30-year-old database with extensions.

## Why this repo exists

The user runs small purpose-built agents on Strands (e.g., `../camping-db` — a family camping assistant). Each new agent currently needs its own SQLite + memweave memory dir + Strands `FileSessionManager` dir + bespoke FastAPI + LXC container. Setup cost per agent is too high. This framework cuts the plumbing to: clone the template, edit prompts, `docker compose up`.

Previous approach (general-purpose agent on OpenClaw) was too expensive to run. Pivot: lots of small purpose-built agents, each cheap.

## Locked decisions

1. **Postgres co-located per-agent, not shared.** Each agent LXC runs its own Postgres. Self-contained. No blast radius across agents. Matches user's existing Mealie CT-100 pattern.
2. **LXC-per-agent on Proxmox.** Each agent gets its own LXC. Inside the LXC: `docker compose up` brings up `agent` + `db`. One compose file per LXC.
3. **Docker, not raw LXC installs.** LXC becomes the thin host. All dependencies captured in Dockerfiles. Solves the "we keep `apt install`-ing things without tracking it" drift problem the user flagged.
4. **Strands `SessionManager` ABC is the plug-in point.** Verified by reading `strands-agents/sdk-python/src/strands/session/session_manager.py`. It's `HookProvider + ABC` with `@abstractmethod`s: `append_message`, `redact_latest_message`, `sync_agent`, `initialize`, plus multi-agent and bidi variants. Existing impls: `FileSessionManager`, `S3SessionManager`, `RepositorySessionManager`. We implement `PgSessionManager(SessionManager)` — idiomatic, no shims.
5. **Proof-of-pattern: ship framework + migrate `camping-db` in parallel.** Don't build primitives in a vacuum. camping-db's real load decides which primitives are actually needed vs. imagined.
6. **RLS deferred.** Under decision #1, agent isolation is at the OS/network boundary, not a DB role. RLS is only for intra-agent per-user rows (camping-db's multi-identity case). Wire it when an agent needs it.
7. **Migrations: raw SQL files + tiny runner.** Numbered `001_init.sql`, `002_*.sql`. No alembic. If we outgrow it we'll know.
8. **Embeddings: try `pgai-vectorizer` first.** Timescale's `pgai` manages embedding sync via DB triggers — simpler than rolling our own. Fall back to custom embedding logic in `PgMemoryStore` if it doesn't fit.
9. **Scaffolding base: `strands-agents/extension-template-python`.** Copy its `pyproject.toml` / `src/` layout as starting point for the `strands-pg` package.
10. **Two Docker images to publish:**
    - `strands-pg-agent` — Python + Strands + `strands_pg` + FastAPI + uvicorn + common tools (web search, calc, time, python-exec)
    - `strands-pg-db` — `postgres:17` + `pgvector` + `postgis` + `pg_trgm` (pg_trgm ships built-in) + optionally pgai

## Research findings (2026-04-18)

- **No prior art on Strands.** No `strands-pg*` on PyPI. No postgres-specific repo in `strands-agents` org (12 repos: `sdk-python`, `tools`, `samples`, `agent-builder`, `agent-sop`, `evals`, `mcp-server`, `extension-template-python`, `sdk-typescript`, `devtools`, `docs`, `.github`).
- **Supabase sample is NOT prior art.** `strands-agents/samples/python/03-integrate/databases/supabase` wires up `supabase-mcp-server` as an MCP tool — the agent talks to Supabase as a client. Does not replace session/memory. Different in kind.
- **pgai is complementary.** `timescale/pgai` is a Postgres extension + Python vectorizer + examples. Not an agent framework. We can use its vectorizer.
- **No generic "stamp out a purpose-built agent with PG-for-everything" template exists.** Dify/Flowise/AnythingLLM are chat platforms, not stamping frameworks. Individual pgvector adapters abound, but not the whole-pattern approach.
- Verdict: **build it.**

## Repo layout (proposed)

```
strands-pgsql-agent-framework/
├── README.md
├── PLAN.md                     # this file
├── pyproject.toml              # package: strands-pg
├── src/strands_pg/
│   ├── __init__.py
│   ├── session.py              # PgSessionManager(SessionManager)
│   ├── memory.py               # PgMemoryStore (pgvector + HNSW; try pgai-vectorizer)
│   ├── api.py                  # FastAPI factory: /health /chat /chat/stream
│   ├── migrate.py              # raw SQL migration runner
│   └── (phase-2) knowledge.py, queue.py, spatial.py, telemetry.py, identity.py
├── migrations/
│   └── 001_init.sql            # baseline: extensions + sessions + memory tables
├── images/
│   ├── agent/Dockerfile        # → ghcr.io/<owner>/strands-pg-agent
│   └── db/Dockerfile           # → ghcr.io/<owner>/strands-pg-db
├── example/                    # reference agent AND the "stamp this" template
│   ├── docker-compose.yml      # agent + db
│   ├── Dockerfile              # FROM strands-pg-agent + COPY prompts
│   ├── prompts/
│   │   ├── soul.md             # who the agent is
│   │   └── rules.md            # how it behaves
│   ├── tools/                  # domain-specific tools (empty to start)
│   └── app.py                  # Strands agent wired to strands_pg
├── tests/
└── .devcontainer/              # uv + ruff + pgvector-enabled postgres for dev
```

## Phase 1 MVP — thin end-to-end slice

Goal: `docker compose up` → working "ask me anything" agent with persistent session + semantic memory across restarts.

1. **`strands-pg-db` image** (`images/db/Dockerfile`): `FROM postgres:17` + install `pgvector`, `postgis`, `pgai` extensions. `pg_trgm` ships built-in.
2. **`migrations/001_init.sql`**: `CREATE EXTENSION` for all of the above; tables for `sessions` (JSONB messages + agent state) and `memory` (pgvector column + text).
3. **`strands-pg-agent` image** (`images/agent/Dockerfile`): `FROM python:3.13-slim` + `uv` + install `strands` + `strands_pg` + `fastapi` + `uvicorn`. Entrypoint runs the example app.
4. **`PgSessionManager`**: subclass `SessionManager`, implement `append_message`, `redact_latest_message`, `sync_agent`, `initialize`. Messages → JSONB rows keyed by session_id. Agent state → JSONB rows.
5. **`PgMemoryStore`**: `add(text, metadata)` → compute embedding (try `pgai-vectorizer` trigger-based first), insert row. `search(query, k=5)` → HNSW KNN.
6. **`api.py` FastAPI factory**: `make_app(agent) -> FastAPI` with `/health` and `/chat` (POST `{message, session_id}` → `{response}`). Non-streaming v1. SSE deferred to phase 2.
7. **`example/app.py`**: Strands agent with `PgSessionManager` + memory tool that calls `PgMemoryStore`. Basic "ask me anything" behavior.
8. **`example/docker-compose.yml`**: two services (`agent`, `db`) on a bridge network, volumes for `db` data.
9. **End-to-end test**: `docker compose up` → POST to `/chat` → response. Restart. POST again referring to prior conversation → agent recalls via session. Store a memory. Restart. Search memory → hit.

## Phase 2 (deferred)

- `PgKnowledge` (tsvector + pg_trgm for FTS + fuzzy)
- `PgQueue` (`FOR UPDATE SKIP LOCKED` for background jobs)
- `PgSpatial` (PostGIS — camping-db needs this)
- `PgTelemetry` (partitioned tables + BRIN for event/trace logs)
- `PgIdentity` (RLS + per-user rows — camping-db's multi-identity case)
- SSE streaming for `/chat/stream`
- PostgREST or `pg_graphql` for auto-API
- `camping-db` migration onto the framework (validates everything on real load)

## Tooling & conventions

- Python: `uv` for deps, `ruff` for lint+format, `pytest` for tests.
- `UV_EXCLUDE_NEWER` set to 2 days ago in devcontainer (supply-chain buffer).
- Conventional commits. Branches: `feat/N-short-desc`, `fix/N-short-desc`, etc.
- Prompts convention (from camping-db): `prompts/soul.md` + `prompts/rules.md` mounted into the container.
- No git yet — first session task: `git init`, initial commit of PLAN.md + layout, push to GitHub under `brianpeterson/strands-pgsql-agent-framework`.

## Reference agents

- `../camping-db` — the pressure-test. Has 14 tools, spatial search, per-user memory via memweave, file-based sessions, identity files, parcel service registry. Phase-2 migration target.
- `../local_network` — Proxmox setup. CT-100 Mealie shows the "PG-next-to-app" pattern already works.

## Open questions for Phase 1

- **Exact embedding model** for `PgMemoryStore`. pgai supports several (OpenAI, Ollama, etc.). Default to Bedrock Titan embeddings? Local Ollama? Env-configurable with a sensible default.
- **Session_id provenance.** Client-provided (like camping-db today — keyed off email) or server-issued? Default to client-provided with server fallback on missing.
- **Model provider.** Camping-db uses Bedrock Claude Sonnet 4.6. Keep that as default; make swappable via env.

## Not doing (explicit no's)

- Not building a chat platform (Dify/AnythingLLM territory).
- Not building a no-code builder.
- Not targeting horizontal scale — if an agent outgrows one Postgres, it outgrows this framework.
- Not inventing a new migration system — raw SQL + a runner until we hit a wall.
- Not supporting multi-tenancy across agents — that's what LXC boundaries are for.
