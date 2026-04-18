"""FastAPI factory for a strands_pg agent.

Strands binds a ``SessionManager`` to an ``Agent`` at construction time, so we
take a *factory* — ``agent_factory(session_id) -> Agent`` — rather than a
single agent instance. Callers decide whether to cache agents by session_id
or rebuild per request; default behavior caches in-process.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from strands.agent.agent import Agent

logger = logging.getLogger(__name__)

AgentFactory = Callable[[str], "Agent"]


class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    session_id: str
    response: str


def make_app(
    agent_factory: AgentFactory,
    *,
    cache_agents: bool = True,
    title: str = "strands-pg agent",
) -> FastAPI:
    """Build a FastAPI app exposing /health and /chat."""
    app = FastAPI(title=title)
    agents: dict[str, Any] = {}

    def get_agent(session_id: str) -> Any:
        if cache_agents and session_id in agents:
            return agents[session_id]
        agent = agent_factory(session_id)
        if cache_agents:
            agents[session_id] = agent
        return agent

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/chat", response_model=ChatResponse)
    def chat(req: ChatRequest) -> ChatResponse:
        try:
            agent = get_agent(req.session_id)
            result = agent(req.message)
        except Exception as exc:  # noqa: BLE001 — surface as 500 to the client
            logger.exception("chat failed for session_id=%s", req.session_id)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        return ChatResponse(session_id=req.session_id, response=str(result))

    return app
