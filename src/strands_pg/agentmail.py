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
import urllib.parse
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from strands.hooks import MessageAddedEvent

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


def agentmail_operator_notify(
    to_email: str,
    from_inbox: str,
    *,
    reply_to: str | None = None,
    api_key: str | None = None,
    timeout: float = 15.0,
) -> Callable[[FailureEvent], None]:
    """Convenience factory for the no-UI / agentmail-only failure path.

    Returns a callable suitable for ``attach_email_webhook(...,
    on_failure=...)``. When invoked with a ``FailureEvent`` it sends a
    failure-notification email via AgentMail's REST API:
    ``POST /v0/inboxes/{from_inbox}/messages/send``.

    Bypasses the MCP send tool intentionally — if the agent failure
    was *in* the MCP path (auth, transport, server-side), routing the
    notification through the same path would just fail again the same
    way, leaving the operator silent.

    Sets a ``Reply-To`` header pointing at ``noreply@<from-domain>``
    by default. This breaks an otherwise-easy feedback loop: without
    it, an operator hitting Reply on a failure email lands a message
    at the agent's inbox FROM a known sender — triggering a fresh
    agent run on the failure trace.

    Args:
        to_email: where to send notifications (the operator).
        from_inbox: the agent's own AgentMail inbox address; the
            notification is sent FROM here. Used both as the REST path
            parameter and as the source for the default reply_to.
        reply_to: optional override for the Reply-To header. Defaults
            to ``noreply@<from_inbox-domain>``.
        api_key: optional AGENTMAIL_API_KEY override. If passed, it's
            captured at factory creation. If left ``None``, the env var
            is read fresh on each call (so rotated secrets pick up
            without a restart).
        timeout: HTTP timeout for the send POST. The notification path
            should never block the webhook handler indefinitely.

    Chat-fronted agents that surface failures in their UI typically
    don't need this — pass nothing for ``on_failure``.
    """
    domain = from_inbox.split("@", 1)[-1] if "@" in from_inbox else from_inbox
    default_reply_to = reply_to or f"noreply@{domain}"
    # Quote the inbox segment for path safety. AgentMail addresses
    # contain ``@`` legitimately, so keep that unescaped — but anything
    # else (path separators, weird Unicode in a misconfigured env var)
    # gets percent-encoded so a typo can't path-traverse the REST API.
    inbox_segment = urllib.parse.quote(from_inbox, safe="@")
    send_url = f"https://api.agentmail.to/v0/inboxes/{inbox_segment}/messages/send"

    def _notify(event: FailureEvent) -> None:
        # Read env on each call when no explicit key was passed, so a
        # rotated AGENTMAIL_API_KEY takes effect without a restart.
        key = api_key or os.environ.get("AGENTMAIL_API_KEY")
        if not key:
            logger.warning(
                "AGENTMAIL_API_KEY missing; cannot notify %s of failure on %s",
                to_email, event.inbound_message.message_id,
            )
            return

        trace_block = "\n".join(event.trace_lines) or "(no tool calls recorded)"
        text = (
            f"Agent failed to reply to an inbound email.\n\n"
            f"From:       {event.sender}\n"
            f"Subject:    {event.inbound_message.subject!r}\n"
            f"Message ID: {event.inbound_message.message_id}\n"
            f"Thread ID:  {event.inbound_message.thread_id}\n\n"
            f"Failure:\n  {event.failure_reason}\n\n"
            f"Tool trace:\n{trace_block}\n"
        )
        try:
            r = httpx.post(
                send_url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": to_email,
                    "subject": (
                        f"reply failed: "
                        f"{event.inbound_message.subject or '(no subject)'}"
                    ),
                    "text": text,
                    "reply_to": default_reply_to,
                },
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to send operator notification to %s for inbound %s",
                to_email, event.inbound_message.message_id,
            )
            return
        if r.status_code >= 400:
            logger.error(
                "operator notification to %s rejected for inbound %s: "
                "%s %s body=%r",
                to_email, event.inbound_message.message_id,
                r.status_code, r.reason_phrase, r.text[:500],
            )

    return _notify


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
    session_id_for: Callable[[AgentMailMessage], str] | None = None,
    lock_session: Callable[[str], AbstractContextManager[None]] | None = None,
    on_failure: Callable[[FailureEvent], None] | None = None,
) -> None:
    """Register a ``POST /api/webhook/email`` route on ``app``.

    Args:
        app: the FastAPI instance (typically from ``make_app(...)``).
        build_agent: factory invoked per inbound email with signature
            ``build_agent(session_id, *, user_email, extra_prompt="")``.
            ``session_id`` is whatever ``session_id_for`` returns
            (defaults to lowercased sender). ``user_email`` is always
            the lowercased sender so identity / memory namespacing stays
            tied to the person even when ``session_id`` is something
            else (e.g. an AgentMail thread_id). ``extra_prompt`` carries
            the inbound email's IDs and a directive to call
            ``reply_to_message``.
        known_emails: called per-request; returns the set of emails
            allowed to trigger the agent. Dynamic (so new identities
            work immediately). Lowercase all entries.
        agentmail_address: the agent's own send-from address, lowercase.
            Used for echo-loop prevention (skip messages from self).
        path: URL path for the webhook. Default ``/api/webhook/email``.
        inbound_prompt_template: Python ``str.format``-style template
            with ``{inbox_id} {message_id} {thread_id} {subject}
            {sender} {cc}`` placeholders. Override if your agent's rules
            need different framing.
        session_id_for: optional callable returning the session_id for a
            given inbound message. Defaults to the lowercased sender —
            historic sender-as-session behavior. Recommended:
            ``lambda m: m.thread_id or m.message_id`` to scope sessions
            per email thread (eliminates cross-thread context bleed and
            reduces same-session message_id races at the data layer).
        lock_session: optional callable returning a context manager that
            holds a session-scoped lock for the duration of an agent
            run. ``strands_pg.session_lock`` is the intended impl.
            Without it, two webhooks for the same session_id can race
            on Strands' message_id arithmetic and crash the in-flight
            agent with a unique-constraint violation.
        on_failure: optional callback fired when the agent either
            raises or fails to call ``reply_to_message`` with
            ``status="success"``. Receives a ``FailureEvent`` carrying
            the inbound message, sender, failure reason, and full tool
            trace. The framework does not assume any particular
            delivery channel — wire ``agentmail_operator_notify`` for
            agentmail-only / no-UI agents, or anything else (Slack,
            PagerDuty) by writing your own callable.
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

        # session_id_for is consumer-supplied; if it raises (bug in the
        # lambda, malformed payload, etc.) fall back to sender so the
        # webhook keeps its always-200 contract — n8n / AgentMail
        # shouldn't see 5xx for what's effectively a misconfiguration.
        try:
            session_id = session_id_for(msg) if session_id_for else sender
        except Exception:  # noqa: BLE001
            logger.exception(
                "session_id_for raised for inbound %s; falling back to sender",
                msg.message_id,
            )
            session_id = sender

        threading.Thread(
            target=_process,
            args=(build_agent, msg, sender, session_id,
                  inbound_prompt_template, lock_session, on_failure),
            daemon=True,
        ).start()
        return {"status": "accepted", "message_id": msg.message_id}


def _process(
    build_agent: Callable[..., Any],
    msg: AgentMailMessage,
    sender: str,
    session_id: str,
    template: str,
    lock_session: Callable[[str], AbstractContextManager[None]] | None,
    on_failure: Callable[[FailureEvent], None] | None,
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

    # Capture this turn's messages via a Strands hook rather than slicing
    # ``agent.messages`` after the run. Slicing is unreliable: Strands'
    # default sliding-window conversation manager and the
    # ``_fix_broken_tool_use`` path can both replace ``agent.messages``
    # wholesale during a cycle, invalidating the length snapshot. The
    # hook fires once per appended message — robust against in-place
    # pruning, list replacement, and partial-failure unwinds.
    #
    # ASSUMPTION: build_agent returns a fresh Agent per call. Strands'
    # ``HookRegistry`` has no ``remove_callback`` method, so the
    # registration below survives until the Agent is GC'd. With a
    # fresh-per-call factory the closure (and its ``turn_messages``
    # list) dies at function exit. With a *cached* agent (e.g.
    # ``make_app(cache_agents=True)`` reused across email turns) the
    # callbacks would accumulate — every prior turn's _capture closure
    # would also fire, and its turn_messages list would survive,
    # producing both a leak and incorrect trace data. Email-webhook
    # consumers MUST use ``cache_agents=False`` (the per-email
    # ``extra_prompt`` injection makes caching wrong on its own merits
    # anyway).
    turn_messages: list[Any] = []

    def _capture(event: Any) -> None:
        turn_messages.append(event.message)

    failure_reason: str | None = None
    cm = lock_session(session_id) if lock_session else nullcontext()
    try:
        with cm:
            agent = build_agent(
                session_id, user_email=sender, extra_prompt=extra,
            )
            # Register AFTER build_agent returns: PgSessionManager
            # registers its own MessageAddedEvent persistence callback
            # during Agent construction, and HookRegistry runs callbacks
            # in registration order. Persisting first means if
            # persistence raises, our capture for that message doesn't
            # fire — but agent(body) propagates the exception, and the
            # except block synthesizes a Python-exception failure_reason
            # below. Don't move this line up.
            agent.hooks.add_callback(MessageAddedEvent, _capture)
            agent(body)
    except Exception as e:  # noqa: BLE001
        logger.exception("agent processing failed for message %s", msg.message_id)
        failure_reason = f"Python exception: {type(e).__name__}: {e}"

    reply_status, trace_lines = walk_tool_trace(turn_messages)
    if failure_reason is None and reply_status != "success":
        failure_reason = (
            f"reply_to_message not successfully called "
            f"(last status={reply_status!r})"
        )

    if failure_reason is None:
        return

    # Dump the full trace in one WARNING record so it shows up via
    # Python's lastResort handler even when the consumer hasn't
    # configured logging. Per-line INFO would be silently filtered at
    # default log levels — and we don't want to dictate consumer log
    # config from a framework.
    trace_block = "\n  ".join(trace_lines) or "(no tool calls recorded)"
    logger.warning(
        "[email %s] agent failed: %s\n  trace:\n  %s",
        msg.message_id, failure_reason, trace_block,
    )

    if on_failure is not None:
        try:
            on_failure(FailureEvent(
                inbound_message=msg,
                sender=sender,
                failure_reason=failure_reason,
                trace_lines=trace_lines,
            ))
        except Exception:  # noqa: BLE001
            logger.exception(
                "on_failure callback raised for inbound %s", msg.message_id,
            )
