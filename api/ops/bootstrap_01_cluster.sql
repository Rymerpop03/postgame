-- Cluster-level bootstrap: roles and the database. Run once, as a superuser.
--
--   psql -U postgres -d postgres -f ops/bootstrap_01_cluster.sql
--
-- Then ops/bootstrap_02_schema.sql against the new database. Two files rather than one so
-- neither needs a psql \connect, which would prompt for the superuser password a second time.
--
-- BACKEND-PLAN.md Phase 2: "Two database roles: a migration role that owns the schema, and an
-- application role that cannot ALTER, DROP, or CREATE. The running app cannot change its own
-- schema."
--
-- That separation is the whole point. If the application role can run DDL then an SQL injection
-- bug anywhere -- or a leaked application credential -- escalates from reading rows to dropping
-- tables. Splitting the roles stops the blast radius of the app's own credentials at the data
-- it is supposed to touch.
--
-- The passwords below are development-only and deliberately obvious: they are committed, so
-- they are not secrets. Staging and production generate their own and supply them through the
-- environment.

\set ON_ERROR_STOP on

-- Owns the schema. Alembic connects as this role and nothing else does.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgame_migrate') THEN
    CREATE ROLE postgame_migrate LOGIN PASSWORD 'dev_migrate_only' CREATEDB;
  END IF;
END $$;

-- The running application. No DDL, ever.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgame_app') THEN
    CREATE ROLE postgame_app LOGIN PASSWORD 'dev_app_only';
  END IF;
END $$;

-- Bound how long a single statement may hold resources. A runaway query on a shared database
-- is an availability problem, and 30s is far beyond anything this application legitimately
-- does -- every query in the plan is indexed and paginated.
ALTER ROLE postgame_app SET statement_timeout = '30s';
ALTER ROLE postgame_app SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE postgame_app SET lock_timeout = '10s';

-- Migrations legitimately take longer than a request does.
ALTER ROLE postgame_migrate SET statement_timeout = '300s';
ALTER ROLE postgame_migrate SET lock_timeout = '30s';

-- CREATE DATABASE cannot run inside a transaction block, hence \gexec rather than a DO block.
SELECT 'CREATE DATABASE postgame OWNER postgame_migrate ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'postgame')
\gexec

-- Same database, used only by the test suite, so a test run can never touch development data.
SELECT 'CREATE DATABASE postgame_test OWNER postgame_migrate ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'postgame_test')
\gexec

-- Nobody should be able to connect to a database just because it exists.
REVOKE ALL ON DATABASE postgame FROM PUBLIC;
REVOKE ALL ON DATABASE postgame_test FROM PUBLIC;
GRANT CONNECT ON DATABASE postgame TO postgame_app, postgame_migrate;
GRANT CONNECT ON DATABASE postgame_test TO postgame_app, postgame_migrate;

\echo ''
\echo 'roles postgame_migrate (owner) and postgame_app (DML only) ready'
\echo 'databases postgame and postgame_test created'
\echo 'now run: psql -U postgres -d postgame      -f ops/bootstrap_02_schema.sql'
\echo 'and:     psql -U postgres -d postgame_test -f ops/bootstrap_02_schema.sql'
