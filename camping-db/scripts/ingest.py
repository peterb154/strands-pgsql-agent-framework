"""One-shot ingest: pull camping-db domain data into Postgres.

Reads from the legacy repo (SQLite + JSON + markdown) and writes to the
framework Postgres. Run once per fresh database:

    # from the camping-db/ directory, with the stack up:
    docker compose run --rm agent python scripts/ingest.py \
        --source /legacy/data

    # or from host against a running stack:
    STRANDS_PG_DSN=postgresql://strands:strands@localhost:5433/strands \
        python scripts/ingest.py --source /path/to/old/camping-db/data
"""

from __future__ import annotations

import argparse
import json
import logging
import re
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

# SQLite has a `date` column; rename to data_date for Postgres (date is reserved-ish).
SQLITE_COL_MAP = {"date": "data_date"}


def ingest_camps(sqlite_path: Path, pg_dsn: str) -> int:
    """Copy camps from SQLite to Postgres, including PostGIS point + search blob."""
    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row
    rows = src.execute("SELECT * FROM camps").fetchall()
    log.info("loaded %d rows from sqlite", len(rows))

    insert_cols = [*CAMP_COLS, "location", "search_text"]
    placeholders = ", ".join(
        # location is derived; search_text is a plain text blob
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
            record = {SQLITE_COL_MAP.get(k, k): r[k] for k in r.keys()}
            values: list = [record.get(c) for c in CAMP_COLS]
            # location: (lon, lat) for PostGIS
            lat = record.get("lat")
            lon = record.get("lon")
            values.extend([lon, lat])
            # search_text blob
            parts = [
                str(record.get(k) or "")
                for k in ("camp", "town", "state", "directions", "comments")
            ]
            values.append(" ".join(parts).strip())
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
            cur.execute(
                """
                INSERT INTO parcel_services (key, name, url, metadata)
                VALUES (%s, %s, %s, %s)
                """,
                (key, row["name"], row["url"], Jsonb({k: v for k, v in row.items() if k not in {"name", "url"}})),
            )
        conn.commit()
    log.info("inserted %d parcel services", len(data))
    return len(data)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML-ish frontmatter parser — we only care about a handful of keys."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    fm_raw, body = match.group(1), match.group(2)
    meta: dict = {}
    for line in fm_raw.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
        else:
            meta[k] = v
    return meta, body


def ingest_identities(identities_dir: Path, pg_dsn: str) -> int:
    count = 0
    with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE identity_emails")
        cur.execute("TRUNCATE identities CASCADE")
        for md in sorted(identities_dir.glob("*.md")):
            text = md.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            user_id = md.stem
            title = meta.get("title")
            tags = meta.get("tags", [])
            emails = meta.get("emails", [])
            metadata = {k: v for k, v in meta.items() if k not in {"title", "tags", "emails"}}
            cur.execute(
                """
                INSERT INTO identities (user_id, title, body, tags, metadata)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, title, body.strip(), tags, Jsonb(metadata)),
            )
            for email in emails:
                cur.execute(
                    "INSERT INTO identity_emails (email, user_id) VALUES (%s, %s)",
                    (email, user_id),
                )
            count += 1
        conn.commit()
    log.info("inserted %d identities", count)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest legacy camping-db data into Postgres.")
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Path to the legacy camping-db data/ directory "
        "(contains camping.db, parcel_services.json, identities/)",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="Postgres DSN; falls back to STRANDS_PG_DSN.",
    )
    args = parser.parse_args()

    import os

    dsn = args.dsn or os.environ.get("STRANDS_PG_DSN")
    if not dsn:
        log.error("no DSN: pass --dsn or set STRANDS_PG_DSN")
        return 2

    src = args.source
    ingest_camps(src / "camping.db", dsn)
    ingest_parcel_services(src / "parcel_services.json", dsn)
    ingest_identities(src / "identities", dsn)
    log.info("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
