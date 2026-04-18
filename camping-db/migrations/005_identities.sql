-- 005_identities.sql — per-user context docs + email mapping.
-- identities.body is the full markdown profile that gets prepended to the
-- agent's system prompt for this user. One user -> many emails (e.g. personal
-- + ZOLEO satellite address) via identity_emails.

CREATE TABLE IF NOT EXISTS identities (
    user_id    TEXT PRIMARY KEY,     -- slug: 'alice_rider'
    title      TEXT,
    body       TEXT NOT NULL,
    tags       TEXT[] NOT NULL DEFAULT '{}',
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity_emails (
    email      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES identities(user_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS identity_emails_user_idx ON identity_emails (user_id);
