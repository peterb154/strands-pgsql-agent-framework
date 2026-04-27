"""Tests for ``strands_pg.session.session_lock``.

These hit a real Postgres — there's no point in mocking an advisory-lock
helper since the whole behavior under test is "Postgres serializes us."
Skipped when the configured pool can't reach Postgres (CI without a
DB, fresh checkout without env vars, etc.).
"""

from __future__ import annotations

import threading
import time

import pytest

try:
    from strands_pg._pool import get_pool
    from strands_pg.session import session_lock

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


pytestmark = pytest.mark.skipif(
    not _pg_reachable(), reason="Postgres not reachable"
)


def test_same_session_id_serializes() -> None:
    sid = f"test-lock-same-{time.time_ns()}"
    active = 0
    peak = 0
    guard = threading.Lock()

    def worker() -> None:
        nonlocal active, peak
        with session_lock(sid):
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.2)
            with guard:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert peak == 1, "lock should serialize same-session workers"


def test_distinct_session_ids_run_concurrently() -> None:
    active = 0
    peak = 0
    guard = threading.Lock()

    def worker(suffix: int) -> None:
        nonlocal active, peak
        with session_lock(f"test-lock-distinct-{time.time_ns()}-{suffix}"):
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.2)
            with guard:
                active -= 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert peak > 1, "distinct session_ids should run concurrently"


def test_lock_releases_on_exception() -> None:
    sid = f"test-lock-exc-{time.time_ns()}"

    class Boom(Exception):
        pass

    with pytest.raises(Boom), session_lock(sid):
        raise Boom

    acquired = threading.Event()

    def probe() -> None:
        with session_lock(sid):
            acquired.set()

    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(timeout=2)
    assert acquired.is_set(), "lock did not release after exception"
