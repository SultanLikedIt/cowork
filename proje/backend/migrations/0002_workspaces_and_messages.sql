-- Phase 2: workspaces become a real, named, long-lived entity, and
-- messages give each workspace a conversation transcript that gets
-- prepended as bounded context to new tasks submitted into it.

CREATE TABLE workspaces (
    workspace_id TEXT PRIMARY KEY,
    name TEXT,
    created_at REAL NOT NULL,
    last_active_at REAL NOT NULL
);

CREATE INDEX idx_workspaces_last_active ON workspaces(last_active_at DESC);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    job_id TEXT REFERENCES jobs(job_id) ON DELETE SET NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX idx_messages_workspace_created ON messages(workspace_id, created_at);
