-- A workspace is a persistent container; conversations are the independent
-- threads inside it. Existing data historically used workspace_id as the
-- thread identifier, so backfill one conversation per populated workspace.

CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    title TEXT,
    created_at REAL NOT NULL,
    last_active_at REAL NOT NULL
);

CREATE INDEX idx_conversations_workspace_active
    ON conversations(workspace_id, last_active_at DESC);

ALTER TABLE jobs ADD COLUMN conversation_id TEXT REFERENCES conversations(conversation_id) ON DELETE SET NULL;
ALTER TABLE messages ADD COLUMN conversation_id TEXT REFERENCES conversations(conversation_id) ON DELETE SET NULL;

INSERT INTO conversations (
    conversation_id, workspace_id, title, created_at, last_active_at
)
SELECT
    jobs.workspace_id,
    jobs.workspace_id,
    (
        SELECT first_job.topic
        FROM jobs AS first_job
        WHERE first_job.workspace_id = jobs.workspace_id
        ORDER BY first_job.created_at ASC
        LIMIT 1
    ),
    MIN(jobs.created_at),
    MAX(jobs.created_at)
FROM jobs
WHERE jobs.workspace_id IS NOT NULL
GROUP BY jobs.workspace_id;

UPDATE jobs
SET conversation_id = workspace_id
WHERE workspace_id IS NOT NULL;

UPDATE messages
SET conversation_id = workspace_id
WHERE workspace_id IS NOT NULL
  AND workspace_id IN (SELECT conversation_id FROM conversations);

CREATE INDEX idx_jobs_conversation_created
    ON jobs(conversation_id, created_at DESC);
CREATE INDEX idx_messages_conversation_created
    ON messages(conversation_id, created_at);
