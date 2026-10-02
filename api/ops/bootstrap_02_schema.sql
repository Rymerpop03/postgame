-- Per-database bootstrap: extension, schema ownership, and the grants that keep the
-- application role out of DDL. Run as a superuser, once per database.
--
--   psql -U postgres -d postgame      -f ops/bootstrap_02_schema.sql
--   psql -U postgres -d postgame_test -f ops/bootstrap_02_schema.sql

\set ON_ERROR_STOP on

-- The only extension the schema needs. Case-insensitive username and email lookup without
-- lower() on every comparison -- which matters because a functional index is easy to forget and
-- a sequential scan on the login path is a denial of service waiting to happen.
CREATE EXTENSION IF NOT EXISTS citext;

-- Postgres 15+ already removes CREATE on public from PUBLIC, but being explicit costs nothing
-- and survives a restore onto an older server.
REVOKE ALL ON SCHEMA public FROM PUBLIC;

ALTER SCHEMA public OWNER TO postgame_migrate;

-- USAGE lets the app resolve names in the schema. It does not let it create anything.
GRANT USAGE ON SCHEMA public TO postgame_app;

-- Rows, not structure. Note the absence of any DDL privilege and of CREATE on the schema:
-- together those are what make "the running app cannot change its own schema" true rather
-- than merely intended.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO postgame_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO postgame_app;

-- And for everything a future migration creates, so nobody has to remember to re-grant.
ALTER DEFAULT PRIVILEGES FOR ROLE postgame_migrate IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO postgame_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgame_migrate IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO postgame_app;

\echo ''
\echo 'schema ready in this database: public owned by postgame_migrate, postgame_app has DML only'
