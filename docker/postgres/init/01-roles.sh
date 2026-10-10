#!/bin/bash
# Runs once, on first start of an empty data volume. Creates two login roles:
#   p03_migrator - owns the tables; used only for migrations (can change structure)
#   p03_app      - the API, scheduler and MCP server; reads/writes rows, cannot create or drop tables
# Least privilege: a bug or injection in the app cannot drop or alter tables.
# audit_log will additionally lose UPDATE/DELETE for p03_app in its migration (append-only).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username postgres --dbname p03 <<SQL
CREATE ROLE p03_migrator LOGIN PASSWORD '${P03_MIGRATOR_PASSWORD}' NOSUPERUSER NOBYPASSRLS;
CREATE ROLE p03_app      LOGIN PASSWORD '${P03_APP_PASSWORD}'      NOSUPERUSER NOBYPASSRLS;

ALTER DATABASE p03 OWNER TO p03_migrator;
ALTER SCHEMA public OWNER TO p03_migrator;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO p03_app;

-- Tables that p03_migrator creates later automatically become readable/writable by the app.
ALTER DEFAULT PRIVILEGES FOR ROLE p03_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO p03_app;
ALTER DEFAULT PRIVILEGES FOR ROLE p03_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO p03_app;
SQL
