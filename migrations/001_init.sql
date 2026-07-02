-- =====================================================
-- 1. HABILITAR EXTENSÕES NECESSÁRIAS
-- =====================================================
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm; -- opcional, para buscas textuais

-- =====================================================
-- 2. TABELA usuarios
-- =====================================================
CREATE TABLE IF NOT EXISTS usuarios (
    id BIGSERIAL PRIMARY KEY,
    youtube_id TEXT UNIQUE NOT NULL,
    nome TEXT,
    primeiro_contato TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_interacoes INTEGER NOT NULL DEFAULT 0,
    pontos INTEGER NOT NULL DEFAULT 0,
    ultimo_cerebro TEXT CHECK (ultimo_cerebro IN ('cerebro_a', 'cerebro_b')), -- restrição de domínio
    sucesso_cerebro_a INTEGER NOT NULL DEFAULT 0,
    sucesso_cerebro_b INTEGER NOT NULL DEFAULT 0,
    falhas_cerebro_a INTEGER NOT NULL DEFAULT 0,
    falhas_cerebro_b INTEGER NOT NULL DEFAULT 0,
    -- Campo para controle de humor do usuário (opcional)
    ultimo_humor TEXT CHECK (ultimo_humor IN ('alegre', 'neutro', 'irritado')) DEFAULT 'neutro',
    -- Timestamp para cache de contexto (evita reprocessar sempre)
    ultima_atualizacao_contexto TIMESTAMPTZ DEFAULT now()
);

-- =====================================================
-- 3. TABELA mensagens
-- =====================================================
CREATE TABLE IF NOT EXISTS mensagens (
    id BIGSERIAL PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    conteudo TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    tipo TEXT NOT NULL CHECK (tipo IN ('comment', 'live'))
);
-- Índice para consultas por usuário + tempo (comum em relatórios)
CREATE INDEX IF NOT EXISTS idx_mensagens_usuario_timestamp 
    ON mensagens(usuario_id, timestamp DESC);

-- =====================================================
-- 4. TABELA respostas_geradas
-- =====================================================
CREATE TABLE IF NOT EXISTS respostas_geradas (
    id BIGSERIAL PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    mensagem_original TEXT NOT NULL,
    resposta_gerada TEXT NOT NULL,
    cerebro_utilizado TEXT NOT NULL CHECK (cerebro_utilizado IN ('cerebro_a', 'cerebro_b')),
    aprovada BOOLEAN NOT NULL,
    motivo_rejeicao TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Número de tentativas de reparo (útil para análise)
    tentativas_repair INTEGER DEFAULT 0
);
-- Índice para consultar respostas aprovadas por usuário (útil para contexto)
CREATE INDEX IF NOT EXISTS idx_respostas_usuario_aprovada 
    ON respostas_geradas(usuario_id, aprovada, timestamp DESC);

-- =====================================================
-- 5. TABELA memorias_semanticas (com pgvector)
-- =====================================================
CREATE TABLE IF NOT EXISTS memorias_semanticas (
    id BIGSERIAL PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    embedding vector(1536) NOT NULL,  -- tamanho padrão do text-embedding-3-small
    texto_resumido TEXT NOT NULL,
    tipo TEXT NOT NULL CHECK (tipo IN ('fato', 'contexto', 'episodio')),
    versao_anterior_id BIGINT REFERENCES memorias_semanticas(id) ON DELETE SET NULL,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Metadados adicionais
    relevancia REAL DEFAULT 1.0,        -- peso para busca
    ultimo_acesso TIMESTAMPTZ           -- para estratégias de cache
);
-- Índice para busca por usuário
CREATE INDEX IF NOT EXISTS idx_memorias_usuario ON memorias_semanticas(usuario_id);

-- Índice vetorial usando IVFFlat (excelente para Neon DB)
-- Ajuste o número de "lists" conforme o volume de dados (ex: sqrt(n_registros))
CREATE INDEX IF NOT EXISTS idx_memorias_embedding 
    ON memorias_semanticas USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Índice para consultas por tipo e data (consolidação)
