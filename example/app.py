"""Reference agent wired to strands_pg.

Session state, semantic memory, AND prompts live in Postgres. The first time
the process boots against an empty ``prompts`` table, we seed it from
``./prompts/*.md``. After that, prompts are edited via the ``/prompts/{name}``
API — no rebuild, no volume mount, no restart.
"""

from __future__ import annotations

import os
from pathlib import Path

from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from strands_pg import (
    PgMemoryStore,
    PgPromptStore,
    PgSessionManager,
    make_app,
)

PROMPT_DIR = Path(__file__).parent / "prompts"
SYSTEM_PROMPT_PARTS = ["soul", "rules"]
DEFAULT_PROMPT = "You are a helpful assistant."

MODEL_ID = os.environ.get("STRANDS_PG_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

memory = PgMemoryStore()
prompts = PgPromptStore()
prompts.seed_from_dir(PROMPT_DIR)


@tool
def remember(text: str) -> str:
    """Save a durable note for this user."""
    mid = memory.add(text)
    return f"Saved memory #{mid}"


@tool
def recall(query: str, k: int = 5) -> str:
    """Search durable notes by meaning. Returns top-k hits."""
    hits = memory.search(query, k=k)
    if not hits:
        return "No matches."
    return "\n".join(f"- [{h.id}] {h.text}" for h in hits)


def build_agent(session_id: str) -> Agent:
    system_prompt = prompts.assemble(SYSTEM_PROMPT_PARTS) or DEFAULT_PROMPT
    return Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=system_prompt,
        tools=[remember, recall],
        session_manager=PgSessionManager(session_id=session_id),
    )


app = make_app(
    build_agent,
    title="strands-pg example",
    prompt_store=prompts,
)
