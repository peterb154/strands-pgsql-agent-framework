# CHANGELOG


## v0.6.0 (2026-04-21)

### Features

- Multi-scope memory_tools, per-request auth_verifier, commit_sha helper
  ([`3ca218c`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/3ca218c797302669a444041ad701dfc4f7ef3bc8))

Three additive capabilities for agents that serve real users over HTTP:

1. memory_tools(namespaces={suffix: ns, ...}) — builds parallel remember_<suffix> / recall_<suffix>
  tool pairs, each closed over its own namespace. Enables per-user + per-household (or per-org)
  memory scopes from a single agent build. Single-namespace form (namespace=...) is unchanged and
  still returns [remember, recall].

2. make_app(auth_verifier=...) — opt-in per-request auth for /chat and /chat/stream. Callable
  (token) -> {session_id, ...} | None; a 401 is returned on missing/invalid tokens. When configured,
  session_id is derived from the verifier dict (not the request body) so clients can't spoof another
  user's session. The full context dict is passed to agent_factory(session_id, context=ctx) IF the
  factory accepts a `context` kwarg (detected via inspect) — keeps existing camping-db factories
  working unchanged.

3. commit_sha() + make_app(health_info=...) — /health can advertise the deployed revision without
  shelling out. Reads .git/HEAD (supports worktrees + packed-refs). Pair with health_info=lambda:
  {"commit": commit_sha()} so deploy pipelines (n8n, GHA) can verify a push actually landed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>


## v0.5.0 (2026-04-21)

### Features

- Host-side deploy + optional AgentMail webhook helper
  ([`435c932`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/435c9327c11fe1e2d75e08921f430dd00fb72426))

Two optional framework additions earned from the camping-db migration. Both off by default — agents
  opt in.

1. Host-side deploy orchestration (make_app(deploy=True))

POST /api/deploy now ships as an opt-in feature of make_app. When enabled, the endpoint: - Auths a
  bearer token from DEPLOY_TOKEN - Writes a timestamp to $DEPLOY_TRIGGER - Returns {"status": "ok",
  "action": "triggered", ...} — shape that matches typical IF-node success checks in workflow tools
  like n8n.

