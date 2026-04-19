# strands-pg

Postgres-backed primitives for building purpose-built Strands agents.

## What this is

A library, not a framework in the "inherit from `BaseAgent`" sense. You install it,
apply its migrations, and use the pieces you need. Your agent is still a regular
`strands.Agent` you build yourself.

It exists because a small purpose-built agent — the kind you write to solve one
problem well, not a general assistant — needs a handful of things beyond the LLM
and its tools:

- Conversation state that survives a restart
- Long-term memory the agent can search semantically
- System prompts and per-user context that shouldn't be baked into source
- A place for domain data the tools query

In a typical build, each of those lands in a different dependency: SQLite for
campsite records, a vector database for memory, a dotted folder for session
files, another for prompts, a bespoke FastAPI to tie it together. Three days of
plumbing before the agent does its first useful thing. Most of that plumbing is
the same from one agent to the next.

`strands-pg` puts all of it in one Postgres with pgvector, PostGIS, and pg_trgm,
and ships the Strands-specific glue that can't be expressed as a tool. The
thesis: one boring database with a few extensions is enough for a fleet of
small, narrow agents, and the wiring between it and Strands is worth writing
once.

Inference and embeddings go through **AWS Bedrock** by default — Claude
(Sonnet/Opus/Haiku) for the agent's reasoning, Titan Text Embeddings v2 for
memory vectors. Bedrock is the default because it keeps billing in one place
and avoids per-provider API key sprawl, but it's not load-bearing: pass any
Strands `Model` into `Agent(model=...)` and swap `PgMemoryStore(embedder=...)`
if you want OpenAI, Ollama, a local model, or anything else.

The three extensions, briefly:

- **pgvector** — adds a `vector` column type and similarity-search operators
  (cosine distance, L2, inner product) plus HNSW and IVFFlat index types for
  fast approximate nearest-neighbor queries. Used here for semantic memory:
  you store an embedding per row, then `ORDER BY embedding <=> query_vec`.
- **PostGIS** — adds `geometry` and `geography` column types and a large
  library of spatial functions and GIST indexes: `ST_MakePoint`, `ST_DWithin`
  (points within *N* meters), `ST_Distance`, and so on. The `camping-db/`
  example uses it for "campsites within 30 miles of these coordinates."
- **pg_trgm** — trigram indexing for fuzzy text: `LIKE '%foo%'` becomes fast,
  `similarity()` gives a score, typos get tolerated. Complements tsvector
  full-text search — tsvector is stemmed word matching, pg_trgm is
  character-level fuzziness.

## What's included

- `PgSessionManager` — a `SessionManager` subclass that persists conversations
  and agent state in Postgres JSONB. Pass it to `Agent(session_manager=...)` and
  history survives restarts.

- `PgMemoryStore` + `memory_tools(namespace=...)` — semantic memory on pgvector
  with HNSW indexing. The factory returns `[remember, recall]` closures bound
  to whatever namespace you pass (usually a session id or email), so every user
  gets an isolated memory bucket automatically.

- `PgPromptStore` — prompts live as rows in a `prompts` table. On first boot
  the store seeds itself from `./prompts/*.md`; after that the database is the
  source of truth, edited via API or SQL without rebuilding the image.

- `PgIdentity` — per-user profile documents keyed by slug, with a many-to-one
  email mapping (one user, multiple addresses). Typically loaded in your
  `build_agent()` and prepended to the system prompt.

- A migration runner (`strands-pg-migrate`) that applies numbered SQL files in
  order. Framework migrations occupy 001–099; your agent's start at 100. No
  ORM, no Alembic.

- `make_app(agent_factory)` — a FastAPI factory with `/health`, `/chat`, and
  optional `/prompts` endpoints. Convenience, not essence. Skip it if you have
  your own HTTP layer.

- `strands-pg-chat` — a small CLI that talks to `/chat` over HTTP. Useful for
  iterating on prompts and tools without building a frontend.

- Two Docker images: `strands-pg-db` (Postgres 17 + pgvector + PostGIS + pg_trgm)
  and `strands-pg-agent` (Python + Strands + this library + uvicorn, with an
  entrypoint that runs migrations on boot).

- An optional **PostgREST** sidecar pattern (shown in `camping-db/`). PostgREST
  is a standalone Haskell service that auto-generates a REST API from your
  database schema — filtered GET, JSON POST/PATCH/DELETE, OpenAPI spec, JWT
  auth — for the tables you explicitly grant to a scoped role. It's how
  `strands-pg` handles domain-data CRUD (`/camps`, `/parcel_services`, etc.)
  without reinventing a handler per table. `/chat` stays hand-written in
  FastAPI because PostgREST can't run an LLM; everything that's just tables
  delegates to PostgREST.

## A minimum working agent

```python
# app.py
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands_pg import PgSessionManager, make_app, memory_tools

def build_agent(session_id: str) -> Agent:
    return Agent(
        model=BedrockModel(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        system_prompt="You are a helpful assistant.",
        tools=memory_tools(namespace=session_id),
        session_manager=PgSessionManager(session_id=session_id),
    )

app = make_app(build_agent)
```

With `STRANDS_PG_DSN` pointing at a Postgres that has the framework migrations
applied, `uvicorn app:app` gives you an agent with per-user memory and durable
sessions at `POST /chat`. That's it.

