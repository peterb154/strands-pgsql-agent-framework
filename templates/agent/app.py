"""Your agent. Edit freely — you own this file.

The `build_agent(session_id)` callable is the single extension point; everything
else (prompts, identity, domain tools) plugs into the Agent you construct here.
Copy the commented-out blocks out of TODO as you grow.
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

# TODO (identity): uncomment when you have per-user profile docs.
# from strands_pg import PgIdentity

PROMPT_DIR = Path(__file__).parent / "prompts"
SYSTEM_PROMPT_PARTS = ["soul", "rules"]
MODEL_ID = os.environ.get("STRANDS_PG_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

prompts = PgPromptStore()
prompts.seed_from_dir(PROMPT_DIR)

# TODO (identity):
# identities = PgIdentity()
# identities.seed_from_dir(Path(__file__).parent / "identities")


def _system_prompt_for(session_id: str) -> str:
    base = prompts.assemble(SYSTEM_PROMPT_PARTS) or "You are a helpful assistant."
    # TODO (identity):
    # identity = identities.get_by_email(session_id)
    # if identity:
    #     base = f"{base}\n\n## USER CONTEXT\n{identity.body}"
    return base


def build_agent(session_id: str) -> Agent:
    return Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=_system_prompt_for(session_id),
        tools=[
            # TODO: import your domain tools from tools/ and add them here:
            # from tools.orders import search_orders, create_order
            # search_orders, create_order,
            *memory_tools(namespace=session_id),
        ],
        session_manager=PgSessionManager(session_id=session_id),
    )


app = make_app(build_agent, prompt_store=prompts)
