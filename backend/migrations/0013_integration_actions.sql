-- Changes to an outside system (Jira first) that a run PROPOSED and a person
-- has to approve before anything is written (jira_actions.py).
--
-- A row is the exact change, already validated: what the approval card
-- shows is what apply sends. status moves only forward:
--   proposed -> applying -> applied | failed
--   proposed -> cancelled
-- and proposed -> applying is a single conditional UPDATE, so two clicks
-- (or two tabs) can never apply the same change twice.
CREATE TABLE integration_actions (
    action_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    system TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    result_json TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX idx_integration_actions_job ON integration_actions(job_id);
