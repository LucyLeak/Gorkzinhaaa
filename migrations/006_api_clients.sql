CREATE TABLE IF NOT EXISTS api_clients (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    scopes TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_api_clients_active ON api_clients(id) WHERE revoked_at IS NULL;
