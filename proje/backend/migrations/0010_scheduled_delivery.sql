-- Email delivery of scheduled runs through Outlook (backend/outlook.py and
-- scheduling/delivery.py). A run's delivery is tried from the scheduler tick,
-- retried with backoff while the bridge or Outlook is unavailable.

-- {"to": [...], "cc": [...], "on": "success" | "change" | "always", "attach_files": true}
ALTER TABLE scheduled_tasks ADD COLUMN delivery_json TEXT;

-- NULL (no delivery) | pending | sent | failed
ALTER TABLE scheduled_runs ADD COLUMN delivery_status TEXT;
ALTER TABLE scheduled_runs ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scheduled_runs ADD COLUMN delivery_next_at REAL;
ALTER TABLE scheduled_runs ADD COLUMN delivery_error TEXT;
ALTER TABLE scheduled_runs ADD COLUMN delivered_at REAL;

CREATE INDEX idx_scheduled_runs_delivery ON scheduled_runs(delivery_status, delivery_next_at);
