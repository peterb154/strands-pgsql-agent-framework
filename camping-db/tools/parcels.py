"""Parcel lookup: who owns the land at lat/lon.

Workflow:
 1. Reverse-geocode via OSM Nominatim to resolve county + state.
 2. Look up the county's ArcGIS service URL in the ``parcel_services`` table.
 3. Query that service at the point; extract owner/acres/address heuristically
    (field names vary county to county).

If the county is not in the registry, returns an actionable hint so the agent
can add it via a subsequent tool call (web_search + an INSERT into
parcel_services). The old camping-db used run_python + a JSON file for that
path — on strands_pg it's a one-liner SQL INSERT.
"""

from __future__ import annotations

from typing import Any

import httpx
from strands import tool

from strands_pg._pool import get_pool

NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "strands-pg/camping-db (+https://github.com/peterb154/strands-pgsql-agent-framework)"


@tool
def parcel_lookup(lat: float, lon: float) -> str:
    """Look up the private land owner at given GPS coordinates.

    Queries county ArcGIS parcel services to find the property owner, acreage,
    address, etc. Works for counties in the parcel_services registry.

    Args:
        lat: Latitude of the location.
        lon: Longitude of the location.
    """
    county, state = _reverse_geocode(lat, lon)
    if not county or not state:
        return f"Could not determine county/state for ({lat}, {lon})."

    key = f"{state}_{county}"
    service = _load_service(key)
    if service is None:
        return (
            f"County: {county} County, {state}\n"
            f"Registry key: {key}\n"
            f"This county is not yet in the parcel_services registry.\n\n"
            f"To add it: use web_search to find the county's ArcGIS "
            f"FeatureServer URL, then INSERT it:\n"
            f"  INSERT INTO parcel_services (key, name, url) VALUES\n"
            f"    ('{key}', '{county} County, {state}', 'THE_URL');"
        )

    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = httpx.get(f"{service['url']}/query", params=params, timeout=15)
    except httpx.TimeoutException:
        return "Parcel lookup timed out."
    except httpx.HTTPError as exc:
        return f"Parcel lookup failed: {exc}"

    if resp.status_code != 200:
        return f"Parcel lookup failed: HTTP {resp.status_code}"

    features = resp.json().get("features", [])
    if not features:
        return f"No parcel found at ({lat}, {lon}) in {county} County, {state}."

    attrs = features[0].get("attributes", {})
    return _format_parcel(attrs, lat, lon, county, state)


# ---------------------------------------------------------------------------


def _reverse_geocode(lat: float, lon: float) -> tuple[str | None, str | None]:
    try:
        resp = httpx.get(
            NOMINATIM_REVERSE_URL,
            params={"lat": lat, "lon": lon, "format": "json"},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
    except httpx.HTTPError:
        return None, None
    if resp.status_code != 200:
        return None, None
    addr = resp.json().get("address", {})
    county = (addr.get("county", "") or addr.get("city", "")).replace(" County", "").strip()
    state_iso = addr.get("ISO3166-2-lvl4", "")
    state = state_iso[-2:] if state_iso else ""
    return (county or None), (state or None)


def _load_service(key: str) -> dict[str, str] | None:
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, url FROM parcel_services WHERE key = %s", (key,))
        row = cur.fetchone()
    if row is None:
        return None
    return {"name": row[0], "url": row[1]}


def _find_attr(attrs: dict[str, Any], patterns: list[str]) -> str:
    for pattern in patterns:
        pl = pattern.lower()
        for k, v in attrs.items():
            if pl in k.lower() and v is not None and str(v).strip():
                return str(v).strip()
    return ""


def _format_parcel(
    attrs: dict[str, Any], lat: float, lon: float, county: str, state: str
) -> str:
    owner = _find_attr(attrs, ["owner", "owname", "ownr"])
    owner2 = _find_attr(attrs, ["owner2", "owname2"])
    address = _find_attr(attrs, ["mail_add", "owadr1", "owneraddress", "mailadd"])
    city = _find_attr(attrs, ["mail_city", "ownercity"])
    state_addr = _find_attr(attrs, ["mail_st", "ownerstate"])
    zipcode = _find_attr(attrs, ["mail_zip", "ownerzip"])
    acres = _find_attr(attrs, ["acre", "calcacre", "gisacre", "legalacre"])
    parcel_id = _find_attr(attrs, ["parcel", "pin", "pid", "apn"])
    legal = _find_attr(attrs, ["legal", "lgldesc"])
    value = _find_attr(attrs, ["propertyvalue", "avtk", "sumofavtk"])
    zoning = _find_attr(attrs, ["zoning", "usedesc", "classcodedesc", "avclasdesc"])
    site_addr = _find_attr(attrs, ["qryaddress", "siteaddr", "address"])

    lines = [f"Parcel at ({lat}, {lon}) — {county} County, {state}:"]
    if owner:
        lines.append(f"  Owner: {owner}")
    if owner2:
        lines.append(f"  Owner 2: {owner2}")
    if site_addr:
        lines.append(f"  Site Address: {site_addr}")
    if address:
        mail = address
        if city:
            mail += f", {city}"
        if state_addr:
            mail += f", {state_addr}"
        if zipcode:
            mail += f" {zipcode}"
        lines.append(f"  Mailing Address: {mail}")
    if acres:
        lines.append(f"  Acres: {acres}")
    if parcel_id:
        lines.append(f"  Parcel ID: {parcel_id}")
    if value:
        lines.append(f"  Property Value: ${value}")
    if zoning:
        lines.append(f"  Zoning/Use: {zoning}")
    if legal:
        lines.append(f"  Legal: {legal}")
    return "\n".join(lines)
