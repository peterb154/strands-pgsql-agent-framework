"""Tests for ``strands_pg.agentmail_operator_notify``.

The factory builds a ``Callable[[FailureEvent], None]`` that POSTs to
AgentMail's REST send endpoint. We monkeypatch ``httpx.post`` and
assert on the args.
"""

from __future__ import annotations

from typing import Any

import pytest

from strands_pg import FailureEvent, agentmail_operator_notify
from strands_pg.agentmail import AgentMailMessage


@pytest.fixture
def captured_post(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace httpx.post with a recorder; return the captured calls."""
    calls: list[dict] = []

    class _Resp:
        status_code = 200
        reason_phrase = "OK"
        text = ""

    def fake_post(url: str, **kwargs: Any) -> _Resp:
        calls.append({"url": url, **kwargs})
        return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("AGENTMAIL_API_KEY", "test-key")
    return calls


def _failure_event(**overrides: Any) -> FailureEvent:
    msg = AgentMailMessage(
        message_id="m1",
        from_="user@example.com",
        thread_id="t1",
        subject="dinner ideas",
        inbox_id="agent@bot.example.com",
    )
    return FailureEvent(
        inbound_message=msg,
        sender="user@example.com",
        failure_reason=overrides.get(
            "failure_reason",
            "reply_to_message not successfully called (last status=None)",
        ),
        trace_lines=overrides.get(
            "trace_lines",
            ["tool=web_search status=success input={} result=()"],
        ),
    )


def test_posts_to_correct_inbox_endpoint(captured_post: list[dict]) -> None:
    notify = agentmail_operator_notify(
        "ops@example.com", "agent@bot.example.com",
    )
    notify(_failure_event())

    assert len(captured_post) == 1
    assert (
        captured_post[0]["url"]
        == "https://api.agentmail.to/v0/inboxes/agent@bot.example.com/messages/send"
    )


def test_uses_bearer_auth_for_rest(captured_post: list[dict]) -> None:
    """REST API uses Bearer; only the MCP transport uses x-api-key."""
    notify = agentmail_operator_notify(
        "ops@example.com", "agent@bot.example.com",
    )
    notify(_failure_event())

    assert (
        captured_post[0]["headers"]["Authorization"] == "Bearer test-key"
    )


def test_default_reply_to_is_noreply_at_from_domain(
    captured_post: list[dict],
) -> None:
    """Default Reply-To breaks the operator-replies-to-agent feedback loop."""
    notify = agentmail_operator_notify(
        "ops@example.com", "agent@bot.example.com",
    )
    notify(_failure_event())

    body = captured_post[0]["json"]
    assert body["reply_to"] == "noreply@bot.example.com"


def test_explicit_reply_to_overrides_default(
    captured_post: list[dict],
) -> None:
    notify = agentmail_operator_notify(
        "ops@example.com", "agent@bot.example.com",
        reply_to="postmaster@elsewhere.example.com",
    )
    notify(_failure_event())

    body = captured_post[0]["json"]
    assert body["reply_to"] == "postmaster@elsewhere.example.com"


def test_subject_includes_inbound_subject(captured_post: list[dict]) -> None:
    notify = agentmail_operator_notify(
        "ops@example.com", "agent@bot.example.com",
    )
    notify(_failure_event())
    assert captured_post[0]["json"]["subject"] == "reply failed: dinner ideas"


def test_body_includes_failure_reason_and_trace(
    captured_post: list[dict],
) -> None:
    notify = agentmail_operator_notify(
        "ops@example.com", "agent@bot.example.com",
    )
    notify(_failure_event(
        failure_reason="Python exception: RuntimeError: boom",
        trace_lines=[
            "tool=geocode status=success input={'q': 'x'} result=...",
            "tool=reply_to_message status=error input={...} result=400",
        ],
    ))

    text = captured_post[0]["json"]["text"]
    assert "Python exception: RuntimeError: boom" in text
    assert "tool=geocode" in text
    assert "tool=reply_to_message" in text
    assert "From:       user@example.com" in text
    assert "Message ID: m1" in text


def test_missing_api_key_logs_and_returns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """No API key → log a warning, do NOT raise. The webhook must keep
    accepting future inbound messages even if notifications fail."""
    monkeypatch.delenv("AGENTMAIL_API_KEY", raising=False)
    notify = agentmail_operator_notify(
        "ops@example.com", "agent@bot.example.com",
    )

    with caplog.at_level("WARNING", logger="strands_pg"):
        notify(_failure_event())  # must not raise

    assert any(
        "AGENTMAIL_API_KEY missing" in rec.message
        for rec in caplog.records
    )
