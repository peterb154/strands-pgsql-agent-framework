# strands-pg

Postgres-backed primitives for [Strands](https://github.com/strands-agents/sdk-python) agents: session state, semantic memory, knowledge search, queues, spatial data, telemetry — one boring database doing everything.

## What this is

A reusable pattern for stamping out small, purpose-built Strands agents. Each agent gets:

- One Python process (Strands + FastAPI + `strands_pg`)
- One Postgres (with pgvector, PostGIS, pg_trgm, optionally pgai)
- Brought up with `docker compose up`
- Deployed to one Proxmox LXC (or anywhere Docker runs)

See `PLAN.md` for the full design. See `example/` for a runnable reference agent and the template to copy when stamping a new one.

## Quick start

```bash
cd example
docker compose up --build
# in another shell
curl -s localhost:8000/health
curl -s -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"session_id":"demo","message":"hello"}'
```

## Why

Running small purpose-built agents (one per domain — camping, finance, fitness, etc.) should be cheap to stand up. Instead of a polyglot stack (Redis + vector DB + queue + search + dashboards + bespoke API) per agent, `strands-pg` gives you one Postgres that does it all.

Inspired by the "Postgres for everything" thesis.

## Status

Phase 1 MVP: session + memory + `/chat`. See `PLAN.md` for the roadmap.

## License

MIT
