"""Tests for namespace scoping on ``PgMemoryStore``.

These hit a real Postgres — the behavior under test is a SQL WHERE clause,
so mocking the cursor would only assert that we wrote the string we wrote.
Skipped locally when the configured pool can't reach Postgres (fresh
checkout without env vars, etc.).

Under CI the skip is an error instead. These guard a cross-tenant boundary,
and a skipped security test is indistinguishable from a passing one in a CI
summary — silently skipping here is how a regression on #3 ships green.

The embedder is a fake: deterministic, no AWS, no network. We never assert
on ranking quality here, only on which rows a query is allowed to touch.
"""

from __future__ import annotations

import os
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


_REACHABLE = _pg_reachable()

if os.environ.get("CI") == "true" and not _REACHABLE:
    raise RuntimeError(
        "Postgres is required in CI: the namespace-scoping tests must fail "
        "loudly rather than skip. Check the db service in .github/workflows/"
        "test.yml and that STRANDS_PG_DSN points at it."
    )

pytestmark = pytest.mark.skipif(not _REACHABLE, reason="Postgres not reachable")

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


def test_delete_across_namespaces_reaches_what_scoped_delete_cannot(
    store: PgMemoryStore, namespaces: tuple[str, str]
) -> None:
    """The admin escape hatch, asserted by contrast.

    The contrast is the test: asserting only that the row is gone afterwards
    would pass identically for a correctly-scoped delete, so it would stay
    green through the exact regression it's named for.
    """
    alice, bob = namespaces
    try:
        mid = store.add("alice's private note", namespace=alice)

        assert store.delete(mid, namespace=bob) is False  # scoped can't touch it
        assert store.delete_across_namespaces(mid) is True  # only this can

        assert [h.id for h in store.list(namespace=alice)] == []
    finally:
        _cleanup(alice, bob)


def test_delete_with_none_namespace_fails_closed(
    store: PgMemoryStore, namespaces: tuple[str, str]
) -> None:
    """Belt and braces: the annotation says ``str``, but Python doesn't
    enforce annotations, so an untyped caller can still get ``None`` in
    here. It must delete nothing rather than everything — the old behavior
    of this argument was the opposite, which is what #3 was about.
    """
    alice, bob = namespaces
    try:
        mid = store.add("alice's private note", namespace=alice)

        assert store.delete(mid, namespace=None) is False  # type: ignore[arg-type]
        assert mid in [h.id for h in store.list(namespace=alice)]
    finally:
        _cleanup(alice, bob)


def test_update_preserves_identity_and_reembeds(
    store: PgMemoryStore, namespaces: tuple[str, str]
) -> None:
    """The acceptance criteria from mealie-agent#15, in one pass.

    Editing preserves id and created_at (that's the whole point — otherwise
    delete-then-add would do), and search follows the new wording rather
    than the old, which is the part that's easy to get wrong.
    """
    alice, bob = namespaces
    try:
        mid = store.add("Bes prefers gluten-free and also dislikes pork", namespace=alice)
        before = store.list(namespace=alice)[0]

        assert store.update(mid, "Bes prefers gluten-free", namespace=alice) is True

        after = store.list(namespace=alice)[0]
        assert after.id == mid  # identity preserved
        assert after.created_at == before.created_at  # provenance preserved
        assert after.text == "Bes prefers gluten-free"

        # The embedding moved with the text: the new wording is an exact hit,
        # the dropped wording no longer is.
        exact = store.search("Bes prefers gluten-free", k=1, namespace=alice)[0]
        assert exact.id == mid
        assert exact.distance == pytest.approx(0.0, abs=1e-6)

        stale = store.search(
            "Bes prefers gluten-free and also dislikes pork", k=1, namespace=alice
        )[0]
        assert stale.distance > 1e-6
    finally:
        _cleanup(alice, bob)


def test_update_wrong_namespace_changes_nothing(
    store: PgMemoryStore, namespaces: tuple[str, str]
) -> None:
    """Cross-tenant overwrite is worse than cross-tenant delete — the victim
    keeps a note that no longer says what they wrote."""
    alice, bob = namespaces
    try:
        mid = store.add("alice's original note", namespace=alice)

        assert store.update(mid, "bob's replacement text", namespace=bob) is False

        surviving = store.list(namespace=alice)[0]
        assert surviving.id == mid
        assert surviving.text == "alice's original note"
    finally:
        _cleanup(alice, bob)


def test_search_never_crosses_namespaces(store: PgMemoryStore, namespaces: tuple[str, str]) -> None:
    alice, bob = namespaces
    try:
        store.add("the shared secret phrase", namespace=alice)
        store.add("the shared secret phrase", namespace=bob)

        hits = store.search("the shared secret phrase", k=10, namespace=alice)

        assert len(hits) == 1
        assert {h.namespace for h in hits} == {alice}
    finally:
        _cleanup(alice, bob)


def test_list_offset_pages(store: PgMemoryStore, namespaces: tuple[str, str]) -> None:
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


def test_created_at_is_populated(store: PgMemoryStore, namespaces: tuple[str, str]) -> None:
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
