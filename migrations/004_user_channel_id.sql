ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS youtube_channel_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_youtube_channel_id
    ON usuarios(youtube_channel_id) WHERE youtube_channel_id IS NOT NULL;
