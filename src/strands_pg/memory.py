"""Semantic memory store backed by pgvector.

One row = one remembered fact. ``namespace`` partitions memory per-user /
per-topic inside a single agent (e.g. email address, user id).

Embeddings are computed by a caller-supplied callable — typically Bedrock
Titan / Cohere, or a local Ollama. Defaults to a Bedrock Titan v2 embedder
(1024 dims) when boto3 is available; fall back to passing your own.

Phase-2 option: swap this out for pgai-vectorizer so the DB manages embedding
sync via triggers. Left as an exercise — wire whichever embedder suits the
agent.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from strands_pg._pool import get_pool

logger = logging.getLogger(__name__)

Embedder = Callable[[str], list[float]]


@dataclass
class MemoryHit:
    """One result from a memory search."""

    id: int
    namespace: str
    text: str
    metadata: dict[str, Any]
    distance: float  # cosine distance in [0, 2]; lower = closer
    created_at: datetime | None = None  # None only if constructed by hand


class PgMemoryStore:
    """Add/search/delete semantic memories."""

    def __init__(
        self,
        embedder: Embedder | None = None,
        dsn: str | None = None,
        default_namespace: str = "default",
    ) -> None:
        self._pool = get_pool(dsn)
        self._embedder = embedder or _default_embedder()
        self._default_namespace = default_namespace

    def add(
        self,
        text: str,
        metadata: dict[str, Any] | None = None,
        namespace: str | None = None,
    ) -> int:
        """Insert a memory, computing its embedding. Returns the new row id."""
        ns = namespace or self._default_namespace
        embedding = self._embedder(text)

        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memories (namespace, text, metadata, embedding)
                VALUES (%s, %s, %s, %s::vector)
                RETURNING id
                """,
                (ns, text, Jsonb(metadata or {}), embedding),
            )
            row = cur.fetchone()
            assert row is not None
            conn.commit()
            return int(row[0])

    def search(
        self,
        query: str,
        k: int = 5,
        namespace: str | None = None,
    ) -> list[MemoryHit]:
        """KNN search by cosine distance."""
        ns = namespace or self._default_namespace
        query_vec = self._embedder(query)

        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, namespace, text, metadata, created_at,
                       embedding <=> %s::vector AS distance
                FROM memories
                WHERE namespace = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query_vec, ns, query_vec, k),
            )
            rows = cur.fetchall()

        return [
            MemoryHit(
                id=int(r[0]),
                namespace=r[1],
                text=r[2],
                metadata=r[3] if isinstance(r[3], dict) else {},
                created_at=r[4],
                distance=float(r[5]),
            )
            for r in rows
        ]

    def delete(self, memory_id: int, *, namespace: str | None) -> bool:
        """Delete by id, scoped to ``namespace``. True if a row was removed.

        ``namespace`` is required and has no default on purpose. Row ids are
        sequential (``BIGSERIAL``) and are shown to callers — the built-in
        recall tool renders hits as ``- [id] text`` — so an unscoped delete
        lets any tenant remove another tenant's memories by guessing an
        integer. Pass the same namespace you'd pass to ``search``:

            store.delete(mid, namespace=f"user:{email}")

        A namespace mismatch removes nothing and returns ``False``, which
        callers should surface as not-found (not as "wrong tenant" — that
        would leak the existence of the row).

        Pass ``namespace=None`` to delete across every namespace. That's the
        old behavior, kept for admin/prune scripts, and now has to be said
        out loud rather than being what you get by forgetting an argument.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            if namespace is None:
                cur.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
            else:
                cur.execute(
                    "DELETE FROM memories WHERE id = %s AND namespace = %s",
                    (memory_id, namespace),
                )
            conn.commit()
            return cur.rowcount > 0

    def list(
        self,
        namespace: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryHit]:
        """Most-recent-first list. Convenience; not embedded.

        ``offset`` pages through a namespace larger than one call: the ids
        are stable, so ``list(ns, limit=100, offset=100)`` is the second page.
        """
        ns = namespace or self._default_namespace
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, namespace, text, metadata, created_at
                FROM memories
                WHERE namespace = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                (ns, limit, offset),
            )
            rows = cur.fetchall()
        return [
            MemoryHit(
                id=int(r[0]),
                namespace=r[1],
                text=r[2],
                metadata=r[3] if isinstance(r[3], dict) else {},
                created_at=r[4],
                distance=0.0,
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Default embedder
# ---------------------------------------------------------------------------


def _default_embedder() -> Embedder:
    """Pick a sensible default: Bedrock Titan v2 if configured, else raise."""
    model_id = os.environ.get("STRANDS_PG_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
    provider = os.environ.get("STRANDS_PG_EMBED_PROVIDER", "bedrock")

    if provider == "bedrock":
        return _bedrock_embedder(model_id)
    raise RuntimeError(
        f"Unknown STRANDS_PG_EMBED_PROVIDER={provider!r}. "
        "Pass embedder=... explicitly or set provider to 'bedrock'."
    )


def _bedrock_embedder(model_id: str) -> Embedder:
    """Bedrock embedding via boto3. Titan v2 returns 1024-dim by default.

    Client is created lazily on first embed() call so app import doesn't
    require AWS creds just to boot /health.
    """
    client_holder: dict[str, Any] = {}

    def embed(text: str) -> list[float]:
        import json

        if "client" not in client_holder:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError(
                    "boto3 is required for the default Bedrock embedder. "
                    "Install with `pip install strands-pg[bedrock]` or "
                    "pass embedder=... yourself."
                ) from exc
            region = os.environ.get("AWS_REGION", "us-east-1")
            client_holder["client"] = boto3.client("bedrock-runtime", region_name=region)

        body = json.dumps({"inputText": text})
        resp = client_holder["client"].invoke_model(modelId=model_id, body=body)
        payload = json.loads(resp["body"].read())
        return list(payload["embedding"])

    return embed
