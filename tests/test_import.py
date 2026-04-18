"""Minimum viable smoke test — package imports cleanly."""

from __future__ import annotations


def test_public_api_imports() -> None:
    from strands_pg import PgMemoryStore, PgSessionManager, make_app

    assert PgSessionManager is not None
    assert PgMemoryStore is not None
    assert make_app is not None
