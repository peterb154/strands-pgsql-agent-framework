"""FastAPI factory for a strands_pg agent.

Strands binds a ``SessionManager`` to an ``Agent`` at construction time, so we
take a *factory* — ``agent_factory(session_id) -> Agent`` — rather than a
single agent instance. Callers decide whether to cache agents by session_id
or rebuild per request; default behavior caches in-process.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from strands_pg.prompts import PgPromptStore

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


class PromptBody(BaseModel):
    body: str = Field(..., min_length=1)


class PromptOut(BaseModel):
    name: str
    body: str


def make_app(
    agent_factory: AgentFactory,
    *,
    cache_agents: bool = True,
    title: str = "strands-pg agent",
    prompt_store: PgPromptStore | None = None,
) -> FastAPI:
    """Build a FastAPI app exposing /health, /chat, and /prompts endpoints.

    ``prompt_store``: if provided, /prompts endpoints are registered and the
    agent factory is dropped from the cache whenever a prompt changes (so the
    next request builds a fresh agent with the updated prompt). If None, no
    /prompts endpoints are registered.
    """
    app = FastAPI(title=title)
    agents: dict[str, Any] = {}

    def get_agent(session_id: str) -> Any:
        if cache_agents and session_id in agents:
            return agents[session_id]
        agent = agent_factory(session_id)
        if cache_agents:
            agents[session_id] = agent
        return agent

    def invalidate_agents() -> None:
        agents.clear()

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

    @app.post("/chat/stream")
    async def chat_stream(req: ChatRequest) -> EventSourceResponse:
        """Same as /chat but streams events as Server-Sent Events.

        Event shape is normalized across Strands SDK versions:
          - ``event: text``       text delta chunks
          - ``event: thinking``   reasoning-text deltas (when model streams them)
          - ``event: tool_use``   tool name being invoked
          - ``event: done``       terminal event (empty data)
          - ``event: error``      any exception (data = error message)
        """
        return EventSourceResponse(_stream_agent(get_agent, req))

    if prompt_store is not None:

        @app.get("/prompts", response_model=list[PromptOut])
        def list_prompts() -> list[PromptOut]:
            return [PromptOut(name=p.name, body=p.body) for p in prompt_store.list()]

        @app.get("/prompts/{name}", response_model=PromptOut)
        def get_prompt(name: str) -> PromptOut:
            p = prompt_store.get(name)
            if p is None:
                raise HTTPException(status_code=404, detail=f"prompt {name!r} not found")
            return PromptOut(name=p.name, body=p.body)

        @app.put("/prompts/{name}", response_model=PromptOut)
        def put_prompt(name: str, req: PromptBody) -> PromptOut:
            p = prompt_store.put(name, req.body)
            invalidate_agents()
            return PromptOut(name=p.name, body=p.body)

        @app.delete("/prompts/{name}")
        def delete_prompt(name: str) -> dict[str, bool]:
            ok = prompt_store.delete(name)
            if not ok:
                raise HTTPException(status_code=404, detail=f"prompt {name!r} not found")
            invalidate_agents()
            return {"deleted": True}

    return app


async def _stream_agent(
    get_agent: Callable[[str], Any], req: ChatRequest
) -> AsyncIterator[dict[str, str]]:
    """Bridge Strands' native stream_async into normalized SSE events.

    Strands emits dict events with keys like ``data``, ``reasoningText``,
    ``current_tool_use``, ``complete``. We collapse those into a stable
    ``{event, data}`` shape so SSE consumers don't break when the SDK's
    internal event shape evolves.
    """
    try:
        agent = get_agent(req.session_id)
        seen_tool_ids: set[str] = set()
        async for ev in agent.stream_async(req.message):
            if "reasoningText" in ev and ev["reasoningText"]:
                yield {"event": "thinking", "data": ev["reasoningText"]}
            elif "current_tool_use" in ev:
                tool = ev["current_tool_use"] or {}
                tool_id = tool.get("toolUseId") or ""
                if tool_id and tool_id not in seen_tool_ids:
                    seen_tool_ids.add(tool_id)
                    yield {"event": "tool_use", "data": tool.get("name", "") or ""}
            elif "data" in ev and ev["data"]:
                yield {"event": "text", "data": ev["data"]}
        yield {"event": "done", "data": ""}
    except Exception as exc:  # noqa: BLE001 — surface via SSE error event
        logger.exception("/chat/stream failed for session_id=%s", req.session_id)
        yield {"event": "error", "data": str(exc)}
