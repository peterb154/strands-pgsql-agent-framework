"""Look up a per-user identity by email for system-prompt assembly.

This is a camping-db-local helper today. Once proven, promote to
``strands_pg.identity`` as the framework's ``PgIdentity`` primitive.
"""

from __future__ import annotations

from strands_pg._pool import get_pool


def load_identity_by_email(email: str) -> str | None:
    """Return the identity body for this email, or None if unmapped."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.body
            FROM identity_emails ie
            JOIN identities i ON i.user_id = ie.user_id
            WHERE ie.email = %s
            """,
            (email,),
        )
        row = cur.fetchone()
    return row[0] if row else None
