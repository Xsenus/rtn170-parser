-- Standalone global registry; PostgreSQL 14+; schema contract version 1.
CREATE SCHEMA IF NOT EXISTS rtn170_shared;
CREATE TABLE IF NOT EXISTS rtn170_shared.schema_version (version integer PRIMARY KEY CHECK(version=1));
INSERT INTO rtn170_shared.schema_version VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS rtn170_shared.crawl_runs (
    id uuid PRIMARY KEY, started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz, heartbeat_at timestamptz NOT NULL DEFAULT now(),
    state text NOT NULL CHECK(state IN ('running','paused','failed','complete')),
    phase text NOT NULL DEFAULT 'discovery' CHECK(phase IN ('discovery','details','complete')),
    next_page integer NOT NULL DEFAULT 1, expected_total integer,
    first_signature text, parser_version text NOT NULL, note text
);
CREATE TABLE IF NOT EXISTS rtn170_shared.crawl_pages (
    run_id uuid NOT NULL REFERENCES rtn170_shared.crawl_runs(id), page_number integer NOT NULL,
    first_record integer NOT NULL, last_record integer NOT NULL, total integer NOT NULL,
    signature text NOT NULL, fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id, page_number)
);
CREATE TABLE IF NOT EXISTS rtn170_shared.crawl_queue (
    run_id uuid NOT NULL REFERENCES rtn170_shared.crawl_runs(id), license_number text NOT NULL,
    source_url text NOT NULL, page_number integer NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done','skipped','error')),
    attempts integer NOT NULL DEFAULT 0, error text, checked_at timestamptz,
    PRIMARY KEY(run_id, license_number)
);
CREATE INDEX IF NOT EXISTS rtn170_queue_state ON rtn170_shared.crawl_queue(run_id,state,page_number);
CREATE TABLE IF NOT EXISTS rtn170_shared.licenses (
    id uuid PRIMARY KEY, source_key text NOT NULL DEFAULT 'gosnadzor_170fz' CHECK(source_key='gosnadzor_170fz'),
    licensee_full_name text NOT NULL,
    licensee_short_name text NOT NULL,
    licensee_address text NOT NULL,
    licensee_inn text NOT NULL,
    licensee_ogrn text NOT NULL,
    object_full_name text NOT NULL,
    object_short_name text NOT NULL,
    object_address text NOT NULL,
    object_inn text NOT NULL,
    object_ogrn text NOT NULL,
    license_number text NOT NULL,
    registered_on_raw text NOT NULL,
    valid_until_raw text NOT NULL,
    activity text NOT NULL,
    work_types_text text NOT NULL,
    activity_addresses_text text NOT NULL,
    source_status text NOT NULL,
    registered_on date, valid_until date, valid_indefinite boolean NOT NULL,
    source_url text NOT NULL, source_hash text NOT NULL,
    last_observed_status text NOT NULL,
    missing_from_latest_scan boolean NOT NULL DEFAULT false,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_confirmed_active_at timestamptz NOT NULL DEFAULT now(),
    last_status_check_at timestamptz NOT NULL DEFAULT now(),
    last_changed_at timestamptz NOT NULL DEFAULT now(),
    last_seen_run_id uuid NOT NULL REFERENCES rtn170_shared.crawl_runs(id),
    parser_version text NOT NULL,
    UNIQUE(source_key, license_number),
    CHECK(source_status='Действующая')
);
CREATE INDEX IF NOT EXISTS rtn170_licensee_inn ON rtn170_shared.licenses(licensee_inn);
CREATE INDEX IF NOT EXISTS rtn170_object_inn ON rtn170_shared.licenses(object_inn);
CREATE INDEX IF NOT EXISTS rtn170_valid_until ON rtn170_shared.licenses(valid_until);
CREATE TABLE IF NOT EXISTS rtn170_shared.license_work_items (
    license_id uuid NOT NULL REFERENCES rtn170_shared.licenses(id), ordinal integer NOT NULL,
    value text NOT NULL, PRIMARY KEY(license_id,ordinal)
);
CREATE TABLE IF NOT EXISTS rtn170_shared.license_activity_addresses (
    license_id uuid NOT NULL REFERENCES rtn170_shared.licenses(id), ordinal integer NOT NULL,
    value text NOT NULL, PRIMARY KEY(license_id,ordinal)
);
CREATE TABLE IF NOT EXISTS rtn170_shared.license_fields (
    license_id uuid NOT NULL REFERENCES rtn170_shared.licenses(id), ordinal integer NOT NULL,
    section text NOT NULL, label text NOT NULL, value_text text NOT NULL,
    value_html text NOT NULL, mapped_column text, PRIMARY KEY(license_id,ordinal)
);
CREATE TABLE IF NOT EXISTS rtn170_shared.license_revisions (
    id uuid PRIMARY KEY, license_id uuid NOT NULL REFERENCES rtn170_shared.licenses(id),
    source_hash text NOT NULL, parsed_data jsonb NOT NULL, html_gzip bytea NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT now(), parser_version text NOT NULL,
    UNIQUE(license_id,source_hash)
);
CREATE TABLE IF NOT EXISTS rtn170_shared.license_observations (
    run_id uuid NOT NULL REFERENCES rtn170_shared.crawl_runs(id), license_number text NOT NULL,
    observed_status text NOT NULL, source_url text NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(run_id,license_number)
);
CREATE OR REPLACE VIEW rtn170_shared.active_licenses AS
SELECT * FROM rtn170_shared.licenses
WHERE last_observed_status IN ('Действующая','Возобновлена') AND NOT missing_from_latest_scan;
REVOKE ALL ON SCHEMA rtn170_shared FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA rtn170_shared FROM PUBLIC;

