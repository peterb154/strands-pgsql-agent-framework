-- 103_api_schema.sql — isolate agent-owned tables from PostGIS noise.
--
-- PostGIS installs ~250 ST_* functions into public. If PostgREST is pointed at
-- public, the auto-generated OpenAPI doc enumerates all of them (as unreachable
-- /rpc/* endpoints) and buries our real tables in the noise.
--
-- The standard PostgREST pattern is to put user-exposed tables in a dedicated
-- schema and aim PostgREST at that. PostGIS stays in public, invisible to the
-- API. Framework tables (sessions, session_messages, memories, prompts,
-- identities, identity_emails) also stay in public — they're never exposed
-- via PostgREST anyway, and moving them would churn the session manager code.

CREATE SCHEMA IF NOT EXISTS api;

-- Move the agent-owned tables. ALTER TABLE SET SCHEMA carries grants, indexes,
-- generated columns, and the tsvector with it.
ALTER TABLE IF EXISTS public.camps            SET SCHEMA api;
ALTER TABLE IF EXISTS public.parcel_services  SET SCHEMA api;

-- Make unqualified FROM clauses in tool SQL resolve to api.* without code
-- changes. Applies to new connections opened by the strands role.
ALTER ROLE strands SET search_path = api, public;

-- PostgREST's role still needs USAGE on the new schema. SELECT grants moved
-- with the tables via SET SCHEMA, so no need to re-grant them.
GRANT USAGE ON SCHEMA api TO web_anon;

-- Tell PostgREST to reload its schema cache now that tables have moved.
-- PostgREST listens on the 'pgrst' channel; NOTIFY is cheap and idempotent.
-- Without this, a running PostgREST container keeps its old empty cache
-- until restarted.
NOTIFY pgrst, 'reload schema';

-- And auto-reload on any future DDL so we don't re-discover this one.
-- Wrapped in DO to survive reruns against an already-created trigger.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_event_trigger WHERE evtname = 'pgrst_watch') THEN
        CREATE OR REPLACE FUNCTION public.pgrst_watch() RETURNS event_trigger
        LANGUAGE plpgsql AS $fn$
        BEGIN
            NOTIFY pgrst, 'reload schema';
        END;
        $fn$;

        CREATE EVENT TRIGGER pgrst_watch ON ddl_command_end
            EXECUTE FUNCTION public.pgrst_watch();
    END IF;
END
$$;
