CREATE TABLE IF NOT EXISTS youtube_quota_usage (
    usage_date DATE PRIMARY KEY,
    units_used INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
