-- 102_pgrst_role.sql — read-only role for the PostgREST sidecar.
--
-- The compose stack runs PostgREST as an optional third service pointed at
-- this role. Everything exposed to the outside world as auto-CRUD lives here.
-- Tables NOT granted below (sessions, session_messages, memories, identities,
-- identity_emails, prompts) are invisible to the PostgREST surface.
--
-- If you add new public tables that should be browsable, grant SELECT to
-- web_anon in a later migration.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'web_anon') THEN
        CREATE ROLE web_anon NOLOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO web_anon;

GRANT SELECT ON TABLE camps            TO web_anon;
GRANT SELECT ON TABLE parcel_services  TO web_anon;

-- strands (the superuser-ish app role) needs to be able to SET ROLE to web_anon
-- so PostgREST can switch into it per request.
GRANT web_anon TO strands;
