"""Pre-built memory tools, auto-namespaced per session.

``memory_tools(namespace)`` returns ``[remember, recall]`` closing over the
namespace so a fresh pair is bound to each session at agent build time.
This is the default way to get multi-user memory with zero plumbing — drop
them into ``tools=[...]`` and every user's memory stays in their own bucket.

Usage (stamp template pattern):

    def build_agent(session_id: str) -> Agent:
        return Agent(
            ...,
            tools=[*memory_tools(namespace=session_id)],
        )
"""

from __future__ import annotations

from typing import Any

from strands import tool

from strands_pg.memory import PgMemoryStore


def memory_tools(
    namespace: str,
    *,
    store: PgMemoryStore | None = None,
    top_k: int = 5,
) -> list[Any]:
    """Build ``remember`` and ``recall`` tools bound to ``namespace``.

    Each call returns a fresh pair of tool callables so every session / user
    gets its own isolated memory bucket. The callables share a single
    ``PgMemoryStore`` (and therefore the pool) unless one is passed in.
    """
    if not namespace:
        raise ValueError("memory_tools requires a non-empty namespace")

    mem = store or PgMemoryStore()

    @tool
    def remember(text: str) -> str:
        """Save a durable note for this user."""
        mid = mem.add(text, namespace=namespace)
        return f"Saved memory #{mid}"

    @tool
    def recall(query: str, k: int = top_k) -> str:
        """Search this user's durable notes by meaning. Returns top-k hits."""
        hits = mem.search(query, k=k, namespace=namespace)
        if not hits:
            return "No matches."
        return "\n".join(f"- [{h.id}] {h.text}" for h in hits)

    return [remember, recall]
