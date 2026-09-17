-- Scheduled and watch tasks: a saved prompt the backend runs on its own on a
-- cron schedule, once at a set time, or as a watch that only reports when
-- something changed. See backend/scheduling/.

CREATE TABLE scheduled_tasks (
    task_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    -- May contain {tarih}, {hafta}, {ay}, {son_calisma}; see scheduling/prompts.py.
    prompt TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'multi',
    deep_research INTEGER NOT NULL DEFAULT 0,
    skill_ids_json TEXT NOT NULL DEFAULT '[]',
    -- 'cron' | 'once'
    trigger_kind TEXT NOT NULL,
    cron_expr TEXT,
    run_at REAL,
    timezone TEXT NOT NULL DEFAULT 'Europe/Istanbul',
    -- NULL for a plain scheduled task; see scheduling/watch.py.
    watch_json TEXT,
    -- Per-URL content hashes from the last url_hash check.
    watch_state_json TEXT NOT NULL DEFAULT '{}',
    include_previous_summary INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    paused_reason TEXT,
    next_run_at REAL,
    last_run_at REAL,
    last_job_id TEXT,
    last_status TEXT,
    last_result_summary TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX idx_scheduled_tasks_due ON scheduled_tasks(enabled, next_run_at);
CREATE INDEX idx_scheduled_tasks_workspace ON scheduled_tasks(workspace_id, created_at DESC);

CREATE TABLE scheduled_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES scheduled_tasks(task_id) ON DELETE CASCADE,
    job_id TEXT,
    conversation_id TEXT,
    -- 'schedule' | 'catch_up' | 'manual' | 'retry'
    trigger TEXT NOT NULL,
    scheduled_for REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    -- running | done | partial | failed | error | cancelled | interrupted
    -- | skipped_overlap | missed | no_change
    status TEXT NOT NULL,
    -- Watch tasks: 1 changed, 0 unchanged, NULL not a watch / first baseline.
    change_detected INTEGER,
    change_summary TEXT,
    retry_of TEXT,
    error TEXT,
    seen_at REAL
);

CREATE INDEX idx_scheduled_runs_task ON scheduled_runs(task_id, scheduled_for DESC);
CREATE INDEX idx_scheduled_runs_status ON scheduled_runs(status);
