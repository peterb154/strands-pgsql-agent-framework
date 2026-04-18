"""Camping-db agent, ported to strands_pg.

Stamping pattern: same as example/, but with domain migrations (camps,
parcel_services) and per-user identity context prepended to the system
prompt via the framework's PgIdentity.
"""

from __future__ import annotations

import os
from pathlib import Path

from strands import Agent
from strands.models.bedrock import BedrockModel
from tools.camps import get_campsite, search_camps

from strands_pg import (
    PgIdentity,
    PgPromptStore,
    PgSessionManager,
    make_app,
    memory_tools,
)

PROMPT_DIR = Path(__file__).parent / "prompts"
IDENTITIES_DIR = Path(__file__).parent / "identities"
SYSTEM_PROMPT_PARTS = ["soul", "rules"]
MODEL_ID = os.environ.get("STRANDS_PG_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

prompts = PgPromptStore()
prompts.seed_from_dir(PROMPT_DIR)

identities = PgIdentity()
identities.seed_from_dir(IDENTITIES_DIR)


def _system_prompt_for(session_id: str) -> str:
    base = prompts.assemble(SYSTEM_PROMPT_PARTS)
    identity = identities.get_by_email(session_id)
    if identity is None:
        return base
    return f"{base}\n\n## USER CONTEXT\n{identity.body}"


def build_agent(session_id: str) -> Agent:
    return Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=_system_prompt_for(session_id),
        tools=[search_camps, get_campsite, *memory_tools(namespace=session_id)],
        session_manager=PgSessionManager(session_id=session_id),
    )


app = make_app(
    build_agent,
    title="camping-db agent",
    prompt_store=prompts,
    # Rebuild per request so identity or prompt edits are picked up without restart.
    cache_agents=False,
)
