"""Geocoding + land ownership. External HTTP calls only — no DB involved.

- ``geocode(place_name)``: name -> lat/lon via OpenStreetMap Nominatim.
- ``land_ownership(lat, lon)``: BLM Surface Management Agency dataset, with a
  follow-up to the USFS service for forest name when applicable.
"""

from __future__ import annotations

import httpx
from strands import tool

NOMINATIM_URL = "https://nominatim.openstreetmap.org"
BLM_SMA_URL = (
    "https://gis.blm.gov/arcgis/rest/services"
    "/lands/BLM_Natl_SMA_LimitedScale/MapServer/1/query"
)
USFS_URL = (
    "https://apps.fs.usda.gov/arcx/rest/services"
    "/EDW/EDW_BasicOwnership_01/MapServer/0/query"
)
USER_AGENT = "strands-pg/camping-db (+https://github.com/brianpeterson/strands-pgsql-agent-framework)"

_AGENCY_NAMES = {
    "BLM": "Bureau of Land Management",
    "NPS": "National Park Service",
    "USFS": "US Forest Service",
    "FWS": "US Fish & Wildlife Service",
    "BIA": "Bureau of Indian Affairs",
    "USBR": "Bureau of Reclamation",
    "ST": "State Land",
    "LG": "Local Government",
    "PVT": "Private Land",
    "UND": "Undetermined",
}


@tool
def geocode(place_name: str) -> str:
    """Convert a place name to latitude and longitude coordinates.

    Use this when the user mentions a location by name and you need coordinates
    for a radius search.

    Args:
        place_name: Place name to geocode (e.g. "Mountain West KS", "Moab UT").
    """
    try:
        resp = httpx.get(
            f"{NOMINATIM_URL}/search",
            params={"q": place_name, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
    except httpx.TimeoutException:
        return f"Geocoding timed out for '{place_name}'. Try again."
    except httpx.HTTPError as exc:
        return f"Geocoding failed: {exc}"
    if resp.status_code != 200 or not resp.json():
        return f"Could not geocode '{place_name}'. Try a more specific name."
    result = resp.json()[0]
    return (
        f"Location: {result.get('display_name', place_name)}\n"
        f"Latitude: {result['lat']}\n"
        f"Longitude: {result['lon']}"
    )


@tool
def land_ownership(lat: float, lon: float) -> str:
    """Look up who owns/manages the land at given GPS coordinates.

    Queries the BLM Surface Management Agency database (covers all US land —
    federal, state, local, private). For USFS hits, enriches with forest name.

    Args:
        lat: Latitude of the location.
        lon: Longitude of the location.
    """
    params = {
        "geometry": f"{lon},{lat}",  # ArcGIS wants x,y (lon,lat)
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "ADMIN_DEPT_CODE,ADMIN_AGENCY_CODE,ADMIN_UNIT_NAME,ADMIN_ST",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = httpx.get(BLM_SMA_URL, params=params, timeout=15)
    except httpx.TimeoutException:
        return "Land ownership lookup timed out. Try again."
    except httpx.HTTPError as exc:
        return f"Land ownership lookup failed: {exc}"

    if resp.status_code != 200:
        return f"Land ownership lookup failed: HTTP {resp.status_code}"

    features = resp.json().get("features", [])
    if not features:
        return f"No land ownership data found for ({lat}, {lon})."

    attrs = features[0].get("attributes", {})
    agency = attrs.get("ADMIN_AGENCY_CODE", "Unknown")
    dept = attrs.get("ADMIN_DEPT_CODE", "Unknown")
    unit = attrs.get("ADMIN_UNIT_NAME") or "N/A"
    state = attrs.get("ADMIN_ST", "Unknown")

    lines = [
        f"Land ownership at ({lat}, {lon}):",
        f"  Agency: {_AGENCY_NAMES.get(agency, agency)} ({agency})",
        f"  Department: {dept}",
        f"  Unit: {unit}",
        f"  State: {state}",
    ]

    if agency in ("BLM", "USFS"):
        lines.append("  Camping: Dispersed camping generally ALLOWED (free, no permit)")
    elif agency == "NPS":
        lines.append("  Camping: National Park — dispersed camping NOT allowed")
    elif agency == "PVT":
        lines.append("  Camping: PRIVATE land — do not camp without owner permission")
    elif agency == "ST":
        lines.append("  Camping: State land — check state-specific rules")

    if agency == "USFS":
        forest = _usfs_forest_name(lat, lon)
        if forest:
            lines.append(f"  Forest: {forest}")

    return "\n".join(lines)


def _usfs_forest_name(lat: float, lon: float) -> str | None:
    """Best-effort enrichment; swallow errors — this is optional info."""
    try:
        resp = httpx.get(
            USFS_URL,
            params={
                "geometry": f"{lon},{lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "FORESTNAME",
                "returnGeometry": "false",
                "f": "json",
            },
            timeout=15,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    features = resp.json().get("features", [])
    if not features:
        return None
    return features[0].get("attributes", {}).get("FORESTNAME") or None
