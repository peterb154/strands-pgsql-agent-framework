-- 003_camps.sql — camping-db domain table.
-- Faithful port of the SQLite camps schema, with PostGIS + trigram + tsvector
-- replacing Haversine-in-Python + FTS5.

CREATE TABLE IF NOT EXISTS camps (
    camp_id        INTEGER PRIMARY KEY,
    camp           TEXT,
    name           TEXT,
    state          TEXT,
    town           TEXT,
    type           TEXT,        -- NF, BLM, COE, CP, SP, SF, ...
    lat            DOUBLE PRECISION,
    lon            DOUBLE PRECISION,
    location       geography(Point, 4326),   -- ST_MakePoint(lon, lat)
    elev           INTEGER,
    devel          INTEGER,     -- 2=primitive ... 5=developed
    sites          INTEGER,
    hookups        TEXT,
    toilets        TEXT,
    water          TEXT,        -- DW = drinking, NW = no water
    showers        TEXT,
    dump           TEXT,
    pets           TEXT,
    fee            TEXT,        -- blank/N$ = free, L$ = low cost
    season         TEXT,
    rv_length      INTEGER,
    phone          TEXT,
    url            TEXT,
    url_confirmed  INTEGER,
    directions     TEXT,
    reservations   TEXT,
    comments       TEXT,
    nforg          TEXT,
    ra_number      TEXT,
    air_mi_from_town REAL,
    dir_from_town  TEXT,
    upd            TEXT,
    data_date      TEXT,
    search_text    TEXT,        -- denormalized blob for fuzzy search
    search_tsv     tsvector GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(camp, '') || ' ' ||
            coalesce(town, '') || ' ' ||
            coalesce(state, '') || ' ' ||
            coalesce(directions, '') || ' ' ||
            coalesce(comments, '')
        )
    ) STORED
);

CREATE INDEX IF NOT EXISTS camps_state_idx ON camps (state);
CREATE INDEX IF NOT EXISTS camps_type_idx  ON camps (type);
CREATE INDEX IF NOT EXISTS camps_fee_idx   ON camps (fee);
CREATE INDEX IF NOT EXISTS camps_loc_gix   ON camps USING gist (location);
CREATE INDEX IF NOT EXISTS camps_search_gix ON camps USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS camps_text_trgm ON camps USING gin (search_text gin_trgm_ops);
