-- Câu 2C: Proposed PostgreSQL/Cloud time-series model (NOT auto-migrated).
-- Production needs an actual PostgreSQL deployment, credentials and ETL.
-- Do not execute this file against the SQLite demo database.

CREATE TABLE iot_bins (
    bin_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    is_priority BOOLEAN NOT NULL DEFAULT FALSE
);

-- Separate globally unique registry avoids false dedupe across time partitions.
CREATE TABLE received_message_ids (
    message_id TEXT PRIMARY KEY,
    first_received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE telemetry_series (
    message_id TEXT NOT NULL REFERENCES received_message_ids(message_id),
    bin_id TEXT NOT NULL REFERENCES iot_bins(bin_id),
    device_id TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    captured_at TIMESTAMPTZ,
    distance_cm DOUBLE PRECISION,
    fill_percent DOUBLE PRECISION,
    sensor_status TEXT NOT NULL,
    transport TEXT NOT NULL,
    PRIMARY KEY (received_at, message_id)
) PARTITION BY RANGE (received_at);

-- Example monthly partition. Automate partition creation/retention on Cloud.
CREATE TABLE telemetry_2026_11 PARTITION OF telemetry_series
    FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');

CREATE INDEX idx_telemetry_series_bin_received
    ON telemetry_series(bin_id, received_at DESC);

CREATE TABLE alert_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bin_id TEXT NOT NULL REFERENCES iot_bins(bin_id),
    event_type TEXT NOT NULL,
    details TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_cloud_alert_time ON alert_events(created_at DESC);

-- Unlike the running SQLite app, PostgreSQL version must implement an explicit
-- transactional insertion into received_message_ids first to preserve P2 ACK
-- and message_id idempotency. No PostgreSQL adapter is shipped in this phase.
