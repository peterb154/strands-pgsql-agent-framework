"""Integration tests for ``attach_email_webhook``.

These don't hit Strands' real agent runtime. We pass a fake
``build_agent`` factory that returns a stand-in object with the
``hooks.add_callback`` and ``__call__`` surface that ``_process`` touches,
plus a fake ``MessageAddedEvent`` channel we drive directly. That's
enough to assert on the new contracts:

- ``on_failure`` receives the right ``FailureEvent`` shape on
  successful agent run that didn't reply.
- ``on_failure`` receives a Python-exception failure_reason when
  ``agent(body)`` raises.
- ``on_failure`` is NOT called on a successful reply.
- ``session_id_for`` callable is honored (and a raising callable
  falls back to sender).
- ``lock_session`` context manager wraps the agent run.
- New ``build_agent`` signature is required: ``(session_id, *,
  user_email, extra_prompt)``. Old positional-only signature would
  raise a TypeError at construction.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from strands_pg import FailureEvent
from strands_pg.agentmail import attach_email_webhook


class _FakeHooks:
    def __init__(self) -> None:
        self.callbacks: list[Any] = []

    def add_callback(self, event_type: Any, cb: Any) -> None:
        self.callbacks.append(cb)


class _FakeAgent:
    """Stand-in for a Strands Agent.

    Simulates the per-turn message stream: when called, fires the
    captured ``MessageAddedEvent`` hook with each message in
    ``programmed_messages``. ``raises`` lets a test simulate
    ``agent(body)`` raising.
    """

    def __init__(
        self,
        programmed_messages: list[dict] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.hooks = _FakeHooks()
        self._programmed_messages = programmed_messages or []
        self._raises = raises
        self.called_with: str | None = None

    def __call__(self, body: str) -> None:
        self.called_with = body
        for m in self._programmed_messages:
            for cb in self.hooks.callbacks:
                cb(_Event(m))
        if self._raises is not None:
            raise self._raises


class _Event:
    def __init__(self, message: dict) -> None:
        self.message = message


def _webhook_payload(
    *, message_id: str = "msg-1", from_: str = "user@example.com",
    thread_id: str | None = "thread-1", body: str = "hello",
) -> dict:
    return {
        "event_type": "message.received",
        "message": {
            "message_id": message_id,
            "from_": from_,
            "thread_id": thread_id,
            "extracted_text": body,
            "inbox_id": "inbox-1",
            "subject": "test",
        },
    }


def _wait_for_thread(timeout: float = 2.0) -> None:
    """Wait for the daemon thread spawned by the webhook to finish.

    The webhook fires-and-forgets, so test assertions need to wait for
    the agent run to complete. Joining on the only non-main thread
    is good enough for these tests.
    """
    deadline = threading.current_thread()  # noqa: F841 — sentinel only
    for t in threading.enumerate():
        if t is threading.current_thread() or not t.daemon:
            continue
        t.join(timeout=timeout)


def _build_app(
    build_agent: Any,
    *,
    on_failure: Any = None,
    session_id_for: Any = None,
    lock_session: Any = None,
) -> TestClient:
    app = FastAPI()
    attach_email_webhook(
        app,
        build_agent=build_agent,
        known_emails=lambda: {"user@example.com"},
        on_failure=on_failure,
        session_id_for=session_id_for,
        lock_session=lock_session,
    )
    return TestClient(app)


def test_on_failure_fires_when_reply_not_called() -> None:
    """No reply_to_message in the trace → on_failure with reason."""
    captured: list[FailureEvent] = []

    def fake_build_agent(
        session_id: str, *, user_email: str, extra_prompt: str = "",
    ) -> _FakeAgent:
        return _FakeAgent(programmed_messages=[
            {"role": "assistant", "content": [{"toolUse": {
                "toolUseId": "1", "name": "web_search", "input": {"q": "x"},
            }}]},
            {"role": "user", "content": [{"toolResult": {
                "toolUseId": "1", "status": "success",
                "content": [{"text": "ok"}],
            }}]},
        ])

    client = _build_app(
        fake_build_agent, on_failure=lambda fe: captured.append(fe),
    )
    r = client.post("/api/webhook/email", json=_webhook_payload())
    assert r.status_code == 200
    _wait_for_thread()

    assert len(captured) == 1
    fe = captured[0]
    assert fe.sender == "user@example.com"
    assert "reply_to_message not successfully called" in fe.failure_reason
    assert fe.inbound_message.message_id == "msg-1"
    assert any("tool=web_search" in line for line in fe.trace_lines)


def test_on_failure_fires_on_python_exception() -> None:
    """agent(body) raising → failure_reason starts with Python exception."""
    captured: list[FailureEvent] = []

    def fake_build_agent(
        session_id: str, *, user_email: str, extra_prompt: str = "",
    ) -> _FakeAgent:
        return _FakeAgent(raises=RuntimeError("boom"))

    client = _build_app(
        fake_build_agent, on_failure=lambda fe: captured.append(fe),
    )
    r = client.post("/api/webhook/email", json=_webhook_payload())
    assert r.status_code == 200
    _wait_for_thread()

    assert len(captured) == 1
    assert captured[0].failure_reason.startswith("Python exception: RuntimeError")


def test_on_failure_not_called_on_successful_reply() -> None:
    captured: list[FailureEvent] = []

    def fake_build_agent(
        session_id: str, *, user_email: str, extra_prompt: str = "",
    ) -> _FakeAgent:
        return _FakeAgent(programmed_messages=[
            {"role": "assistant", "content": [{"toolUse": {
                "toolUseId": "1", "name": "reply_to_message",
                "input": {"text": "hi"},
            }}]},
            {"role": "user", "content": [{"toolResult": {
                "toolUseId": "1", "status": "success",
                "content": [{"text": "sent"}],
            }}]},
        ])

    client = _build_app(
        fake_build_agent, on_failure=lambda fe: captured.append(fe),
    )
    r = client.post("/api/webhook/email", json=_webhook_payload())
    assert r.status_code == 200
    _wait_for_thread()

    assert captured == [], "successful reply should not fire on_failure"


def test_session_id_for_is_honored() -> None:
    seen_session_ids: list[str] = []

    def fake_build_agent(
        session_id: str, *, user_email: str, extra_prompt: str = "",
    ) -> _FakeAgent:
        seen_session_ids.append(session_id)
        return _FakeAgent()

    client = _build_app(
        fake_build_agent,
        session_id_for=lambda m: m.thread_id or m.message_id,
    )
    client.post("/api/webhook/email", json=_webhook_payload(thread_id="T"))
    _wait_for_thread()

    assert seen_session_ids == ["T"]


def test_session_id_for_raise_falls_back_to_sender() -> None:
    """A raising session_id_for must not 5xx the webhook."""
    seen_session_ids: list[str] = []

    def fake_build_agent(
        session_id: str, *, user_email: str, extra_prompt: str = "",
    ) -> _FakeAgent:
        seen_session_ids.append(session_id)
        return _FakeAgent()

    def boom(m: Any) -> str:
        raise ValueError("kaboom")

    client = _build_app(fake_build_agent, session_id_for=boom)
    r = client.post("/api/webhook/email", json=_webhook_payload())
    assert r.status_code == 200
    _wait_for_thread()
    assert seen_session_ids == ["user@example.com"]


def test_lock_session_wraps_run() -> None:
    """The lock_session context manager must enter before agent build
    and exit after agent run."""
    events: list[str] = []

    @contextmanager
    def fake_lock(session_id: str) -> Any:
        events.append(f"lock-enter:{session_id}")
        try:
            yield
        finally:
            events.append(f"lock-exit:{session_id}")

    def fake_build_agent(
        session_id: str, *, user_email: str, extra_prompt: str = "",
    ) -> _FakeAgent:
        events.append("build_agent")
        return _FakeAgent()

    client = _build_app(fake_build_agent, lock_session=fake_lock)
    client.post("/api/webhook/email", json=_webhook_payload())
    _wait_for_thread()

    assert events[0] == "lock-enter:user@example.com"
    assert events[-1] == "lock-exit:user@example.com"
    assert "build_agent" in events


def test_unknown_sender_skipped() -> None:
    """known_emails gate: unknown sender returns 200 skipped, no agent run."""
    called = False

    def fake_build_agent(*args: Any, **kwargs: Any) -> _FakeAgent:
        nonlocal called
        called = True
        return _FakeAgent()

    app = FastAPI()
    attach_email_webhook(
        app,
        build_agent=fake_build_agent,
        known_emails=lambda: set(),  # nobody allowed
    )
    client = TestClient(app)
    r = client.post("/api/webhook/email", json=_webhook_payload())
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"
    assert r.json()["reason"] == "unknown sender"
    _wait_for_thread()
    assert called is False


def test_old_build_agent_signature_breaks_loudly() -> None:
    """Old (session_id, extra_prompt='') factories must fail at call.

    Documents that v0.8.0 broke the implicit signature inference and
    consumers MUST use ``(session_id, *, user_email, extra_prompt='')``.
    """
    captured: list[FailureEvent] = []

    def old_factory(session_id: str, extra_prompt: str = "") -> _FakeAgent:
        # No ``user_email`` kwarg — the new contract requires it.
        return _FakeAgent()

    client = _build_app(
        old_factory, on_failure=lambda fe: captured.append(fe),
    )
    r = client.post("/api/webhook/email", json=_webhook_payload())
    assert r.status_code == 200  # webhook stays always-200
    _wait_for_thread()

    # The TypeError surfaces as a Python-exception failure_reason via
    # on_failure — the consumer's signal that they need to update the
    # factory.
    assert len(captured) == 1
    assert "TypeError" in captured[0].failure_reason
    assert "user_email" in captured[0].failure_reason


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
