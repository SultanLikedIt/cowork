-- Phase 1: jobs, per-task/tool-call detail, and manually-added agents survive
-- a backend restart. workspace_id columns are included now (nullable, no FK
-- yet) so phase 2 (conversation continuity) can add a `workspaces` table
-- and start populating them without another jobs/manual_workers migration.

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    workspace_id TEXT,
    topic TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    planning_fallback INTEGER NOT NULL DEFAULT 0,
    planning_errors_json TEXT NOT NULL DEFAULT '[]',
    result TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    duration_seconds REAL,
    model_provider TEXT,
    model_name TEXT,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    workspace_json TEXT,
    usage_json TEXT NOT NULL DEFAULT '{}',
    cancellation_requested INTEGER NOT NULL DEFAULT 0,
    cancellation_note TEXT
);

CREATE INDEX idx_jobs_created_at ON jobs(created_at DESC);
CREATE INDEX idx_jobs_status ON jobs(status);

CREATE TABLE job_tasks (
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    assigned_agent TEXT,
    dependencies_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'queued',
    output TEXT,
    PRIMARY KEY (job_id, task_id)
);

CREATE TABLE job_tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error TEXT
);

CREATE INDEX idx_job_tool_calls_job_id ON job_tool_calls(job_id);

CREATE TABLE manual_workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    description_provided INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
