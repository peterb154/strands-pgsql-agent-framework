-- 004_parcel_services.sql — county parcel ArcGIS service registry.
-- Replaces the old data/parcel_services.json. Seeded by scripts/ingest.py.

CREATE TABLE IF NOT EXISTS parcel_services (
    key        TEXT PRIMARY KEY,          -- e.g., "KS_Pottawatomie"
    name       TEXT NOT NULL,             -- human label, e.g. "Pottawatomie County, KS"
    url        TEXT NOT NULL,             -- ArcGIS FeatureServer URL
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS parcel_services_name_idx ON parcel_services (name);
