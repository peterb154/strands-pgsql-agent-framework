"""Minimum viable smoke test — package imports cleanly."""

from __future__ import annotations


def test_public_api_imports() -> None:
    from strands_pg import (
        PgMemoryStore,
        PgSessionManager,
        commit_sha,
        make_app,
        memory_tools,
    )

    assert PgSessionManager is not None
    assert PgMemoryStore is not None
    assert make_app is not None
    assert memory_tools is not None
    assert commit_sha is not None


def test_commit_sha_returns_something() -> None:
    """In-repo we expect a real 7-char sha; outside repo we expect 'unknown'."""
    from strands_pg import commit_sha

    sha = commit_sha()
    # Either a real sha (7 chars by default) or the unknown sentinel.
    assert sha == "unknown" or len(sha) == 7


def test_memory_tools_requires_namespace_shape() -> None:
    from strands_pg import memory_tools

    try:
        memory_tools()
    except ValueError:
        pass
    else:
        raise AssertionError("memory_tools() with no args should raise ValueError")

    try:
        memory_tools(namespace="u:1", namespaces={"personal": "u:1"})
    except ValueError:
        pass
    else:
        raise AssertionError(
            "memory_tools should reject passing both namespace and namespaces"
        )
