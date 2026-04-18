"""Domain tools for camping-db on strands_pg.

These run SQL against the ``camps`` table directly. PostGIS does the spatial
filtering (``ST_DWithin`` on geography), tsvector does full-text, pg_trgm does
fuzzy name matching.
"""

from __future__ import annotations

from strands import tool

from strands_pg._pool import get_pool

MILES_TO_METERS = 1609.344


@tool
def search_camps(
    lat: float | None = None,
    lon: float | None = None,
    radius_miles: float = 50.0,
    state: str | None = None,
    camp_type: str | None = None,
    free_only: bool = False,
    water: str | None = None,
    development_level: int | None = None,
    text_query: str | None = None,
    limit: int = 20,
) -> str:
    """Search for campsites.

    Args:
        lat: Latitude for location-based search.
        lon: Longitude for location-based search.
        radius_miles: Search radius in miles (default 50).
        state: Two-letter state code (e.g. KS, MT, OR).
        camp_type: Site type: NF, BLM, COE, CP, SP, SF.
        free_only: If true, only return free/no-fee sites.
        water: Water filter: DW (drinking) or NW (no water).
        development_level: Max development level (2=primitive, 5=developed).
        text_query: Full-text search on name, town, directions, comments.
        limit: Max results to return (default 20).

    Returns:
        Formatted list of matching campsites with key details.
    """
    where: list[str] = []
    params: list = []

    if state:
        where.append("state = %s")
        params.append(state.upper())
    if camp_type:
        where.append("type = %s")
        params.append(camp_type.upper())
    if free_only:
        where.append("(fee IS NULL OR fee = '' OR fee = 'N$')")
    if water:
        where.append("water = %s")
        params.append(water.upper())
    if development_level is not None:
        where.append("devel <= %s")
        params.append(development_level)
    if text_query:
        where.append("search_tsv @@ plainto_tsquery('english', %s)")
        params.append(text_query)

    if lat is not None and lon is not None:
        where.append(
            "ST_DWithin(location, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)"
        )
        params.extend([lon, lat, radius_miles * MILES_TO_METERS])
        distance_expr = (
            "ST_Distance(location, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) "
            "/ %s AS distance_miles"
        )
        params_dist = [lon, lat, MILES_TO_METERS]
        order_by = "distance_miles ASC"
    else:
        distance_expr = "NULL::double precision AS distance_miles"
        params_dist = []
        order_by = "camp_id ASC"

    sql = f"""
        SELECT camp_id, camp, state, town, type, fee, water, devel, season,
               directions, comments, lat, lon, {distance_expr}
        FROM camps
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY {order_by}
        LIMIT %s
    """
    # Param order: distance_expr params first (they appear in SELECT), then WHERE, then LIMIT.
    final_params = [*params_dist, *params, limit]

    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, final_params)
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]

    if not rows:
        return "No campsites found matching those criteria."

    results = [dict(zip(cols, r, strict=True)) for r in rows]
    lines = [f"Found {len(results)} campsites:"]
    for r in results:
        dist = f" ({r['distance_miles']:.1f} mi)" if r.get("distance_miles") is not None else ""
        fee = r.get("fee") or "free"
        water_str = r.get("water") or "?"
        dev = r.get("devel") if r.get("devel") is not None else "?"
        season = r.get("season") or "unknown season"
        lines.append(
            f"- [{r['camp_id']}] {r['camp']}, {r['state']}{dist} "
            f"| type={r['type']} fee={fee} water={water_str} devel={dev} "
            f"| season: {season}"
        )
        if r.get("directions"):
            lines.append(f"  Directions: {str(r['directions'])[:150]}")
        if r.get("comments"):
            lines.append(f"  Notes: {str(r['comments'])[:150]}")
    return "\n".join(lines)


@tool
def get_campsite(camp_id: int) -> str:
    """Get detailed information about a specific campsite by ID.

    Args:
        camp_id: Integer campsite id (from search_camps results).
    """
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT camp_id, camp, state, town, type, fee, water, devel, season,
                   directions, comments, reservations, url, phone, sites, elev,
                   toilets, showers, dump, pets, hookups, rv_length, lat, lon
            FROM camps WHERE camp_id = %s
            """,
            (camp_id,),
        )
        row = cur.fetchone()
        if row is None:
            return f"No campsite found with id {camp_id}."
        cols = [d.name for d in cur.description]
    r = dict(zip(cols, row, strict=True))

    def line(label: str, value) -> str | None:
        return f"  {label}: {value}" if value not in (None, "", 0) else None

    out = [f"[{r['camp_id']}] {r['camp']} — {r.get('town') or '?'}, {r['state']}"]
    details = [
        line("Type", r.get("type")),
        line("Fee", r.get("fee") or "free"),
        line("Water", r.get("water")),
        line("Development", r.get("devel")),
        line("Season", r.get("season")),
        line("Sites", r.get("sites")),
        line("Elev (ft)", r.get("elev")),
        line("Toilets", r.get("toilets")),
        line("Showers", r.get("showers")),
        line("Hookups", r.get("hookups")),
        line("Pets", r.get("pets")),
        line("RV length", r.get("rv_length")),
        line("Dump", r.get("dump")),
        line("Reservations", r.get("reservations")),
        line("Phone", r.get("phone")),
        line("URL", r.get("url")),
        line("Coords", f"{r.get('lat')}, {r.get('lon')}"),
        line("Directions", r.get("directions")),
        line("Notes", r.get("comments")),
    ]
    out.extend(d for d in details if d)
    return "\n".join(out)
