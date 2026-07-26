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


def test_delete_signature_refuses_unscoped_calls() -> None:
    """Guards the API *shape* of the #3 fix, with no database needed.

    Lives here rather than in test_memory_scoping.py so it survives when
    Postgres is unreachable — this is the assertion that stops someone
    "helpfully" restoring a default namespace later. Uses inspect rather
    than a live call so no connection is ever opened.
    """
    import inspect

    from strands_pg import PgMemoryStore

    sig = inspect.signature(PgMemoryStore.delete)
    ns = sig.parameters["namespace"]

    assert ns.kind is inspect.Parameter.KEYWORD_ONLY, (
        "namespace must be keyword-only so it can't be passed positionally "
        "by accident"
    )
    assert ns.default is inspect.Parameter.empty, (
        "namespace must have no default — an optional scope on a destructive "
        "op is the bug from #3"
    )
    assert ns.annotation == "str", (
        "namespace must be str, not str | None: None would have to mean "
        "'every namespace', which is what delete_across_namespaces is for"
    )

    # The unscoped path exists, but only under its own explicit name.
    assert callable(PgMemoryStore.delete_across_namespaces)
    assert "namespace" not in inspect.signature(
        PgMemoryStore.delete_across_namespaces
    ).parameters
