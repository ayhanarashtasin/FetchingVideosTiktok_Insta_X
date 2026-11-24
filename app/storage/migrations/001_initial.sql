-- 001_initial: jobs, preferences, per-batch send outcomes.
-- Holds ONLY operational metadata: the submitted URL (needed for retry / original-file
-- callbacks) is purged with its job after the TTL. No media, no signed CDN URLs, no credentials.

CREATE TABLE jobs (
    id                 TEXT PRIMARY KEY,
    owner_id           INTEGER NOT NULL,
    chat_id            INTEGER NOT NULL,
    origin_message_id  INTEGER NOT NULL,
    original_url       TEXT NOT NULL,
    platform           TEXT NOT NULL,
    delivery_mode      TEXT NOT NULL CHECK (delivery_mode IN ('media', 'original')),
    state              TEXT NOT NULL,
    -- '' for a job created by a message; the callback-query id for a Retry / Original-files
    -- job. Together with the columns below it makes duplicate Telegram events idempotent
    -- without suppressing an intentional repeat (a new message has a new origin_message_id).
    request_key        TEXT NOT NULL DEFAULT '',
    expected_count     INTEGER,
    delivered_count    INTEGER NOT NULL DEFAULT 0,
    result_message_ids TEXT NOT NULL DEFAULT '[]',
    error_code         TEXT,
    parent_job_id      TEXT,
    status_message_id  INTEGER,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    UNIQUE (chat_id, origin_message_id, delivery_mode, request_key)
);
CREATE INDEX idx_jobs_expiry ON jobs (expires_at);
CREATE INDEX idx_jobs_owner_state ON jobs (owner_id, state);

CREATE TABLE preferences (
    owner_id      INTEGER PRIMARY KEY,
    delivery_mode TEXT NOT NULL DEFAULT 'media' CHECK (delivery_mode IN ('media', 'original'))
);

-- One row per Telegram send batch, written as soon as the outcome of that batch is known, so a
-- crash mid-album still leaves an accurate record of what was confirmed.
CREATE TABLE job_deliveries (
    job_id      TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    batch_index INTEGER NOT NULL,
    positions   TEXT NOT NULL,                       -- JSON list of source item positions
    outcome     TEXT NOT NULL CHECK (outcome IN ('sent', 'failed', 'unknown')),
    message_ids TEXT NOT NULL DEFAULT '[]',          -- JSON list of confirmed Telegram message ids
    error_code  TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (job_id, batch_index)
);
