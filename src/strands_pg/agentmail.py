"""AgentMail integration helpers.

Opt-in: only wired into an agent that calls ``attach_email_webhook(...)``.
The framework itself doesn't assume any agent uses email.

What's here:

- Pydantic models for the AgentMail webhook payload shape (inbound).
- ``attach_email_webhook(app, build_agent, known_emails, ...)`` — adds a
  ``POST /api/webhook/email`` route that validates, dedups, echo-loop
  guards, and kicks off the agent in a background thread. The agent's
  system prompt gets an ``INBOUND EMAIL CONTEXT`` section injected with
  the ``inbox_id`` / ``message_id`` / ``thread_id`` / ``sender`` / ``cc``
  the agent needs to reply via AgentMail's MCP ``reply_to_message`` tool.
- ``make_agentmail_mcp()`` — one-liner that opens an ``MCPClient`` to
  AgentMail's streamable-HTTP endpoint with the correct ``x-api-key``
  auth header (not Bearer, despite what you might expect).

Notes on AgentMail's event types:
- ``message.received`` fires on clean inbound mail.
- ``message.received.spam`` / ``.blocked`` fire when the classifier
  flags the message. These variants can't be subscribed via the UI;
  they go to the same webhook if subscribed. We use ``startswith(
  "message.received")`` so spam-flagged mail from known senders still
  gets processed — the ``known_emails`` allowlist is the real gate.
- Classifier-flagged delivery still depends on SPF/DKIM/DMARC being
  set up correctly on the *sending* domain. If you're seeing every
  test flagged as spam, check ``dig TXT <domain>`` first.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Module-level constant so a future AgentMail rename of the tool only
# requires bumping this one symbol (vs. a hardcoded string match
# scattered through the trace walker).
_REPLY_TOOL_NAME = "reply_to_message"


# ---------------------------------------------------------------------------
# pydantic payload models
# ---------------------------------------------------------------------------


class AgentMailAttachment(BaseModel):
    attachment_id: str
    filename: str | None = None
    size: int | None = None
    content_type: str | None = None


class AgentMailMessage(BaseModel):
    message_id: str
    from_: str | None = None
    to: list[str] = []
    cc: list[str] = []
    subject: str | None = None
    text: str | None = None
    html: str | None = None
    extracted_text: str | None = None
    thread_id: str | None = None
    inbox_id: str | None = None
    timestamp: str | None = None
    attachments: list[AgentMailAttachment] = []


class AgentMailWebhook(BaseModel):
    event_type: str
    message: AgentMailMessage


# ---------------------------------------------------------------------------
# observability helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureEvent:
    """Snapshot of a single failed inbound-email turn.

    Passed to the ``on_failure`` callback registered with
    ``attach_email_webhook``. Pure data — the framework does not assume
    any particular delivery channel for failure notifications. Consumers
    without a UI (e.g. agentmail-only agents) typically wire
    ``agentmail_operator_notify`` as the callback; chat-fronted agents
    that surface failures in their UI usually pass nothing.

    Attributes:
        inbound_message: the AgentMail webhook payload that triggered
            this turn. Carries ``message_id`` / ``thread_id`` /
            ``inbox_id`` / ``subject`` for diagnostics.
        sender: lowercased sender email (already extracted from the
            ``From:`` header).
        failure_reason: human-readable description. Either a Python
            exception text (if ``agent(body)`` raised) or a
            "reply_to_message not successfully called (last status=...)"
            line synthesized from the tool trace.
        trace_lines: per-tool-call human-readable trace, one entry per
            ``(toolUse, toolResult)`` pair. Already includes orphan
            toolUses surfaced as ``status=(no result)``.
    """

    inbound_message: AgentMailMessage
    sender: str
    failure_reason: str
    trace_lines: list[str] = field(default_factory=list)


def walk_tool_trace(
    messages: list[Any],
) -> tuple[str | None, list[str]]:
    """Walk a list of Strands messages and reconstruct the tool trace.

    Strands stores tool I/O as content items on the message stream:
    ``toolUse`` (model invoking a tool with ``input``) and ``toolResult``
    (the tool's response, with ``status`` ``"success"`` or ``"error"``).
    Strands' default logger only emits ``Tool #N: name`` markers — args
    and results are dropped, so a tool that returned an error result
    (rather than raising) leaves no diagnostic trail. This rebuilds it.

    Pure: returns data, emits no log records. The caller decides whether
    and at what level to log the result.

    Returns ``(reply_status, trace_lines)`` where ``reply_status`` is:

    - ``"success"`` / ``"error"`` — last ``reply_to_message`` toolResult's status
    - ``"no_result"`` — ``reply_to_message`` toolUse appeared but no
      toolResult was ever appended (cycle exited mid-tool)
    - ``None`` — ``reply_to_message`` was never invoked

    ``trace_lines`` is the human-readable per-tool trace, suitable for
    embedding in a notification or dumping to logs.
    """
    tool_uses: dict[str, dict] = {}
    matched: set[str] = set()
    reply_status: str | None = None
    trace_lines: list[str] = []
    for m in messages:
        for c in m.get("content", []) or []:
            if "toolUse" in c:
                tu = c["toolUse"]
                tool_uses[tu["toolUseId"]] = tu
            elif "toolResult" in c:
                tr = c["toolResult"]
                tu = tool_uses.get(tr["toolUseId"], {})
                name = tu.get("name", "<unknown>")
                status = tr.get("status")
                trace_lines.append(
                    f"tool={name} status={status} "
                    f"input={tu.get('input')!r} result={tr.get('content')!r}"
                )
                matched.add(tr["toolUseId"])
                if name == _REPLY_TOOL_NAME:
                    reply_status = status
    # Surface any toolUse without a matching toolResult — Strands can
    # exit a cycle after invoke and before the result message is
    # appended (model returns end_turn early, error mid-execution).
    # Without this we'd see "tool was called" with no input recorded.
    for tu_id, tu in tool_uses.items():
        if tu_id in matched:
            continue
        name = tu.get("name", "<unknown>")
        trace_lines.append(
            f"tool={name} status=(no result) input={tu.get('input')!r}"
        )
        if name == _REPLY_TOOL_NAME and reply_status is None:
            reply_status = "no_result"
    return reply_status, trace_lines


# ---------------------------------------------------------------------------
# MCP client helper
# ---------------------------------------------------------------------------


def make_agentmail_mcp(
    api_key: str | None = None,
    *,
    url: str = "https://mcp.agentmail.to/mcp",
) -> Any:
    """Return a started MCPClient for AgentMail's streamable-HTTP endpoint.

    Call this at module load and pass ``*mcp.list_tools_sync()`` into your
    Agent's tool list. The client stays alive for the process's lifetime.

    AgentMail authenticates with the ``x-api-key`` header, NOT
    ``Authorization: Bearer`` — empirically verified, despite how most
    API docs describe bearer auth. Getting this wrong yields 401.
    """
    key = api_key or os.environ.get("AGENTMAIL_API_KEY")
    if not key:
        raise RuntimeError("AGENTMAIL_API_KEY is not set")

    # Imports deferred so agents that don't use email don't pay the cost.
    from mcp.client.streamable_http import streamablehttp_client
    from strands.tools.mcp import MCPClient

    def _transport() -> Any:
        return streamablehttp_client(url=url, headers={"x-api-key": key})

    return MCPClient(_transport).start()


# ---------------------------------------------------------------------------
# inbound webhook
# ---------------------------------------------------------------------------


_DEFAULT_INBOUND_PROMPT = """\
## ⚠ CRITICAL — YOU MUST SEND YOUR REPLY BY CALLING A TOOL
This request came in as an EMAIL. The user will NOT see your response
unless you call the `reply_to_message` tool to actually send it.
Writing your answer as a chat-style response goes nowhere.

After composing your reply, your FINAL action MUST be:

    reply_to_message(
        inboxId="{inbox_id}",
        messageId="{message_id}",
        replyAll=True,
        text=<plain-text body, same content as html but no markup>,
        html=<HTML body — render markdown into p/h2/strong/ul/a tags>,
    )

## Email context for the reply
  inbox_id:   {inbox_id}
  message_id: {message_id}
  thread_id:  {thread_id}
  subject:    {subject!r}
  from:       {sender}
  cc:         {cc}
"""


def attach_email_webhook(
    app: FastAPI,
    build_agent: Callable[..., Any],
    known_emails: Callable[[], set[str]],
    *,
    agentmail_address: str = "",
    path: str = "/api/webhook/email",
    inbound_prompt_template: str = _DEFAULT_INBOUND_PROMPT,
) -> None:
    """Register a ``POST /api/webhook/email`` route on ``app``.

    Args:
        app: the FastAPI instance (typically from ``make_app(...)``).
        build_agent: must accept ``build_agent(session_id, extra_prompt="")``.
            The helper calls it with ``session_id=<sender_email>`` and an
            ``extra_prompt`` that includes the inbound email's IDs + a
            directive to call ``reply_to_message``. If your ``build_agent``
            doesn't accept ``extra_prompt``, wrap it.
        known_emails: called per-request; returns the set of emails
            allowed to trigger the agent. Dynamic (so new identities work
            immediately). Lowercase all entries.
        agentmail_address: the agent's own send-from address, lowercase.
            Used for echo-loop prevention (skip messages from self).
        path: URL path for the webhook. Default ``/api/webhook/email``.
        inbound_prompt_template: Python ``str.format``-style template
            with ``{inbox_id} {message_id} {thread_id} {subject} {sender}
            {cc}`` placeholders. Override if your agent's rules need
            different framing.
    """
    processed: set[str] = set()

    def _extract_email(raw: str) -> str:
        raw = (raw or "").strip()
        if "<" in raw:
            return raw.split("<")[-1].rstrip(">").strip()
        return raw

    @app.post(path)
    def email_webhook(payload: AgentMailWebhook) -> dict[str, str]:
        # Accept message.received AND its variants (.spam, .blocked) —
        # the known-sender allowlist is the real gate.
        if not payload.event_type.startswith("message.received"):
            return {"status": "skipped", "reason": f"ignored event: {payload.event_type}"}

        msg = payload.message
        sender = _extract_email(msg.from_ or "").lower()
        if not sender:
            return {"status": "skipped", "reason": "no sender"}

        if agentmail_address and sender == agentmail_address.lower():
            return {"status": "skipped", "reason": "echo loop"}

        if msg.message_id in processed:
            return {"status": "skipped", "reason": "duplicate"}
        processed.add(msg.message_id)

        if sender not in known_emails():
            return {"status": "skipped", "reason": "unknown sender"}

        threading.Thread(
            target=_process,
            args=(build_agent, msg, sender, inbound_prompt_template),
            daemon=True,
        ).start()
        return {"status": "accepted", "message_id": msg.message_id}


def _process(
    build_agent: Callable[..., Any],
    msg: AgentMailMessage,
    sender: str,
    template: str,
) -> None:
    body = msg.extracted_text or msg.text or msg.html or ""
    if not body:
        logger.warning("inbound email %s has empty body; skipping", msg.message_id)
        return

    extra = template.format(
        inbox_id=msg.inbox_id or "",
        message_id=msg.message_id,
        thread_id=msg.thread_id or "",
        subject=msg.subject or "",
        sender=sender,
        cc=", ".join(msg.cc) if msg.cc else "(none)",
    )

    try:
        agent = build_agent(sender, extra_prompt=extra)
        agent(body)
    except Exception:  # noqa: BLE001
        logger.exception("agent processing failed for message %s", msg.message_id)
