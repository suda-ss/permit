-- Durable background chat runs and active-run status.
-- Safe to apply to existing Permit Agent databases more than once.

CREATE TABLE IF NOT EXISTS permit_chat_runs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id  UUID NOT NULL REFERENCES permit_conversations(id) ON DELETE CASCADE,
    user_id          BIGINT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'completed', 'failed')),
    error            TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_permit_chat_runs_one_running
    ON permit_chat_runs (conversation_id) WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_permit_chat_runs_conversation
    ON permit_chat_runs (conversation_id, started_at DESC);