The orchestration itself runs on the LXC HOST via systemd, not inside the agent container.
  templates/agent/systemd/*.in holds two unit templates (*.path + *.service) with @AGENT@ and @DIR@
  placeholders; bootstrap-lxc.sh substitutes at install time so each agent gets distinct unit names
  (camping-db-deploy.path vs mealie-deploy.path can coexist). deploy.sh on the host does git pull +
  docker compose up --build and survives the rebuild (in-container orchestrators don't).

This graduates the pattern from camping-db, replacing the earlier in-container deploy that fought
  SIGTERM races, docker.sock security concerns, and docker-compose-plugin packaging issues.

2. AgentMail webhook helper (strands_pg.agentmail)

New module with attach_email_webhook() + make_agentmail_mcp() for agents that want to serve email.

attach_email_webhook(app, build_agent, known_emails, ...) registers POST /api/webhook/email with all
  the gates that would otherwise be copy-pasted into every email agent: - Accepts message.received,
  .spam, .blocked variants (startswith) - Sender allowlist via dynamic known_emails() callable -
  Echo-loop prevention (skip messages from our own address) - message_id dedup - Background-thread
  processing so the webhook returns fast - System-prompt injection telling the model to call
  reply_to_message (without this, agents generate replies that never get sent)

make_agentmail_mcp() is a tiny factory that opens the MCPClient with the x-api-key auth header
  AgentMail actually wants (not Bearer — empirically verified, 401 without).

Intentionally not exported from __init__.py — keeps the opt-in discipline, so agents that don't
  email don't pay the MCP import cost.

README: - New "Deploy architecture" section with the host-side diagram. - New "Email agents" section
  with the wiring recipe + gotchas (x-api-key not Bearer, SPF/DKIM/DMARC prerequisite, must call
  reply_to_message). - Anti-pattern noted: don't mount /var/run/docker.sock into the agent
  container.

Next: update camping-db to use the new helpers (removes ~70 lines of email-webhook boilerplate + ~40
  lines of deploy wiring from its app.py).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>


## v0.4.0 (2026-04-20)

### Features

- **install**: --force flag to stamp into existing directories
  ([`7bf0a91`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/7bf0a913060ed938fd3d90b418ed60f2bbc92e10))

install.sh now accepts --force (or -f) to overlay the framework stamp into an existing directory.
  Without the flag, the existing safety check still refuses (so nobody nukes an unrelated dir by
  accident).

Use cases: - Re-stamp an existing agent to pick up framework updates, then diff against working tree
  to pick which changes to keep (the "shadcn update" flow the README already describes). - Migrate
  an existing repo onto strands-pg — stamp files overlay the repo's working tree without touching
  .git, existing docs, or unrelated files. How camping-db's rewrite lands on its existing git
  history.

Overlay semantics: cp -R with target already existing. Same-name files get overwritten (soul.md,
  app.py, Dockerfile, etc.); files that aren't part of the stamp (README.md, CHANGELOG.md, .git,
  existing data/ dirs) are left alone. No rm -rf anywhere.

Verified: bash install.sh /tmp/force-test --force --ref main with a pre-existing keep-me.txt file
  left keep-me.txt intact and stamped all expected files.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>


## v0.3.0 (2026-04-20)

### Features

- Stamped bootstrap-lxc.sh + chromium sidecar stanza
  ([`6c4a7bd`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/6c4a7bd1ec9bd2deb99cb1b1e453b496ec6a322c))

Two additions to the stamped agent template:

templates/agent/bootstrap-lxc.sh: Idempotent host prep for a fresh Debian/Ubuntu LXC. Installs
  Docker engine + compose plugin via get.docker.com, writes daemon.json with log rotation, installs
  baseline tools (git, curl, jq), and adds a systemd unit that auto-starts any
  /opt/*/docker-compose.yml stack on reboot. Includes a preflight check that warns if nesting=1 /
  keyctl=1 features are missing on a Proxmox LXC (Docker won't work without them).

The script is stamped INTO the agent repo (not hosted at strands-pg's main) so each agent owns it.
  When a specific agent needs unusual host setup (kernel module, sysctl, extra packages), edit this
  file in place and commit — it's a Dockerfile-equivalent for the LXC.

templates/agent/docker-compose.yml: Adds a commented-out chromium sidecar stanza
  (browserless/chrome) with usage notes. Agents that need a browser tool for scraping/automation
  (camping-db's GIS workflow, for example) uncomment and go. Comes with shm_size 2gb —
  non-negotiable for headless Chrome.

templates/agent/README.md: New "First time on a fresh host?" section pointing at bootstrap-lxc.sh.

templates/agent/requirements.txt: (already updated in the SSE commit) — sse-starlette pulled in.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>


## v0.2.0 (2026-04-20)

### Features

- Sse streaming /chat/stream endpoint in make_app
  ([`40dc9f4`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/40dc9f49b759b773daeffc55713b117818986f53))

Adds a second chat endpoint alongside /chat that streams Strands agent events as Server-Sent Events.
  Wraps agent.stream_async into normalized event shapes so SSE consumers stay stable across SDK
  upgrades:

event: text text delta chunks

event: thinking reasoningText deltas (when model emits them)

event: tool_use tool name, emitted once per toolUseId

event: done terminal (empty data)

event: error any exception, data = message

Dependencies: - sse-starlette>=2.1 added to framework pyproject + template requirements.txt

Also restores pyproject.toml authors to "Brian Peterson" — was incorrectly scrubbed to "Alice Rider"
  in the earlier PII cleanup pass. The alice/bob personas were intended only for the camping-db/
  walkthrough identities, not the package metadata (git commit authorship is elsewhere and was
  deliberately left alone per user call).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>


## v0.1.3 (2026-04-20)

### Bug Fixes

- Restore over-scrubbed search example + correct User-Agent URL
  ([`29adbca`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/29adbca9abb7667a3eeb8bc1146864cd675a3907))

Two small cleanups after the PII scrub rewrite:

- camping-db/tools/geo.py: the geocode docstring example read "Mountain West KS" after the scrub
  (string replacement accidentally rewrote a city name inside a search example). Fixed to "Topeka
  KS". - camping-db/tools/{geo,parcels}.py: User-Agent string pointed at
  github.com/brianpeterson/strands-pgsql-agent-framework (a placeholder that was wrong from day one
  — the real repo is peterb154/...). Fixed to the actual URL.

Neither of these is PII. Just cleanup from the rewrite pass.


## v0.1.2 (2026-04-19)

### Bug Fixes

- **templates**: Deploy-to-production guide with IAM least-privilege policy
  ([`8a80181`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/8a80181ca0d2104e8393398254b12915c0159243))

The stamped agent's README had no guidance on what real deployment looks like. This adds a
  "Deploying to production" section covering:

1. IAM user + least-privilege policy JSON — scoped to bedrock:InvokeModel on just the Claude
  inference profile, the underlying foundation model, and the Titan embed model. Calls out the
  inference-profile-vs-FM-ARN gotcha explicitly (you need both, not just one). 2. The .env entries
  for static IAM user keys. 3. The ~/.aws mount that needs to be deleted from docker-compose.yml
  (the bind fails on hosts without ~/.aws, which is every fresh LXC). 4. A note on rotating the
  access key.

Complements the existing .env.example Option A (laptop dev with SSO-backed profile) vs Option B
  (production with static keys) distinction.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>


## v0.1.1 (2026-04-19)

### Bug Fixes

- **templates**: .env.example shows both SSO profile and static IAM key paths
  ([`272c7ba`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/272c7bab2df7eded0f8d05487e104201e28d6ebc))

Previous template only showed AWS_PROFILE=default, which works on a laptop with SSO but fails the
  moment someone deploys to a VPS or fresh LXC that has no ~/.aws. Adds a second documented pattern
  for static IAM user keys with guidance on scoping the user narrowly to bedrock:InvokeModel.

docker-compose.yml now calls out that the ~/.aws bind mount is only needed for the profile-based
  pattern and should be deleted when using static keys (the mount fails on hosts without ~/.aws).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Chores

- **ci**: Bump actions/checkout v4 -> v6
  ([`f9313b4`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/f9313b45aa4f3c205e4bbb9a65ff8d9155f7905e))

Silences the Node.js 20 deprecation warning surfaced in the first workflow run. v6 runs on Node.js
  24. Same interface we use (fetch-depth, token).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Continuous Integration

- Semantic-release workflow + credit source talk in README
  ([`0734f1a`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/0734f1ae35de862115278d79ee1a97aad5c07a46))

Two things:

1. README now credits the "Postgres for everything" talk
  (https://www.youtube.com/watch?v=TdondBmyNXc) inline in the thesis paragraph. It's where the
  one-database-replaces-the-stack argument came from and the reader should watch it for the fuller
  pitch.

2. GitHub Actions workflow that uses python-semantic-release to auto-tag on merges to main, driven
  by Conventional Commits: - feat: -> minor version bump - fix:, perf: -> patch - feat! / BREAKING:
  -> major - docs:, ci:, chore: -> no release No PyPI publish (source-distributed via install.sh).
  Workflow only creates a tag + GitHub Release with autogenerated notes.

pyproject.toml has the semantic-release config pointing at both project.version and
  src/strands_pg/__init__.py:__version__ so they stay in sync. Release commit message is tagged
  [skip ci] so the bump commit semantic-release pushes back doesn't retrigger the workflow.

This commit is `ci:` + `docs:` so it won't trigger a release — the first real release will cut on
  the next feat/fix merge.


## v0.1.0 (2026-04-19)

### Bug Fixes

- Pgvector binding + compose env + backlog deploy webhook
  ([`996b0cf`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/996b0cfd372500802475201c9bb595e12e102915))

- memory.py: cast %s::vector explicitly so plain Python list embeddings bind correctly.
  register_vector adapts numpy arrays (and Vector objects) but not lists, which landed as double
  precision[] and broke <=> queries with "operator does not exist: vector <=> double precision[]". -
  _pool.py: register_vector via pool configure= so every pooled connection gets the adapter (for
  reading vectors back as arrays). - example/docker-compose.yml: switch to env_file so host
  AWS_PROFIE exports don't leak in via ${...} substitution. - PLAN.md: add deploy webhook to Phase 2
  backlog — POST /deploy HMAC-verified endpoint for on-commit redeploy.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **camping-db**: Isolate agent tables in api schema, clean up OpenAPI
  ([`db3c5b6`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/db3c5b69d048971028d676859fc2ecbdfacd55c8))

Before: PostgREST pointed at public -> OpenAPI listed ~250 unreachable /rpc/st_* entries from
  PostGIS alongside our 2 real tables.

After: camps + parcel_services live in api schema; PostgREST exposes only that schema. OpenAPI now
  lists exactly camps, parcel_services, and the schema root. 0 rpc entries.

migration 103_api_schema.sql: - CREATE SCHEMA api - ALTER TABLE ... SET SCHEMA api for camps +
  parcel_services (grants and indexes travel with the tables) - ALTER ROLE strands SET search_path =
  api, public so existing tool SQL (FROM camps) keeps resolving without qualifying - GRANT USAGE ON
  SCHEMA api TO web_anon - NOTIFY pgrst to bust the schema cache for the running PostgREST container
  - Event trigger on ddl_command_end that re-fires NOTIFY on every future DDL, so new migrations
  don't require a manual reload

docker-compose.yml: - PGRST_DB_SCHEMAS: api (was public) - PGRST_DB_EXTRA_SEARCH_PATH: api, public
  (so PostgREST sees PostGIS funcs via search_path even though it only exposes api) - ~/.aws mounted
  rw so boto3 can refresh SSO cache (was ro, which broke Bedrock once the first token expired)

mermaid: fix ":3000 data" -> ":3000 /{table}" so it's parallel with

":8000 /chat" and actually describes PostgREST's URL shape.

README: new "Keep PostGIS out of your OpenAPI doc" subsection with the full migration + compose env
  pattern, documented as the standard approach for any agent that exposes tables via PostgREST.

verified end-to-end: - curl localhost:3000/ -> paths: ['/', '/camps', '/parcel_services'], defs:
  ['camps', 'parcel_services'], rpc entries: 0.

- Prefer: count=exact still returns 553 MT rows. - Agent's search_camps tool still works (found 3
  free NF MT camps) — search_path migration handled it transparently.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Documentation

- Acknowledge AgentCore as the preferred AWS-native path
  ([`2740dd6`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/2740dd6ba5640291dd9f9651771deba9f68a0ad5))

Adds a callout up front: if you're running in AWS, use Amazon Bedrock AgentCore. This library is for
  the cases AgentCore doesn't cover — self-hosted VPC, Proxmox LXC, homelab, laptop, anywhere off
  the AWS managed runtime. Bedrock is still used for inference/embeddings even when self-hosted; you
  just own the rest of the stack.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Add mermaid diagrams for runtime topology + /chat sequence
  ([`38d45f7`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/38d45f7cf8730cbf0c84dbb6ba2bbb8409e35be9))

Two diagrams near the top of the README:

1. flowchart showing the three containers (agent + db + optional postgrest) plus external Bedrock,
  with the ports a client hits on each.

2. sequenceDiagram for a POST /chat — build_agent reads prompts and identity from PG,
  PgSessionManager loads history, model calls a tool that hits PG, result synthesized, exchange
  persisted as JSONB.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Brief "what's Strands?" aside under the tagline
  ([`302e556`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/302e556e013003d22978ddfcf192e140b82122fa))

A reader landing on the README sees "Strands agents" in the tagline but has to guess what Strands
  is. Adds a two-sentence blockquote with the link to strands-agents/sdk-python, names the thing
  (AWS's open-source Python SDK), and explains the quality we actually value (extremely lean, no
  DSL, no base-class inheritance) — which is the reason this library's shape fits with it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Clarify the lead paragraph
  ([`3859610`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/38596102890dea834fe1281f16722b97c1f4c5ff))

Replace the "library, not a framework in the 'inherit from BaseAgent' sense" phrasing (which assumes
  the reader already knows that shorthand) with a plain description: it's a Python library, you
  import pieces and call them, there's no base class and no runtime to slot into, your agent stays a
  regular strands.Agent you build yourself.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Decouple the primitives from the "small agent" framing
  ([`8f86692`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/8f866927075d775ef6d85a54e054ee014df1d098))

The library's primitives (session manager, memory, prompts, identity) are useful for any Strands
  agent regardless of scope. Only the deployment pattern (one Postgres per agent, no horizontal
  scaling) is biased toward fleets of small narrow agents. Make that distinction explicit in the
  intro so a reader building a general-purpose agent doesn't bounce off.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Define pgvector, PostGIS, pg_trgm in README
  ([`97798e4`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/97798e46bfd3d6afc78c7ab439564420707fc03b))

Short aside explaining each extension's role — vector similarity search, spatial queries, trigram
  fuzzy text matching — so a reader new to the Postgres extension ecosystem doesn't have to go look
  them up to follow the rest of the doc. Also adds a language tag to the directory-tree fence to
  quiet markdownlint.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Document PostgREST's machine-readable OpenAPI at GET /
  ([`d2fead5`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/d2fead56310e60d9f83f92afc1331b66f178ec6b))

Previously the README mentioned "curl localhost:3000/" as a one-liner in passing. Expands it to a
  named subsection explaining what the spec contains (definitions per exposed table, paths per
  endpoint) and the integration use cases (SDK generation, agent-to-agent discovery) since the whole
  point of using PostgREST's OpenAPI is that another piece of software can read it without a human
  renderer in the loop.

Includes a small jq example for pulling just the exposed table names.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Expand the "what's compelling" list to include PostgREST
  ([`7c8b54d`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/7c8b54dd293fb422ace35250d32b712853c0a16f))

Reframe the "three extensions, briefly" block as "four pieces do most of the heavy lifting — three
  extensions inside Postgres, plus one sidecar beside it." Adds PostgREST to the list with a short
  blurb and a link down to the full section.

PostgREST isn't technically a Postgres extension (it's a separate Haskell service), so the heading
  calls that out explicitly rather than papering over it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Rewrite README as a library intro, not a framework pitch
  ([`fe58175`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/fe58175b42a509e63a9edc7bd4a908b4b22e278e))

Reframes strands-pg as a library of Postgres-backed primitives for Strands agents plus the minimum
  lifecycle glue that can't be tools. Leads with the problem (plumbing cost for purpose-built
  agents), lists what's actually in the box, shows a 15-line minimum agent and a realistic one,
  spells out what it doesn't do, and points at example/ + camping-db/ as worked examples.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Vpc -> VPS in the AgentCore callout
  ([`76d7bff`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/76d7bffb62b4fd1879bb8b616192ff631bd53d03))

Self-hosting on a rented server (DigitalOcean, Hetzner, Linode, etc.) is a VPS. VPC was the wrong
  acronym — that's an AWS/GCP network concept.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

### Features

- Db-backed prompts + CLI chat client + arm64 db image
  ([`8b6dbb4`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/8b6dbb44f7d73eb35652e751bf4e4af2d1843a5a))

- migrations/002_prompts.sql: prompts table (name PK, body, timestamps) -
  strands_pg.prompts.PgPromptStore: get/put/list/delete + seed_from_dir so fresh databases pick up
  ./prompts/*.md on first boot - make_app registers /prompts endpoints when a PromptStore is passed;
  PUT/DELETE invalidate the agent cache so the next /chat rebuilds with the updated system prompt
  (no restart needed) - example/app.py now reads system prompt from DB, seeded from disk -
  strands_pg.cli: `strands-pg-chat` interactive REPL + --prompts list and --put-prompt NAME
  FILE/text for hot-reconfiguration - images/db/Dockerfile: switch to pgvector/pgvector:pg17 base
  (multi-arch) and install postgis from PGDG; avoids postgis/postgis single-arch issue - memory.py:
  lazy-init boto3 client so app boots without AWS creds - example/.env.example: document AWS_PROFILE
  requirement for Bedrock

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Initial scaffold — strands-pg Phase 1 MVP
  ([`50c9ca3`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/50c9ca377721220aed4eaf8090bc28704fa9083b))

Bring up the reusable Strands + Postgres-for-everything framework: PLAN.md, src/strands_pg package
  (PgSessionManager, PgMemoryStore, FastAPI factory, migration runner), db + agent Dockerfiles,
  001_init.sql with pgvector / postgis / pg_trgm + sessions / memories tables, and a runnable
  example (docker-compose + Bedrock-backed reference agent with remember/recall tools).

PgSessionManager subclasses RepositorySessionManager + implements SessionRepository (same pattern as
  FileSessionManager / S3SessionManager) — idiomatic Strands extension, no shims.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Multi-user memory out of the box via memory_tools factory
  ([`77e22a4`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/77e22a4505b12069465efd242fc776a8c1b1925c))

strands_pg.memory_tools(namespace) returns [remember, recall] tools closing over the namespace, so
  every session / user / email gets an isolated memory bucket automatically. The example wires it as
  memory_tools(namespace=session_id) inside build_agent — new agents stamped from example/ are
  multi-user by default.

Verified end-to-end: brian@epetersons.com session recalls Brian's memories; jane@example.com session
  sees none of them. Memory table now partitions cleanly by namespace.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Pgidentity graduates into the framework
  ([`bf391c6`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/bf391c685d273279ca247c18da500ac9d9ad77de))

Moves the identities + identity_emails pattern from camping-db up into strands_pg. First primitive
  earned from the camping-db port.

framework: - migrations/003_identities.sql: identities(user_id PK, title, body, tags, metadata, ts)
  + identity_emails(email PK, user_id FK). - strands_pg.identity.PgIdentity:
  get/get_by_email/put/delete/list + seed_from_dir with a minimal YAML-ish frontmatter parser
  (title/tags/ emails). Same seed-on-first-boot shape as PgPromptStore. - __init__.py exports
  PgIdentity, Identity.

migration numbering convention: - framework owns 001-099, agents start at 100. Agent Dockerfile COPY
  runs after framework base migrations, so both live in /app/migrations/ and the runner sorts them
  numerically. Documented in PLAN.md locked decision 7.

camping-db rebase: - delete camping-db/identity.py + camping-db/migrations/005_identities.sql
  (superseded by framework). - renumber 003_camps.sql -> 100_camps.sql, 004_parcel_services.sql ->
  101_parcel_services.sql to leave room for framework growth. - camping-db/identities/*.md moved
  into the agent repo and seeded via PgIdentity.seed_from_dir on boot (no longer a legacy-mount
  dependency). - ingest.py drops identity-specific logic; now only camps + parcel_services. - app.py
  uses `identities = PgIdentity(); identities.seed_from_dir(...)` and
  `identities.get_by_email(session_id)` — same ergonomics as prompts.

verified end-to-end on a fresh DB: - migrations applied in correct order (001, 002, 003, 100, 101) -
  2 identities auto-seeded on boot with 2 + 3 email mappings - 15,668 camps re-ingested cleanly -
  chat via peterb154@zoleo.com (Brian's satellite address) correctly resolves to the brian_peterson
  identity and surfaces "KTM 890R + 300 XC"

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Postgrest sidecar for auto-CRUD + README notes
  ([`b648154`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/b648154ee38c2d84039a6afc6a7d2b1a4df3872e))

Wires PostgREST as an optional third service in camping-db/docker-compose.yml on port 3000. A new
  migration (102_pgrst_role.sql) creates a scoped web_anon role and grants SELECT on camps +
  parcel_services. Tables that aren't explicitly granted (sessions, session_messages, memories,
  identities, identity_emails, prompts) stay invisible to the PostgREST surface.

Verified: - PostgREST introspected 13 relations cleanly on boot. - GET /camps?state=eq.MT&type=eq.NF
  -> real rows with PostgREST filter syntax; Prefer: count=exact -> Content-Range 0-552/553. - GET
  /identities -> 42501 permission denied (correctly scoped). - OpenAPI spec served at /.

README: - New "Data APIs via PostgREST" section with the compose block, the role migration, and
  usage examples. Frames it as the answer to "how do I expose table data without hand-rolling a
  FastAPI handler per table." - Added a paragraph up front about Bedrock being the default for
  inference (Claude Sonnet/Opus/Haiku) and embeddings (Titan v2), with a note that it's swappable
  via Strands Model and PgMemoryStore(embedder=...). - Mentions the PostgREST sidecar in the "What's
  included" list and in the camping-db example's port layout.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- Shadcn-style installer + stamp-ready templates
  ([`7757cb2`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/7757cb26e22443522bff34ecc0e59863e733c2b0))

Switches distribution from "pip install strands-pg" to a one-shot bash installer that copies the
  framework source and a starter agent shell into a new directory. From that point the user owns
  every file — no runtime dependency on this repo, no pip registry resolving transitive deps in the
  background. The framing is supply-chain security, not ergonomics: pip/npm have become liabilities,
  and a pinned source copy is a narrower attack surface than a registry lookup.

install.sh (repo root): - Takes a target directory + optional --ref (or STRANDS_PG_REF env var). -
  Defaults to the latest git tag (or main if no tags exist). - Shallow clones the repo at the ref
  into a tmpdir. - Copies templates/agent/ + src/strands_pg/ + migrations/ into the target, creates
  tools/, drops a .strands-pg-ref marker so `diff -r` against a fresh stamp later is possible. -
  Prints next-step instructions.

templates/agent/: - Dockerfile — python:3.13-slim, pip install -r requirements.txt, copy code,
  PYTHONPATH=/app so the vendored strands_pg is importable. - docker-compose.yml — agent + db; a
  commented PostgREST stanza with a link to the upstream pattern docs. - db/Dockerfile —
  pgvector/pgvector:pg17 + PostGIS on top. - entrypoint.sh — wait for PG, run `python -m
  strands_pg.migrate`, exec. - app.py — minimum working agent with TODO-marked blocks for identities
  and domain tools so the reader knows the extension points. - prompts/{soul,rules}.md — starter
  content seeded into DB on first boot. - requirements.txt — the runtime Python deps (no pyproject
  ceremony). - .env.example, .gitignore, README.md — quickstart for the stamped agent.

README: - "Install" section rewritten as "You don't. There's no package to install." Walks through
  curl | bash with latest / pinned / paranoid variants. Explains the supply-chain motivation
  explicitly. - Shows the exact directory structure that lands in my-agent/. - "Updating" section:
  re-stamp into /tmp and diff -r. No auto-update. - Removed pip install references elsewhere
  (tagline, tool commands now use `python -m strands_pg.migrate`/`.cli` instead of script entries).

Verified end-to-end on a local simulation of the installer layout: - stamped into /tmp/test-stamp -
  docker compose up --build - 3 framework migrations applied (001_init, 002_prompts, 003_identities)
  - /health -> {"status":"ok"} - /prompts seeded from the template prompts/ directory - /chat via
  brian@example.com hit Bedrock and returned a Claude response

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **camping-db**: Port geocode + land_ownership + parcel_lookup
  ([`fe665b6`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/fe665b6f880ea6886b459935959497ffdf396462))

Three external-API tools that close the camping-db flow:

- tools/geo.py - geocode(place_name) via OSM Nominatim -> lat/lon - land_ownership(lat, lon) via BLM
  SMA ArcGIS, with USFS enrichment for forest name when the agency is USFS

- tools/parcels.py - parcel_lookup(lat, lon): reverse-geocode to county+state, look up the county's
  ArcGIS service in the parcel_services table (not a JSON file like the legacy repo), query the
  point, extract owner/acres/ address heuristically. If the county is missing from the registry,
  returns an actionable SQL INSERT hint for the agent to run.

app.py wires all three into the tool list alongside search_camps, get_campsite, and memory_tools.

verified end-to-end: - "Find me free BLM dispersed camping within 30 miles of Moab, UT" -> geocode
  resolved Moab, spatial search returned 6 correct BLM sites (Sand Flats, Kings Bottom, Goose
  Island, Hal Canyon, Big Bend, Willow Springs) sorted by distance. - land_ownership at (38.574,
  -109.546) correctly returned PVT (downtown Moab) with camping guidance. - parcel_lookup at
  (39.2077, -96.3105) in KS reverse-geocoded to Pottawatomie County -> hit the KS_Pottawatomie
  service URL from the registry -> returned the actual property owner and acreage.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

- **camping-db**: Walk-through port of camping-db onto strands_pg
  ([`240dd41`](https://github.com/peterb154/strands-pgsql-agent-framework/commit/240dd411353caa2d781ddd5531ec4b2d8971f20e))

Demonstrates the stamping pattern end-to-end. A new agent repo living alongside example/ in-tree for
  this walkthrough; would move to its own repo in production.

Structure (maps to PLAN.md steps 3-7 of the camping-db migration):

- migrations/003_camps.sql 15k-row camp table with PostGIS geography (Point), generated tsvector
  column, trigram + GIST indexes (replaces SQLite + FTS5 + Haversine-in-Python). -
  migrations/004_parcel_services replaces data/parcel_services.json. - migrations/005_identities
  two-table identity+email mapping so one user can have multiple email addresses
  (brian@epetersons.com + ZOLEO). Slated to graduate into strands_pg as PgIdentity. -
  scripts/ingest.py reads legacy SQLite, parcel JSON, identity markdown (with YAML frontmatter),
  writes to PG in one shot. ran clean: 15,668 camps + 3 parcels + 2 identities. - tools/camps.py
  search_camps + get_campsite using ST_DWithin / ST_Distance, plainto_tsquery, and the
  devel/water/free filters. - identity.py load_identity_by_email — resolves the session_id (email)
  to the identity body and prepends it to the system prompt. - prompts/soul.md + rules.md ported
  from the legacy prompt files, seeded into DB on first boot. - app.py Agent wired:
  tools=[search_camps, get_campsite, *memory_tools(session_id)], system prompt = soul + rules + user
  context. - Dockerfile FROM python:3.13 + pip install framework from ../src (local path today;
  would be FROM ghcr.io/.../strands-pg-agent once published). Layers on the agent's migrations/ +
  app.py + tools/ + prompts/. - docker-compose.yml second stack on port 8001 with its own pg volume;
  mounts ../../camping-db/data read-only for the one-shot ingest.

Verified: - 5 migrations applied cleanly on a fresh DB (framework 001/002 + camping 003/004/005). -
  Spatial query against Topeka coords returned correct 5 nearest in KS. - Chat as
  brian@epetersons.com correctly loaded Brian's identity (KTM rig, no road restrictions) and used it
  to reason about search results. - Chat with MT coords returned 6 correct free NF/BLM sites sorted
  by distance, with identity-aware framing ("accessible on your 890R").

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
