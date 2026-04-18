"""Reference agent wired to strands_pg.

The simplest possible agent: one Strands Agent per session, persistent
session via PgSessionManager, semantic memory via PgMemoryStore exposed as
two tools (remember / recall). Defaults to Bedrock Claude; swap the model by
setting ``STRANDS_PG_MODEL_ID`` or pre-constructing a ``BedrockModel``.

Copy this directory, edit ``prompts/`` and ``tools/``, and you have a new agent.
"""

from __future__ import annotations

import os
from pathlib import Path

from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from strands_pg import PgMemoryStore, PgSessionManager, make_app


def _load_prompt() -> str:
    base = Path(__file__).parent / "prompts"
    parts = []
    for name in ("soul.md", "rules.md"):
        p = base / name
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    return "\n\n".join(parts).strip() or "You are a helpful assistant."


memory = PgMemoryStore()
SYSTEM_PROMPT = _load_prompt()
MODEL_ID = os.environ.get("STRANDS_PG_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")


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
    lines = [f"- [{h.id}] {h.text}" for h in hits]
    return "\n".join(lines)


def build_agent(session_id: str) -> Agent:
    return Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=SYSTEM_PROMPT,
        tools=[remember, recall],
        session_manager=PgSessionManager(session_id=session_id),
    )


app = make_app(build_agent, title="strands-pg example")
