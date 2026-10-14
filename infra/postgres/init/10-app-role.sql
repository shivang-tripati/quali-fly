-- Runs once, on first database initialisation (docker-entrypoint-initdb.d).
--
-- The official image always creates POSTGRES_USER as a SUPERUSER, and superusers
-- bypass Row Level Security even with FORCE ROW LEVEL SECURITY. If the
-- application connected as that role, every RLS policy would be silently inert.
--
-- So there are two roles:
--   * POSTGRES_USER  (quali_fly)      - superuser, owns the schema, runs Alembic
--   * APP_DB_ROLE    (quali_fly_app)  - NOSUPERUSER NOBYPASSRLS, used by the API
--
-- The API role is neither the table owner nor a superuser, so RLS applies to it.

\getenv app_db_role APP_DB_ROLE
\getenv app_db_password APP_DB_PASSWORD
\getenv app_db_name POSTGRES_DB
\getenv owner_role POSTGRES_USER

-- Create the login role if it does not exist yet. \gexec runs the generated SQL,
-- which avoids a DO $$ block: psql does not interpolate variables inside those.
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_db_role', :'app_db_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_db_role')
\gexec

-- Pin the security attributes every time, so an accidental ALTER ROLE is undone.
SELECT format(
    'ALTER ROLE %I NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION',
    :'app_db_role'
)
\gexec

SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', :'app_db_name', :'app_db_role')
\gexec

SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_db_role')
\gexec

-- Tables created by later migrations (as the owner) are automatically readable and
-- writable by the application role, and are RLS-protected because it is not the owner.
SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
    :'owner_role', :'app_db_role'
)
\gexec

SELECT format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
    'GRANT USAGE, SELECT ON SEQUENCES TO %I',
    :'owner_role', :'app_db_role'
)
\gexec
