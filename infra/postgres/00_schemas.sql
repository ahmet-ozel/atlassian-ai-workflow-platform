-- Postgres schema bootstrap for the platform services.
-- Runs first (alphabetic order) on initial container start via
-- /docker-entrypoint-initdb.d. Idempotent.

CREATE SCHEMA IF NOT EXISTS automation;
CREATE SCHEMA IF NOT EXISTS assistant;
CREATE SCHEMA IF NOT EXISTS auth;
CREATE SCHEMA IF NOT EXISTS shared;
CREATE SCHEMA IF NOT EXISTS temporal;
