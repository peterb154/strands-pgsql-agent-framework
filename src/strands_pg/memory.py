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

    def update(self, memory_id: int, text: str, *, namespace: str) -> bool:
        """Replace a memory's text in place, re-embedding. True if updated.

        Preserves ``id``, ``created_at`` and ``metadata``; replaces ``text``
        and ``embedding`` together — rewriting one without the other leaves
        ``search`` matching the old wording while returning the new text.

        ``namespace`` is required and scoped exactly as in ``delete``: an
        unscoped update would let one tenant overwrite another's memories.
        A mismatch changes nothing and returns ``False``.
        """
        # Embed before checking out a connection so a slow embedder doesn't
        # hold one. Costs a wasted embedding on a bad id — cheaper than a
        # check-then-update round trip, which would race anyway.
        embedding = self._embedder(text)

        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE memories
                SET text = %s, embedding = %s::vector
                WHERE id = %s AND namespace = %s
                """,
                (text, embedding, memory_id, namespace),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete(self, memory_id: int, *, namespace: str) -> bool:
        """Delete by id within ``namespace``. True if a row was removed.

        ``namespace`` is required and cannot be ``None``: ids are sequential
        and shown to callers, so an unscoped delete lets any tenant remove
        another's memories by guessing an integer (#3). Unlike
        ``add``/``search``/``list``, ``None`` isn't "the default namespace"
        here — it would have to mean "every namespace", so it's rejected
        rather than overloaded. See ``delete_across_namespaces``.

        A mismatch removes nothing and returns ``False``; surface that as
        not-found, not "wrong tenant", which would leak the row's existence.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM memories WHERE id = %s AND namespace = %s",
                (memory_id, namespace),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_across_namespaces(self, memory_id: int) -> bool:
        """Delete by id in ANY namespace. True if a row was removed.

        For admin and prune scripts that legitimately span the whole table.
        Named at length so it's obvious at the call site and greppable in
        review. Anything serving a user request wants ``delete``.
        """
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
            conn.commit()
            return cur.rowcount > 0

    def list(
        self,
        namespace: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryHit]:
        """Most-recent-first list. Convenience; not embedded.

        ``offset`` pages a namespace larger than one call. The ``id``
        tiebreaker makes the ordering total — ``created_at`` alone isn't,
        since bulk inserts share a timestamp — so pages don't overlap or
        drop rows. Offset paging, not keyset: a concurrent insert shifts
        later pages by one.
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
