-- Task chains: a task that runs after another task finishes, optionally only
-- under a condition, receiving that run's report and files; plus files kept
-- on a task and attached to every run. See backend/scheduling/chains.py.

ALTER TABLE scheduled_tasks ADD COLUMN upstream_task_id TEXT
    REFERENCES scheduled_tasks(task_id) ON DELETE SET NULL;
-- {"on": "success" | "change" | "always"}
ALTER TABLE scheduled_tasks ADD COLUMN condition_json TEXT;
ALTER TABLE scheduled_tasks ADD COLUMN pass_upstream_result INTEGER NOT NULL DEFAULT 1;
ALTER TABLE scheduled_tasks ADD COLUMN pass_upstream_files INTEGER NOT NULL DEFAULT 1;
-- [{"file_id": "...", "filename": "..."}]
ALTER TABLE scheduled_tasks ADD COLUMN attached_files_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE scheduled_runs ADD COLUMN parent_run_id TEXT;
-- Frozen input of a chained run: {"context": "...", "file_ids": [...], "notes": [...]}
ALTER TABLE scheduled_runs ADD COLUMN input_json TEXT;

CREATE INDEX idx_scheduled_tasks_upstream ON scheduled_tasks(upstream_task_id);
-- One upstream run triggers each downstream task at most once.
CREATE UNIQUE INDEX idx_scheduled_runs_parent
    ON scheduled_runs(task_id, parent_run_id) WHERE parent_run_id IS NOT NULL;
