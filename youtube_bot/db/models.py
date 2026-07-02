from __future__ import annotations

from typing import Any

from youtube_bot.db.pool import Database


INIT_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS usuarios (
    id BIGSERIAL PRIMARY KEY,
    youtube_id TEXT UNIQUE NOT NULL,
    nome TEXT,
    primeiro_contato TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_interacoes INTEGER NOT NULL DEFAULT 0,
    pontos INTEGER NOT NULL DEFAULT 0,
    ultimo_cerebro TEXT,
    sucesso_cerebro_a INTEGER NOT NULL DEFAULT 0,
    sucesso_cerebro_b INTEGER NOT NULL DEFAULT 0,
    falhas_cerebro_a INTEGER NOT NULL DEFAULT 0,
    falhas_cerebro_b INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mensagens (
    id BIGSERIAL PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    conteudo TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
    tipo TEXT NOT NULL CHECK (tipo IN ('comment', 'live'))
);

CREATE TABLE IF NOT EXISTS respostas_geradas (
    id BIGSERIAL PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    mensagem_original TEXT NOT NULL,
    resposta_gerada TEXT NOT NULL,
    cerebro_utilizado TEXT NOT NULL,
    aprovada BOOLEAN NOT NULL,
    motivo_rejeicao TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memorias_semanticas (
    id BIGSERIAL PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    embedding vector(1536) NOT NULL,
    texto_resumido TEXT NOT NULL,
    tipo TEXT NOT NULL CHECK (tipo IN ('fato', 'contexto', 'episodio')),
    versao_anterior_id BIGINT REFERENCES memorias_semanticas(id) ON DELETE SET NULL,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memorias_usuario
    ON memorias_semanticas(usuario_id);

CREATE INDEX IF NOT EXISTS idx_memorias_embedding
    ON memorias_semanticas USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE TABLE IF NOT EXISTS configuracoes_cerebro (
    nome_cerebro TEXT PRIMARY KEY,
    prompt_base TEXT NOT NULL,
    peso_prioridade REAL NOT NULL DEFAULT 1.0,
    ultima_atualizacao TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tts_solicitacoes (
    id BIGSERIAL PRIMARY KEY,
    usuario_id BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    texto_original TEXT NOT NULL,
    texto_falado TEXT NOT NULL,
    audio_url TEXT,
    status TEXT NOT NULL DEFAULT 'pendente' CHECK (status IN ('pendente', 'processando', 'concluido', 'erro', 'reproduzido')),
    erro TEXT,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    concluido_em TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mensagens_timestamp
    ON mensagens(timestamp);

CREATE INDEX IF NOT EXISTS idx_respostas_timestamp
    ON respostas_geradas(timestamp);

CREATE INDEX IF NOT EXISTS idx_tts_criado_em
    ON tts_solicitacoes(criado_em);

CREATE INDEX IF NOT EXISTS idx_memorias_criado_em
    ON memorias_semanticas(criado_em);
"""


async def initialize_schema(db: Database) -> None:
    await db.execute(INIT_SQL)


async def upsert_user(db: Database, youtube_id: str, nome: str | None) -> dict[str, Any]:
    row = await db.fetchrow(
        """
        INSERT INTO usuarios (youtube_id, nome, total_interacoes)
        VALUES ($1, $2, 1)
        ON CONFLICT (youtube_id)
        DO UPDATE SET
            nome = COALESCE(EXCLUDED.nome, usuarios.nome),
            total_interacoes = usuarios.total_interacoes + 1
        RETURNING *
        """,
        youtube_id,
        nome,
    )
    return dict(row) if row else {}


async def get_user_history(db: Database, youtube_id: str) -> dict[str, Any]:
    row = await db.fetchrow("SELECT * FROM usuarios WHERE youtube_id = $1", youtube_id)
    return dict(row) if row else {}


async def insert_message(db: Database, user_id: int, content: str, message_type: str) -> int:
    return await db.fetchval(
        """
        INSERT INTO mensagens (usuario_id, conteudo, tipo)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        user_id,
        content,
        message_type,
    )


async def insert_generated_response(
    db: Database,
    user_id: int,
    original: str,
    answer: str,
    brain_name: str,
    approved: bool,
    rejection_reason: str | None,
) -> int:
    return await db.fetchval(
        """
        INSERT INTO respostas_geradas (
            usuario_id, mensagem_original, resposta_gerada, cerebro_utilizado,
            aprovada, motivo_rejeicao
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        user_id,
        original,
        answer,
        brain_name,
        approved,
        rejection_reason,
    )


async def update_brain_outcome(
    db: Database, user_id: int, brain_name: str, approved: bool
) -> None:
    success_column = "sucesso_cerebro_a" if brain_name == "cerebro_a" else "sucesso_cerebro_b"
    fail_column = "falhas_cerebro_a" if brain_name == "cerebro_a" else "falhas_cerebro_b"

    if approved:
        await db.execute(
            f"""
            UPDATE usuarios
            SET {success_column} = {success_column} + 1,
                {fail_column} = 0,
                ultimo_cerebro = $2
            WHERE id = $1
            """,
            user_id,
            brain_name,
        )
    else:
        await db.execute(
            f"""
            UPDATE usuarios
            SET {fail_column} = {fail_column} + 1,
                ultimo_cerebro = $2
            WHERE id = $1
            """,
            user_id,
            brain_name,
        )


async def add_points(db: Database, user_id: int, points: int) -> int:
    return await db.fetchval(
        """
        UPDATE usuarios
        SET pontos = pontos + $2
        WHERE id = $1
        RETURNING pontos
        """,
        user_id,
        points,
    )


async def insert_tts_request(
    db: Database,
    user_id: int,
    texto_original: str,
    texto_falado: str,
) -> int:
    return await db.fetchval(
        """
        INSERT INTO tts_solicitacoes (usuario_id, texto_original, texto_falado)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        user_id,
        texto_original,
        texto_falado,
    )


async def update_tts_status(
    db: Database,
    tts_id: int,
    status: str,
    audio_url: str | None = None,
    erro: str | None = None,
) -> None:
    await db.execute(
        """
        UPDATE tts_solicitacoes
        SET status = $2,
            audio_url = COALESCE($3, audio_url),
            erro = $4,
            concluido_em = CASE WHEN $2 IN ('concluido', 'erro') THEN now() ELSE concluido_em END
        WHERE id = $1
        """,
        tts_id,
        status,
        audio_url,
        erro,
    )


async def get_tts_request(db: Database, tts_id: int) -> dict[str, object] | None:
    row = await db.fetchrow(
        "SELECT * FROM tts_solicitacoes WHERE id = $1",
        tts_id,
    )
    return dict(row) if row else None


async def get_last_tts_time(db: Database, user_id: int) -> str | None:
    """Retorna o timestamp da ultima solicitacao TTS concluida (ou ja reproduzida) do usuario, ou None."""
    row = await db.fetchrow(
        """
        SELECT criado_em FROM tts_solicitacoes
        WHERE usuario_id = $1 AND status IN ('concluido', 'reproduzido')
        ORDER BY criado_em DESC
        LIMIT 1
        """,
        user_id,
    )
    return row["criado_em"].isoformat() if row else None


async def get_pending_tts(db: Database, limit: int = 10) -> list[dict[str, object]]:
    """Retorna TTS concluidos que ainda nao foram reproduzidos."""
    rows = await db.fetch(
        """
        SELECT t.id, t.texto_falado, t.audio_url, t.criado_em, u.nome AS autor
        FROM tts_solicitacoes t
        JOIN usuarios u ON u.id = t.usuario_id
        WHERE t.status = 'concluido'
        ORDER BY t.criado_em ASC
        LIMIT $1
        """,
        limit,
    )
    return [dict(r) for r in rows]


async def mark_tts_reproduzido(db: Database, tts_id: int) -> None:
    """Marca um TTS como reproduzido."""
    await db.execute(
        "UPDATE tts_solicitacoes SET status = 'reproduzido' WHERE id = $1",
        tts_id,
    )


async def cleanup_old_data(db: Database, retention_days: int) -> dict[str, int]:
    """Remove registros mais antigos que retention_days dias.
    Retorna um dicionario com a contagem de linhas deletadas por tabela."""
    deleted: dict[str, int] = {}

    # Mensagens antigas
    result = await db.fetchval(
        """
        WITH deleted AS (
            DELETE FROM mensagens
            WHERE timestamp < now() - make_interval(days => $1)
            RETURNING id
        )
        SELECT count(*) FROM deleted
        """,
        retention_days,
    )
    deleted["mensagens"] = int(result or 0)

    # Respostas geradas antigas
    result = await db.fetchval(
        """
        WITH deleted AS (
            DELETE FROM respostas_geradas
            WHERE timestamp < now() - make_interval(days => $1)
            RETURNING id
        )
        SELECT count(*) FROM deleted
        """,
        retention_days,
    )
    deleted["respostas_geradas"] = int(result or 0)

    # TTS solicitacoes antigas (concluidas ou com erro)
    result = await db.fetchval(
        """
        WITH deleted AS (
            DELETE FROM tts_solicitacoes
            WHERE criado_em < now() - make_interval(days => $1)
              AND status IN ('concluido', 'erro')
            RETURNING id
        )
        SELECT count(*) FROM deleted
        """,
        retention_days,
    )
    deleted["tts_solicitacoes"] = int(result or 0)

    # Memorias semanticas antigas (apenas episodios, fatos e contexto ficam)
    result = await db.fetchval(
        """
        WITH deleted AS (
            DELETE FROM memorias_semanticas
            WHERE criado_em < now() - make_interval(days => $1)
              AND tipo = 'episodio'
            RETURNING id
        )
        SELECT count(*) FROM deleted
        """,
        retention_days,
    )
    deleted["memorias_semanticas"] = int(result or 0)

    return deleted
