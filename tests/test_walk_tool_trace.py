"""Tests for ``strands_pg.agentmail.walk_tool_trace``.

Pure-function tests — no DB, no network, no Strands runtime dependency.
The walker takes a list of Strands ``message`` dicts and reconstructs
the (toolUse, toolResult) pairs into a human-readable trace.
"""

from __future__ import annotations

from strands_pg import walk_tool_trace


def _tool_use(tool_use_id: str, name: str, input_: dict) -> dict:
    return {
        "role": "assistant",
        "content": [{"toolUse": {
            "toolUseId": tool_use_id, "name": name, "input": input_,
        }}],
    }


def _tool_result(tool_use_id: str, status: str, text: str) -> dict:
    return {
        "role": "user",
        "content": [{"toolResult": {
            "toolUseId": tool_use_id,
            "status": status,
            "content": [{"text": text}],
        }}],
    }


def test_reply_succeeded() -> None:
    messages = [
        _tool_use("1", "reply_to_message", {"text": "hi"}),
        _tool_result("1", "success", "sent"),
    ]
    status, lines = walk_tool_trace(messages)
    assert status == "success"
    assert len(lines) == 1
    assert "tool=reply_to_message" in lines[0]
    assert "status=success" in lines[0]


def test_reply_errored() -> None:
    messages = [
        _tool_use("1", "geocode", {"q": "Harrison, AR"}),
        _tool_result("1", "success", "lat=36.2"),
        _tool_use("2", "reply_to_message", {"messageId": "m1"}),
        _tool_result("2", "error", "400 Bad Request"),
    ]
    status, lines = walk_tool_trace(messages)
    assert status == "error"
    assert len(lines) == 2
    assert "tool=reply_to_message" in lines[1]
    assert "status=error" in lines[1]
    assert "400 Bad Request" in lines[1]


def test_reply_never_called() -> None:
    messages = [
        _tool_use("1", "web_search", {"q": "maps"}),
        _tool_result("1", "success", "no coords"),
    ]
    status, lines = walk_tool_trace(messages)
    assert status is None
    assert len(lines) == 1


def test_empty_message_list() -> None:
    status, lines = walk_tool_trace([])
    assert status is None
    assert lines == []


def test_orphan_tool_use_surfaces() -> None:
    """toolUse without matching toolResult should still appear in trace.

    Strands can exit a cycle after invoking a tool but before its
    result message is appended (model returns end_turn early, error
    mid-execution, etc.). The walker must still surface the toolUse
    and treat a ``reply_to_message`` orphan as ``status="no_result"``
    so the failure callback fires with diagnostic data.
    """
    messages = [
        _tool_use("1", "web_search", {"q": "foo"}),
        _tool_result("1", "success", "ok"),
        _tool_use("2", "reply_to_message", {"text": "hi"}),
        # No toolResult for "2" — cycle exited mid-tool.
    ]
    status, lines = walk_tool_trace(messages)
    assert status == "no_result"
    assert len(lines) == 2
    orphan_line = next(line for line in lines if "reply_to_message" in line)
    assert "status=(no result)" in orphan_line


def test_walker_does_not_log() -> None:
    """The walker must be pure: side effects belong to the caller.

    Regression for the ``_log_tool_trace``-emits-INFO design that
    forced tests to ignore logging side effects and made the walker
    awkward to reuse outside the email webhook.
    """
    import logging

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    handler.setLevel(logging.DEBUG)
    pkg_logger = logging.getLogger("strands_pg")
    pkg_logger.addHandler(handler)
    prior = pkg_logger.level
    pkg_logger.setLevel(logging.DEBUG)
    try:
        walk_tool_trace([
            _tool_use("1", "reply_to_message", {"text": "hi"}),
            _tool_result("1", "success", "sent"),
        ])
    finally:
        pkg_logger.removeHandler(handler)
        pkg_logger.setLevel(prior)

    assert records == [], (
        f"walk_tool_trace must not log; got {len(records)} record(s)"
    )
