-- The workspace's document library: every file uploaded into any of its
-- conversations, searchable by the Knowledge Agent (knowledge_index.py).
--
-- conversation_files cannot serve for this: it is a per-conversation bag
-- capped at CONVERSATION_FILE_BAG_MAX that silently drops its oldest file.
-- Deleting a conversation keeps its files here on purpose; they leave the
-- library only when removed from it or when the workspace is deleted.
CREATE TABLE workspace_documents (
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    file_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    added_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, file_id)
);

CREATE INDEX idx_workspace_documents_file ON workspace_documents(file_id);

-- Backfill from job attachments, which (unlike conversation_files) never
-- dropped anything. Rows whose workspace no longer exists are skipped.
-- Non-object entries are replaced by '{}' before anything reads them:
-- json_each yields a string element as bare SQL text, and json_extract on
-- that raises "malformed JSON", which would abort this whole migration.
INSERT OR IGNORE INTO workspace_documents (workspace_id, file_id, filename, added_at)
SELECT workspace_id, file_id, COALESCE(filename, file_id), MIN(created_at)
FROM (
    SELECT
        jobs.workspace_id AS workspace_id,
        jobs.created_at AS created_at,
        json_extract(
            CASE WHEN attachment.type = 'object' THEN attachment.value ELSE '{}' END,
            '$.file_id'
        ) AS file_id,
        json_extract(
            CASE WHEN attachment.type = 'object' THEN attachment.value ELSE '{}' END,
            '$.filename'
        ) AS filename
    FROM jobs
    JOIN workspaces ON workspaces.workspace_id = jobs.workspace_id
    JOIN json_each(
        CASE WHEN json_valid(jobs.attachments_json) THEN jobs.attachments_json ELSE '[]' END
    ) AS attachment
)
WHERE file_id IS NOT NULL AND file_id != ''
GROUP BY workspace_id, file_id;

INSERT OR IGNORE INTO workspace_documents (workspace_id, file_id, filename, added_at)
SELECT conversations.workspace_id, conversation_files.file_id,
       conversation_files.filename, MIN(conversation_files.added_at)
FROM conversation_files
JOIN conversations ON conversations.conversation_id = conversation_files.conversation_id
JOIN workspaces ON workspaces.workspace_id = conversations.workspace_id
GROUP BY conversations.workspace_id, conversation_files.file_id;
