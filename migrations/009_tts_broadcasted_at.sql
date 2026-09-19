ALTER TABLE tts_solicitacoes
    ADD COLUMN IF NOT EXISTS broadcasted_at TIMESTAMPTZ;

-- Existing completed rows predate durable broadcast tracking and must not replay.
UPDATE tts_solicitacoes
SET broadcasted_at = COALESCE(concluido_em, criado_em, now())
WHERE status = 'concluido'
  AND broadcasted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tts_broadcast_pending
    ON tts_solicitacoes(status, broadcasted_at, criado_em);
