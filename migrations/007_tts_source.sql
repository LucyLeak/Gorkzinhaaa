ALTER TABLE tts_solicitacoes
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'admin';

CREATE INDEX IF NOT EXISTS idx_tts_source
    ON tts_solicitacoes(source);

ALTER TABLE api_clients
    ADD COLUMN IF NOT EXISTS key_prefix TEXT;

CREATE INDEX IF NOT EXISTS idx_api_clients_key_prefix ON api_clients(key_prefix)
    WHERE revoked_at IS NULL;
