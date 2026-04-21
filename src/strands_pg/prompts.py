"""Database-backed prompt store.

Prompts (soul.md, rules.md, any named prompt) live as rows in the ``prompts``
table. Agents read them at build time. They can be updated live via the
``/prompts/{name}`` API — no rebuild, no volume mount, no restart.

The first time an agent boots against a fresh database, ``seed_from_dir()``
copies ``<name>`` entries from ``./prompts/<name>.md`` files on disk. After
that, the DB is the source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from strands_pg._pool import get_pool

logger = logging.getLogger(__name__)


@dataclass
class Prompt:
    """One prompt stored in the DB."""

    name: str
    body: str


class PgPromptStore:
    """Read/write named prompts by key."""

    def __init__(self, dsn: str | None = None) -> None:
        self._pool = get_pool(dsn)

    def get(self, name: str) -> Prompt | None:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT name, body FROM prompts WHERE name = %s", (name,))
            row = cur.fetchone()
        if row is None:
            return None
        return Prompt(name=row[0], body=row[1])

    def _get_updated_at(self, name: str) -> datetime | None:
        """Internal: timestamp of the last write to this prompt row, or None
        if the row doesn't exist. Used by seed_from_dir to compare against
        the source file's mtime."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT updated_at FROM prompts WHERE name = %s", (name,))
            row = cur.fetchone()
        if row is None:
            return None
        ts = row[0]
        # psycopg returns naive or aware depending on driver config; normalize.
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)

    def put(self, name: str, body: str) -> Prompt:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO prompts (name, body)
                VALUES (%s, %s)
                ON CONFLICT (name) DO UPDATE
                  SET body = EXCLUDED.body,
                      updated_at = now()
                RETURNING name, body
                """,
                (name, body),
            )
            row = cur.fetchone()
            assert row is not None
            conn.commit()
        return Prompt(name=row[0], body=row[1])

    def list(self) -> list[Prompt]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT name, body FROM prompts ORDER BY name")
            rows = cur.fetchall()
        return [Prompt(name=r[0], body=r[1]) for r in rows]

    def delete(self, name: str) -> bool:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM prompts WHERE name = %s", (name,))
            conn.commit()
            return cur.rowcount > 0

    def seed_from_dir(self, directory: str | Path, *, overwrite: bool = False) -> list[str]:
        """Load every ``*.md`` file in ``directory`` into the prompts table.

        Semantics, per file:

        - Row missing → insert.
        - Row exists, file's mtime is NEWER than the row's ``updated_at`` →
          update. This makes the edit-on-disk → redeploy workflow "just
          work": a prompts file that changed between deploys wins, and the
          row's timestamp then leapfrogs the file until the next edit.
        - Row exists, file's mtime is OLDER or EQUAL → leave alone. Live
          tweaks made via ``put()`` (e.g. the ``/prompts`` API) survive
          container restarts because their ``updated_at`` will be newer
          than the baked-in file mtime.

        ``overwrite=True`` forces every file to be written regardless of
        timestamps — useful for tests or a deliberate "reset to disk".

        Returns the list of names that were written.
        """
        path = Path(directory)
        if not path.is_dir():
            return []

        written: list[str] = []
        for md in sorted(path.glob("*.md")):
            name = md.stem
            body = md.read_text(encoding="utf-8")
            if overwrite:
                self.put(name, body)
                written.append(name)
                continue
            existing_ts = self._get_updated_at(name)
            if existing_ts is None:
                self.put(name, body)
                written.append(name)
                continue
            file_mtime = datetime.fromtimestamp(md.stat().st_mtime, tz=UTC)
            if file_mtime > existing_ts:
                self.put(name, body)
                written.append(name)
        if written:
            logger.info("seeded prompts from %s: %s", path, ", ".join(written))
        return written

    def assemble(self, names: list[str], separator: str = "\n\n") -> str:
        """Concatenate named prompts in order. Missing names are skipped."""
        parts: list[str] = []
        for n in names:
            p = self.get(n)
            if p is not None:
                parts.append(p.body)
        return separator.join(parts).strip()
