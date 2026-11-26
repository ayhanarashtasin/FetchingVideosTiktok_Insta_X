-- 002_media_cache: pre-extraction Telegram file_id cache for completed public non-Story posts.
-- Holds only Telegram file_ids and display metadata; never credentials, CDN URLs, or Stories.

CREATE TABLE media_cache (
    platform       TEXT NOT NULL,
    post_id        TEXT NOT NULL,
    delivery_mode  TEXT NOT NULL CHECK (delivery_mode IN ('media', 'original')),
    expected_count INTEGER,
    items_json     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    PRIMARY KEY (platform, post_id, delivery_mode)
);
CREATE INDEX idx_media_cache_expiry ON media_cache (expires_at);
