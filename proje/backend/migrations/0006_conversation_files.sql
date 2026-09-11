-- Thread-scoped upload bag. Jobs remain isolated workspaces, but follow-up
-- turns need to re-bind files the user already uploaded in this conversation
-- without the client resending file_ids (the composer clears attachments
-- after each submit).
CREATE TABLE conversation_files (
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    file_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    added_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, file_id)
);

CREATE INDEX idx_conversation_files_added
    ON conversation_files(conversation_id, added_at);
