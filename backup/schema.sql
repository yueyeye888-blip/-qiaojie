-- Schema reference only. No row data. Do not execute against a live database.

CREATE UNIQUE INDEX one_active_job ON jobs(desktop_id)
                    WHERE state IN ('queued','waking','preflight','running','waiting_heartbeat');

CREATE TABLE agents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    desktop_id TEXT UNIQUE,
                    display_name TEXT NOT NULL,
                    machine_id TEXT UNIQUE,
                    hostname TEXT,
                    tailscale_ip TEXT,
                    token_hash TEXT,
                    enrolled_at INTEGER,
                    last_seen INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    last_report TEXT NOT NULL DEFAULT '{}'
                );

CREATE TABLE cloud_inventory (
                    desktop_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    region TEXT NOT NULL, cloud_status TEXT NOT NULL, updated_at INTEGER NOT NULL
                );

CREATE TABLE enrollments (
                    token_hash TEXT PRIMARY KEY,
                    agent_id INTEGER NOT NULL REFERENCES agents(id),
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );

CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    event TEXT NOT NULL,
                    agent_id INTEGER,
                    details TEXT NOT NULL DEFAULT '{}'
                );

CREATE TABLE incidents (
                    incident_key TEXT PRIMARY KEY, agent_id INTEGER, level TEXT NOT NULL,
                    code TEXT NOT NULL, message TEXT NOT NULL,
                    opened_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, resolved_at INTEGER,
                    acknowledged_at INTEGER, notified INTEGER NOT NULL DEFAULT 0
                );

CREATE TABLE jobs (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, desktop_id TEXT NOT NULL,
                    operation TEXT NOT NULL, state TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
                    invoke_id TEXT, error_code TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    deadline INTEGER NOT NULL, expected_hash TEXT NOT NULL DEFAULT ''
                );

CREATE TABLE lease (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    session_id TEXT,
                    target_id INTEGER REFERENCES agents(id),
                    pending_target_id INTEGER REFERENCES agents(id),
                    generation INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'idle',
                    expires_at INTEGER,
                    updated_at INTEGER NOT NULL
                , release_grace_until INTEGER NOT NULL DEFAULT 0, reset_nonce TEXT, reset_error TEXT NOT NULL DEFAULT '');

CREATE TABLE notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                    due_at INTEGER NOT NULL, error_code TEXT NOT NULL DEFAULT ''
                );