CREATE INDEX IF NOT EXISTS idx_memorias_tipo_criado 
    ON memorias_semanticas(tipo, criado_em);

-- =====================================================
-- 6. TABELA configuracoes_cerebro (com verificação de coluna)
-- =====================================================
CREATE TABLE IF NOT EXISTS configuracoes_cerebro (
    nome_cerebro TEXT PRIMARY KEY CHECK (nome_cerebro IN ('cerebro_a', 'cerebro_b')),
    prompt_base TEXT NOT NULL,
    peso_prioridade REAL NOT NULL DEFAULT 1.0,
    ultima_atualizacao TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Adiciona a coluna "temperatura" se ela não existir
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'configuracoes_cerebro'
          AND column_name = 'temperatura'
    ) THEN
        ALTER TABLE configuracoes_cerebro ADD COLUMN temperatura REAL DEFAULT 0.7;
    END IF;
END $$;

-- Inserir configurações padrão (se não existirem)
INSERT INTO configuracoes_cerebro (nome_cerebro, prompt_base, peso_prioridade, temperatura)
VALUES 
    ('cerebro_a', 'Você é um assistente sério, analítico e educado. Responda de forma clara e objetiva.', 1.0, 0.3),
    ('cerebro_b', 'Você é um comediante inteligente, cheio de ironia e piadas. Seja criativo e divertido, mas nunca ofensivo.', 1.0, 0.9)
ON CONFLICT (nome_cerebro) DO UPDATE
SET 
    prompt_base = EXCLUDED.prompt_base,
    peso_prioridade = EXCLUDED.peso_prioridade,
    temperatura = EXCLUDED.temperatura,
    ultima_atualizacao = now()
WHERE configuracoes_cerebro.prompt_base IS DISTINCT FROM EXCLUDED.prompt_base
   OR configuracoes_cerebro.peso_prioridade IS DISTINCT FROM EXCLUDED.peso_prioridade
   OR configuracoes_cerebro.temperatura IS DISTINCT FROM EXCLUDED.temperatura;

-- =====================================================
-- 7. TABELA tts_solicitacoes (para futura integração)
-- =====================================================
CREATE TABLE IF NOT EXISTS tts_solicitacoes (
    id BIGSERIAL PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    texto_original TEXT NOT NULL,
    texto_falado TEXT NOT NULL,
    audio_url TEXT,
    status TEXT NOT NULL DEFAULT 'pendente' 
        CHECK (status IN ('pendente', 'processando', 'concluido', 'erro', 'reproduzido')),
    aprovado BOOLEAN DEFAULT NULL,
    erro TEXT,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    concluido_em TIMESTAMPTZ
);
-- Índices para consultas de limpeza
CREATE INDEX IF NOT EXISTS idx_tts_status_criado ON tts_solicitacoes(status, criado_em);

-- =====================================================
-- 8. TABELA para histórico de humor (opcional, mas útil)
-- =====================================================
CREATE TABLE IF NOT EXISTS historico_humor (
    id BIGSERIAL PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    humor TEXT NOT NULL CHECK (humor IN ('alegre', 'neutro', 'irritado')),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_historico_humor_usuario 
    ON historico_humor(usuario_id, timestamp DESC);

-- =====================================================
-- 9. ÍNDICES AUXILIARES PARA LIMPEZA E CONSULTAS
-- =====================================================
-- Para limpeza de dados antigos (ex: mensagens com mais de 1 ano)
CREATE INDEX IF NOT EXISTS idx_mensagens_timestamp ON mensagens(timestamp);
CREATE INDEX IF NOT EXISTS idx_respostas_timestamp ON respostas_geradas(timestamp);
CREATE INDEX IF NOT EXISTS idx_memorias_criado_em ON memorias_semanticas(criado_em);