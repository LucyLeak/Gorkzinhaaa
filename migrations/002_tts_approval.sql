-- =====================================================
-- 002: Adiciona suporte a aprovacao e fila TTS do admin
-- =====================================================

-- Adiciona a coluna "aprovado" se ela não existir
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'tts_solicitacoes'
          AND column_name = 'aprovado'
    ) THEN
        ALTER TABLE tts_solicitacoes ADD COLUMN aprovado BOOLEAN DEFAULT NULL;
    END IF;
END $$;

-- Índice para listagem da fila TTS em tempo real no admin panel
CREATE INDEX IF NOT EXISTS idx_tts_status_criado
    ON tts_solicitacoes(status, criado_em);

-- Índice para consultar TTS pendentes de aprovação
CREATE INDEX IF NOT EXISTS idx_tts_aprovado
    ON tts_solicitacoes(aprovado, criado_em)
    WHERE status = 'concluido' AND aprovado IS NULL;