## A realistic agent

Once you have domain data, the shape doesn't change much. You add migrations,
tools, prompt files, and a few identity profiles:

```text
my-agent/
├── app.py
├── prompts/
│   ├── soul.md               # seeded into DB on first boot
│   └── rules.md
├── identities/
│   └── brian.md              # YAML frontmatter + markdown body
├── migrations/
│   └── 100_orders.sql        # your domain tables
├── tools/
│   └── orders.py             # @tool search_orders, @tool create_order
├── Dockerfile
└── docker-compose.yml
```

`build_agent` pulls prompts and identity from the database, picks up the
per-session memory tools, and adds your domain tools:

```python
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands_pg import (
    PgIdentity, PgPromptStore, PgSessionManager,
    make_app, memory_tools,
)
from tools.orders import search_orders, create_order

prompts = PgPromptStore();    prompts.seed_from_dir("./prompts")
identities = PgIdentity();    identities.seed_from_dir("./identities")

def build_agent(session_id: str) -> Agent:
    system_prompt = prompts.assemble(["soul", "rules"])
    identity = identities.get_by_email(session_id)
    if identity:
        system_prompt += f"\n\n## USER CONTEXT\n{identity.body}"

    return Agent(
        model=BedrockModel(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        system_prompt=system_prompt,
        tools=[
            search_orders,
            create_order,
            *memory_tools(namespace=session_id),
        ],
        session_manager=PgSessionManager(session_id=session_id),
    )

app = make_app(build_agent, prompt_store=prompts)
```

Two worked examples live in the repo:

- `example/` — the minimum agent above, wrapped in a `docker-compose.yml` you
  can copy.
- `camping-db/` — a larger port of a real family-camping agent: 15,668 campsite
  records with PostGIS spatial search and tsvector full-text, two user
  identities mapped to multiple emails each, and five tools (`search_camps`,
  `get_campsite`, `geocode`, `land_ownership`, `parcel_lookup`). Runs on port
  8001 alongside `example/` on 8000, plus a PostgREST sidecar on 3000.

## Data APIs via PostgREST

`make_app` handles the *agent* endpoint (`/chat`) and some admin plumbing
(`/prompts`). Anything else — listing/filtering/editing rows in your domain
tables — is delegated to [PostgREST](https://postgrest.org), mounted as a
third Docker service that points at the same database:

```yaml
postgrest:
  image: postgrest/postgrest:v12.2.0
  environment:
    PGRST_DB_URI: postgres://strands:strands@db:5432/strands
    PGRST_DB_SCHEMAS: public
    PGRST_DB_ANON_ROLE: web_anon
  ports: ["3000:3000"]
```

A small migration grants `web_anon` SELECT on the tables you want browsable:

```sql
CREATE ROLE web_anon NOLOGIN;
GRANT USAGE ON SCHEMA public TO web_anon;
GRANT SELECT ON TABLE camps, parcel_services TO web_anon;
GRANT web_anon TO strands;
```

You now have:

```bash
# Filtered GET with PostgREST's query syntax:
curl 'localhost:3000/camps?state=eq.MT&type=eq.NF&limit=5&select=camp,town,lat,lon'

# Row count via the Prefer header:
curl -I -H 'Prefer: count=exact' 'localhost:3000/camps?state=eq.MT'
# Content-Range: 0-552/553

# OpenAPI spec:
curl localhost:3000/
```

Tables that aren't granted to `web_anon` stay invisible — `sessions`,
`session_messages`, `memories`, `identities`, and `prompts` never leak out
this surface. Skip the compose block entirely if you don't want an admin
API; the agent works the same either way.

## What it doesn't do

- **No horizontal scaling.** One Postgres per agent, one agent per container.
  If your agent outgrows a single Postgres, you've outgrown this library.
- **No cross-agent multi-tenancy.** Each agent has its own database. If two
  agents need to share data, that's an explicit choice you make.
- **No deployment layer.** Docker images are provided; how and where you run
  them is up to you. The author happens to run each agent in its own Proxmox
  LXC.
- **Not a replacement for Strands tools.** `memory_tools` is the only piece
  exposed as tools. Everything else is lifecycle/plumbing that couldn't be
  tools if it tried.
- **No agent-authoring DSL or no-code builder.** You write Python.

## Install and run

```bash
# In your agent repo:
pip install "strands-pg[bedrock]"   # brings boto3 for Bedrock model + Titan embeddings

# Apply framework + your agent's migrations against a running Postgres:
STRANDS_PG_DSN=postgresql://strands:strands@localhost:5432/strands \
  strands-pg-migrate --dir migrations

# Run:
uvicorn app:app --host 0.0.0.0 --port 8000

# Chat with it from the terminal:
strands-pg-chat --session-id you@example.com
```

If you want the whole stack up in one command, copy `example/docker-compose.yml`
into your repo — it brings up Postgres and the agent together, runs migrations
on boot, and exposes `/chat` on port 8000.

## Status

Pre-1.0. The primitives above work end-to-end and have been used to port a
real agent (`camping-db/`). The API will shift as more agents are built on it;
the `PgSessionManager` contract and the migration-numbering convention
(framework 001–099, agents 100+) are stable.

## License

MIT.
