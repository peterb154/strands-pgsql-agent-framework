# my-agent

A strands-pg agent. Stamped from
[strands-pgsql-agent-framework](https://github.com/peterb154/strands-pgsql-agent-framework)
— you own every file in this directory.

## Quickstart

```bash
cp .env.example .env
# edit AWS_PROFILE to one with Bedrock access (SSO-backed is fine)

docker compose up --build
# agent on :8000, Postgres on :5432 (internal)

# in another shell:
curl -s localhost:8000/health
curl -s -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"session_id":"you@example.com","message":"hello"}'
```

## What's here

- **`app.py`** — where you build your Strands `Agent`. This is the main
  extension point; read it top to bottom.
- **`strands_pg/`** — vendored framework code. You own it. Bugfix upstream?
  Patch it here, send a PR if you feel like it.
- **`migrations/`** — framework migrations `001-099`, plus your own `100+`.
  Applied in order on boot by `python -m strands_pg.migrate`.
- **`tools/`** — where your domain tools go. Strands `@tool`-decorated
  functions. Import them in `app.py` and add to `tools=[...]`.
- **`prompts/`** — `soul.md` + `rules.md` get seeded into the DB on first
  boot. Edit there or live via `PUT /prompts/{name}`.
- **`Dockerfile`**, **`docker-compose.yml`**, **`entrypoint.sh`** — your
  container stack. Edit freely.
- **`db/Dockerfile`** — Postgres 17 + pgvector + PostGIS.

## Adding a domain tool

```python
# tools/orders.py
from strands import tool
from strands_pg._pool import get_pool

@tool
def search_orders(customer: str) -> str:
    """Look up orders for a given customer."""
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, total FROM orders WHERE customer = %s", (customer,))
        return "\n".join(f"#{r[0]}: ${r[1]}" for r in cur.fetchall())
```

```python
# app.py
from tools.orders import search_orders

# in build_agent:
tools=[search_orders, *memory_tools(namespace=session_id)],
```

## Adding a domain table

Add `migrations/100_orders.sql` (framework uses 001-099, agents start at 100):

```sql
CREATE TABLE IF NOT EXISTS orders (
    id       BIGSERIAL PRIMARY KEY,
    customer TEXT NOT NULL,
    total    NUMERIC NOT NULL,
    ...
);
```

`docker compose up --build` reapplies migrations on boot.

## Updating from upstream

Re-run the installer into a scratch directory and `diff -r` against this one:

```bash
bash install.sh /tmp/fresh-stamp --ref v0.2.0
diff -r /tmp/fresh-stamp .
```

Pick what you want, patch what you don't. The code is yours.
