"""One-shot ingest: pull camping-db domain data into Postgres.

Imports camps from legacy SQLite and parcel_services from the legacy JSON.
Identity files are NOT ingested here — the framework's ``PgIdentity.seed_from_dir``
runs at app boot against ``camping-db/identities/*.md``.

Run once against a fresh database:

    # from inside the agent container (legacy data mounted at /legacy):
    docker exec camping-db-agent-1 python /app/scripts/ingest.py --source /legacy

    # or from host against a running stack:
    STRANDS_PG_DSN=postgresql://strands:strands@localhost:5433/strands \
        python scripts/ingest.py --source /path/to/old/camping-db/data
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ingest")


CAMP_COLS = [
    "camp_id", "camp", "name", "state", "town", "type",
    "lat", "lon", "elev", "devel", "sites",
    "hookups", "toilets", "water", "showers", "dump", "pets",
    "fee", "season", "rv_length", "phone", "url", "url_confirmed",
    "directions", "reservations", "comments", "nforg", "ra_number",
    "air_mi_from_town", "dir_from_town", "upd", "data_date",
]

SQLITE_COL_MAP = {"date": "data_date"}  # `date` is awkward in PG.


def ingest_camps(sqlite_path: Path, pg_dsn: str) -> int:
    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row
    rows = src.execute("SELECT * FROM camps").fetchall()
    log.info("loaded %d rows from sqlite", len(rows))

    insert_cols = [*CAMP_COLS, "location", "search_text"]
    placeholders = ", ".join(
        "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography"
        if c == "location"
        else "%s"
        for c in insert_cols
    )
    sql = f"""
        INSERT INTO camps ({", ".join(insert_cols)})
        VALUES ({placeholders})
        ON CONFLICT (camp_id) DO NOTHING
    """

    n = 0
    with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE camps")
        for r in rows:
            record = {SQLITE_COL_MAP.get(k, k): r[k] for k in r.keys()}  # noqa: SIM118
            values: list = [record.get(c) for c in CAMP_COLS]
            values.extend([record.get("lon"), record.get("lat")])
            blob = " ".join(
                str(record.get(k) or "")
                for k in ("camp", "town", "state", "directions", "comments")
            ).strip()
            values.append(blob)
            cur.execute(sql, values)
            n += 1
        conn.commit()
    log.info("inserted %d camps", n)
    return n


def ingest_parcel_services(json_path: Path, pg_dsn: str) -> int:
    data = json.loads(json_path.read_text())
    with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE parcel_services")
        for key, row in data.items():
            extras = {k: v for k, v in row.items() if k not in {"name", "url"}}
            cur.execute(
                """
                INSERT INTO parcel_services (key, name, url, metadata)
                VALUES (%s, %s, %s, %s)
                """,
                (key, row["name"], row["url"], Jsonb(extras)),
            )
        conn.commit()
    log.info("inserted %d parcel services", len(data))
    return len(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest legacy camping-db data into Postgres.")
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Path to the legacy data/ dir (camping.db + parcel_services.json).",
    )
    parser.add_argument("--dsn", default=None, help="Postgres DSN; falls back to STRANDS_PG_DSN.")
    args = parser.parse_args()

    import os

    dsn = args.dsn or os.environ.get("STRANDS_PG_DSN")
    if not dsn:
        log.error("no DSN: pass --dsn or set STRANDS_PG_DSN")
        return 2

    ingest_camps(args.source / "camping.db", dsn)
    ingest_parcel_services(args.source / "parcel_services.json", dsn)
    log.info("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
