"""Tests for namespace scoping on ``PgMemoryStore``.

These hit a real Postgres — the behavior under test is a SQL WHERE clause,
so mocking the cursor would only assert that we wrote the string we wrote.
Skipped when the configured pool can't reach Postgres (CI without a DB,
fresh checkout without env vars, etc.).

The embedder is a fake: deterministic, no AWS, no network. We never assert
on ranking quality here, only on which rows a query is allowed to touch.
"""

from __future__ import annotations

import time

import pytest

try:
    from strands_pg._pool import get_pool
    from strands_pg.memory import PgMemoryStore

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


pytestmark = pytest.mark.skipif(not _pg_reachable(), reason="Postgres not reachable")

_DIMS = 1024


def _fake_embedder(text: str) -> list[float]:
    """Deterministic unit-ish vector derived from the text. Same text ->
    same vector, different text -> different vector. That's all we need."""
    seed = sum(ord(c) for c in text) or 1
    return [((seed * (i + 1)) % 97) / 97.0 for i in range(_DIMS)]


@pytest.fixture
def store() -> PgMemoryStore:
    return PgMemoryStore(embedder=_fake_embedder)


@pytest.fixture
def namespaces() -> tuple[str, str]:
    """Two tenants, unique per run so parallel runs don't collide."""
    stamp = time.time_ns()
    return f"test:alice:{stamp}", f"test:bob:{stamp}"


def _cleanup(*nss: str) -> None:
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM memories WHERE namespace = ANY(%s)", (list(nss),))
        conn.commit()


def test_delete_requires_namespace_argument(store: PgMemoryStore) -> None:
    """The whole point of the fix: you cannot call delete unscoped by accident."""
    with pytest.raises(TypeError):
        store.delete(1)  # type: ignore[call-arg]


def test_delete_wrong_namespace_leaves_row_intact(
    store: PgMemoryStore, namespaces: tuple[str, str]
) -> None:
    alice, bob = namespaces
    try:
        mid = store.add("alice's private note", namespace=alice)

        # Bob knows the id — they're sequential and recall shows them — but
        # is not in alice's namespace.
        assert store.delete(mid, namespace=bob) is False

        surviving = [h.id for h in store.list(namespace=alice)]
        assert mid in surviving
    finally:
        _cleanup(alice, bob)


def test_delete_correct_namespace_removes_row(
    store: PgMemoryStore, namespaces: tuple[str, str]
) -> None:
    alice, bob = namespaces
    try:
        mid = store.add("alice's private note", namespace=alice)

        assert store.delete(mid, namespace=alice) is True
        assert store.delete(mid, namespace=alice) is False  # already gone
        assert [h.id for h in store.list(namespace=alice)] == []
    finally:
        _cleanup(alice, bob)


def test_delete_explicit_none_is_cross_namespace(
    store: PgMemoryStore, namespaces: tuple[str, str]
) -> None:
    """The admin/prune escape hatch still works — it just has to be asked for."""
    alice, bob = namespaces
    try:
        mid = store.add("alice's private note", namespace=alice)
        assert store.delete(mid, namespace=None) is True
        assert [h.id for h in store.list(namespace=alice)] == []
    finally:
        _cleanup(alice, bob)


def test_search_never_crosses_namespaces(
    store: PgMemoryStore, namespaces: tuple[str, str]
) -> None:
    alice, bob = namespaces
    try:
        store.add("the shared secret phrase", namespace=alice)
        store.add("the shared secret phrase", namespace=bob)

        hits = store.search("the shared secret phrase", k=10, namespace=alice)

        assert len(hits) == 1
        assert {h.namespace for h in hits} == {alice}
    finally:
        _cleanup(alice, bob)


def test_list_offset_pages(
    store: PgMemoryStore, namespaces: tuple[str, str]
) -> None:
    alice, bob = namespaces
    try:
        for i in range(5):
            store.add(f"note {i}", namespace=alice)

        page1 = store.list(namespace=alice, limit=2, offset=0)
        page2 = store.list(namespace=alice, limit=2, offset=2)
        page3 = store.list(namespace=alice, limit=2, offset=4)

        assert [len(p) for p in (page1, page2, page3)] == [2, 2, 1]

        # Pages must not overlap and must cover everything exactly once.
        seen = [h.id for h in page1 + page2 + page3]
        assert len(set(seen)) == 5
        assert set(seen) == {h.id for h in store.list(namespace=alice, limit=100)}
    finally:
        _cleanup(alice, bob)


def test_created_at_is_populated(
    store: PgMemoryStore, namespaces: tuple[str, str]
) -> None:
    alice, bob = namespaces
    try:
        store.add("dated note", namespace=alice)

        from_list = store.list(namespace=alice)[0]
        from_search = store.search("dated note", k=1, namespace=alice)[0]

        assert from_list.created_at is not None
        assert from_search.created_at is not None
        assert from_list.created_at == from_search.created_at
    finally:
        _cleanup(alice, bob)
