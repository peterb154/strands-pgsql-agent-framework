"""Tests for ``memory_tools(manage=True)`` — the curation tool surface.

Same real-Postgres posture as test_memory_scoping.py, and for the same
reason: the property under test is that each tool can only reach its own
namespace, which is a WHERE clause, not a mock.

The tools are Strands ``@tool`` objects but remain directly callable, so
these exercise the actual tool bodies an agent would invoke — not the
store methods underneath them. That's deliberate: the store is already
covered, and the risk this file exists for is a tool forgetting to pass
its namespace through.
"""

from __future__ import annotations

import os
import time
import zlib

import pytest

try:
    from strands_pg._pool import get_pool
    from strands_pg.memory import PgMemoryStore
    from strands_pg.memory_tools import memory_tools

    _import_ok = True
except Exception:  # noqa: BLE001
    _import_ok = False


def _pg_reachable() -> bool:
    if not _import_ok:
        return False
    try:
        with get_pool().connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


_REACHABLE = _pg_reachable()

if os.environ.get("CI") == "true" and not _REACHABLE:
    raise RuntimeError(
        "Postgres is required in CI: the manage-tool scoping tests must fail "
        "loudly rather than skip."
    )

pytestmark = pytest.mark.skipif(not _REACHABLE, reason="Postgres not reachable")

_DIMS = 1024


def _fake_embedder(text: str) -> list[float]:
    seed = zlib.crc32(text.encode()) or 1
    return [((seed * (i + 1)) % 97) / 97.0 for i in range(_DIMS)]


@pytest.fixture
def store() -> PgMemoryStore:
    return PgMemoryStore(embedder=_fake_embedder)


@pytest.fixture
def namespaces() -> tuple[str, str]:
    stamp = time.time_ns()
    return f"test:alice:{stamp}", f"test:bob:{stamp}"


def _cleanup(*nss: str) -> None:
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM memories WHERE namespace = ANY(%s)", (list(nss),))
        conn.commit()


def _by_name(tools: list) -> dict:
    """Index built tools by the name the model would see."""
    return {t.__name__: t for t in tools}


def test_manage_is_off_by_default(store: PgMemoryStore) -> None:
    """Deletion must not appear on an agent that didn't ask for it."""
    names = set(_by_name(memory_tools(namespace="test:x", store=store)))

    assert names == {"remember", "recall"}


def test_manage_adds_the_trio(store: PgMemoryStore) -> None:
    names = set(_by_name(memory_tools(namespace="test:x", store=store, manage=True)))

    assert names == {
        "remember",
        "recall",
        "list_memories",
        "update_memory",
        "forget_memory",
    }


def test_manage_tools_are_suffixed_per_scope(store: PgMemoryStore) -> None:
    """Multi-scope agents need to tell household curation from personal."""
    names = set(
        _by_name(
            memory_tools(
                namespaces={"personal": "test:p", "household": "test:h"},
                store=store,
                manage=True,
            )
        )
    )

    assert names == {
        "remember_personal",
        "recall_personal",
        "list_memories_personal",
        "update_memory_personal",
        "forget_memory_personal",
        "remember_household",
        "recall_household",
        "list_memories_household",
        "update_memory_household",
        "forget_memory_household",
    }


def test_curation_round_trip(store: PgMemoryStore, namespaces: tuple[str, str]) -> None:
    """Issue #6's acceptance path: list with dates, edit in place, delete."""
    alice, bob = namespaces
    try:
        tools = _by_name(memory_tools(namespace=alice, store=store, manage=True))
        mid = store.add("prefers gluten-free and dislikes pork", namespace=alice)

        listed = tools["list_memories"]()
        assert f"[{mid}]" in listed
        assert "prefers gluten-free" in listed
        # Dates are rendered so the model can tell standing facts from recent
        # ones — the reason created_at was added to MemoryHit in the first
        # place. Compared against the row's own timestamp, not today's local
        # date: created_at comes back in UTC, so asserting time.strftime()
        # passes all day and fails for the hours when the two dates differ.
        stored = store.list(namespace=alice)[0]
        assert stored.created_at is not None
        assert f"({stored.created_at.strftime('%Y-%m-%d')})" in listed

        assert tools["update_memory"](mid, "prefers gluten-free") == f"Updated memory #{mid}"
        assert "dislikes pork" not in tools["list_memories"]()

        assert tools["forget_memory"](mid) == f"Deleted memory #{mid}"
        assert tools["list_memories"]() == "No notes."
    finally:
        _cleanup(alice, bob)


def test_tools_cannot_reach_another_namespace(
    store: PgMemoryStore, namespaces: tuple[str, str]
) -> None:
    """The #3 property, asserted at the tool layer rather than the store.

    Bob's tools know alice's id — ids are sequential and every listing shows
    them — and must still be unable to read, edit or delete her note.
    """
    alice, bob = namespaces
    try:
        mid = store.add("alice's private note", namespace=alice)
        bobs = _by_name(memory_tools(namespace=bob, store=store, manage=True))

        assert bobs["list_memories"]() == "No notes."
        assert bobs["update_memory"](mid, "overwritten by bob") == f"No memory #{mid} here."
        assert bobs["forget_memory"](mid) == f"No memory #{mid} here."

        survivor = store.list(namespace=alice)[0]
        assert survivor.id == mid
        assert survivor.text == "alice's private note"
    finally:
        _cleanup(alice, bob)


def test_wrong_tenant_and_not_found_are_indistinguishable(
    store: PgMemoryStore, namespaces: tuple[str, str]
) -> None:
    """Distinguishing them would confirm a row exists in someone else's
    namespace — an existence oracle over sequential, guessable ids.

    The two ids can't be compared directly (each message names its own id),
    so this asserts both render the *same template*: nothing but the echoed
    id differs between "exists, not yours" and "doesn't exist".
    """
    alice, bob = namespaces
    try:
        real_id = store.add("alice's private note", namespace=alice)
        missing_id = real_id + 10_000_000  # nobody's row

        bobs = _by_name(memory_tools(namespace=bob, store=store, manage=True))

        for mid in (real_id, missing_id):
            assert bobs["forget_memory"](mid) == f"No memory #{mid} here."
            assert bobs["update_memory"](mid, "x") == f"No memory #{mid} here."
    finally:
        _cleanup(alice, bob)


def test_list_pages_and_reports_the_end(store: PgMemoryStore, namespaces: tuple[str, str]) -> None:
    alice, bob = namespaces
    try:
        for i in range(3):
            store.add(f"note number {i}", namespace=alice)
        tools = _by_name(memory_tools(namespace=alice, store=store, manage=True))

        assert len(tools["list_memories"](limit=2).splitlines()) == 2
        assert len(tools["list_memories"](limit=2, offset=2).splitlines()) == 1
        # Past the end reads differently from an empty store, so the model
        # doesn't conclude the whole namespace is empty.
        assert tools["list_memories"](limit=2, offset=99) == "No more notes."
    finally:
        _cleanup(alice, bob)
