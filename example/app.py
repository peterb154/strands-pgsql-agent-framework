"""Reference agent wired to strands_pg.

Session state, semantic memory, AND prompts live in Postgres. The first time
the process boots against an empty ``prompts`` table, we seed it from
``./prompts/*.md``. After that, prompts are edited via the ``/prompts/{name}``
API — no rebuild, no volume mount, no restart.

Memory is automatically namespaced per session via ``memory_tools(session_id)``,
so every user / email / chat thread gets an isolated memory bucket.
"""

from __future__ import annotations

import os
from pathlib import Path

from strands import Agent
from strands.models.bedrock import BedrockModel

from strands_pg import (
    PgPromptStore,
    PgSessionManager,
    make_app,
    memory_tools,
)

PROMPT_DIR = Path(__file__).parent / "prompts"
SYSTEM_PROMPT_PARTS = ["soul", "rules"]
DEFAULT_PROMPT = "You are a helpful assistant."

MODEL_ID = os.environ.get("STRANDS_PG_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

prompts = PgPromptStore()
prompts.seed_from_dir(PROMPT_DIR)


def build_agent(session_id: str) -> Agent:
    system_prompt = prompts.assemble(SYSTEM_PROMPT_PARTS) or DEFAULT_PROMPT
    return Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=system_prompt,
        tools=memory_tools(namespace=session_id),
        session_manager=PgSessionManager(session_id=session_id),
    )


app = make_app(
    build_agent,
    title="strands-pg example",
    prompt_store=prompts,
)
