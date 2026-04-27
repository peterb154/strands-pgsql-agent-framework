"""Postgres-backed SessionManager for Strands.

Implements ``SessionRepository`` against Postgres and subclasses
``RepositorySessionManager`` — the same pattern ``FileSessionManager`` and
``S3SessionManager`` use. Messages, agent state, and session metadata are
stored as JSONB so Strands' own ``to_dict``/``from_dict`` can round-trip
without schema churn.

Tables (see ``migrations/001_init.sql``):

- ``sessions(session_id PK, data JSONB, ...)``
- ``session_agents(session_id, agent_id, data JSONB, ...)``
- ``session_messages(session_id, agent_id, message_id, data JSONB, ...)``
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg.types.json import Jsonb
from strands.session.repository_session_manager import RepositorySessionManager
from strands.session.session_repository import SessionRepository
from strands.types.exceptions import SessionException
from strands.types.session import Session, SessionAgent, SessionMessage

from strands_pg._pool import get_pool

logger = logging.getLogger(__name__)


@contextmanager
def session_lock(session_id: str, dsn: str | None = None) -> Iterator[None]:
    """Hold a Postgres advisory lock for the duration of an agent run.

    Strands' ``RepositorySessionManager`` computes the next ``message_id``
    in-memory from the prior turn's state and INSERTs into
    ``session_messages``. Two concurrent agent runs on the same session
    both compute the same next ``message_id`` and one loses the unique
    constraint, crashing the in-flight agent — and silently dropping
    whatever tool call was about to run, including a reply MCP.

    Wrapping the agent run in this context manager serializes
    same-session writes via ``pg_advisory_lock(hashtext(session_id))``.
    Postgres-level (not Python-level) so it works across processes and
    replicas. Different ``session_id`` values hash to different keys and
    don't contend.

    The lock is held by the connection for the lifetime of the ``with``
    block. Postgres releases session-level advisory locks automatically
    when the connection closes, so the explicit unlock here is hygiene,
    not correctness; a failed unlock is logged and swallowed rather than
    raising and masking the user's real exception.

    Scale note: this holds a pool connection for the full agent run
    (typically 30-60s of model + tool latency). At default psycopg pool
    size (~4), a handful of concurrent same-process requests can saturate
    the pool. Fine at single-user / rare-burst scale; revisit with a
    dedicated lock-only pool if it bites.
    """
    pool = get_pool(dsn)
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (session_id,))
        try:
            yield
        finally:
            try:
                conn.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))", (session_id,)
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "advisory unlock failed for session_id=%s; "
                    "Postgres will release on session close",
                    session_id,
                )


class PgSessionManager(RepositorySessionManager, SessionRepository):
    """Postgres-backed session manager for a single Strands agent session."""

    def __init__(
        self,
        session_id: str,
        dsn: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize with a session_id. Pool is created lazily from dsn or env."""
        self._pool = get_pool(dsn)
        super().__init__(session_id=session_id, session_repository=self, **kwargs)

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------

    def create_session(self, session: Session, **kwargs: Any) -> Session:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sessions (session_id, data)
                VALUES (%s, %s)
                ON CONFLICT (session_id) DO NOTHING
                """,
                (session.session_id, Jsonb(session.to_dict())),
            )
            if cur.rowcount == 0:
                raise SessionException(f"Session {session.session_id} already exists")
            conn.commit()
        return session

    def read_session(self, session_id: str, **kwargs: Any) -> Session | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT data FROM sessions WHERE session_id = %s", (session_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return Session.from_dict(_as_dict(row[0]))

    def delete_session(self, session_id: str, **kwargs: Any) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
            if cur.rowcount == 0:
                raise SessionException(f"Session {session_id} does not exist")
            conn.commit()

    # ------------------------------------------------------------------
    # agents
    # ------------------------------------------------------------------

    def create_agent(
        self, session_id: str, session_agent: SessionAgent, **kwargs: Any
    ) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO session_agents (session_id, agent_id, data)
                VALUES (%s, %s, %s)
                ON CONFLICT (session_id, agent_id) DO UPDATE
                  SET data = EXCLUDED.data,
                      updated_at = now()
                """,
                (session_id, session_agent.agent_id, Jsonb(session_agent.to_dict())),
            )
            conn.commit()

    def read_agent(
        self, session_id: str, agent_id: str, **kwargs: Any
    ) -> SessionAgent | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT data FROM session_agents
                WHERE session_id = %s AND agent_id = %s
                """,
                (session_id, agent_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return SessionAgent.from_dict(_as_dict(row[0]))

    def update_agent(
        self, session_id: str, session_agent: SessionAgent, **kwargs: Any
    ) -> None:
        previous = self.read_agent(session_id=session_id, agent_id=session_agent.agent_id)
        if previous is None:
            raise SessionException(
                f"Agent {session_agent.agent_id} in session {session_id} does not exist"
            )
        session_agent.created_at = previous.created_at
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE session_agents
                   SET data = %s, updated_at = now()
                 WHERE session_id = %s AND agent_id = %s
                """,
                (Jsonb(session_agent.to_dict()), session_id, session_agent.agent_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------

    def create_message(
        self,
        session_id: str,
        agent_id: str,
        session_message: SessionMessage,
        **kwargs: Any,
    ) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO session_messages
                    (session_id, agent_id, message_id, data)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    session_id,
                    agent_id,
                    session_message.message_id,
                    Jsonb(session_message.to_dict()),
                ),
            )
            conn.commit()

    def read_message(
        self,
        session_id: str,
        agent_id: str,
        message_id: int,
        **kwargs: Any,
    ) -> SessionMessage | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT data FROM session_messages
                WHERE session_id = %s AND agent_id = %s AND message_id = %s
                """,
                (session_id, agent_id, message_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return SessionMessage.from_dict(_as_dict(row[0]))

    def update_message(
        self,
        session_id: str,
        agent_id: str,
        session_message: SessionMessage,
        **kwargs: Any,
    ) -> None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE session_messages
                   SET data = %s, updated_at = now()
                 WHERE session_id = %s AND agent_id = %s AND message_id = %s
                """,
                (
                    Jsonb(session_message.to_dict()),
                    session_id,
                    agent_id,
                    session_message.message_id,
                ),
            )
            if cur.rowcount == 0:
                raise SessionException(
                    f"Message {session_message.message_id} not found "
                    f"(session={session_id}, agent={agent_id})"
                )
            conn.commit()

    def list_messages(
        self,
        session_id: str,
        agent_id: str,
        limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> list[SessionMessage]:
        sql = """
            SELECT data FROM session_messages
            WHERE session_id = %s AND agent_id = %s
            ORDER BY message_id ASC
            OFFSET %s
        """
        params: list[Any] = [session_id, agent_id, offset]
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)

        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [SessionMessage.from_dict(_as_dict(r[0])) for r in rows]


def _as_dict(value: Any) -> dict[str, Any]:
    """psycopg returns JSONB as dict; some adapters hand back str. Normalize."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise TypeError(f"unexpected JSONB payload type: {type(value)!r}")
