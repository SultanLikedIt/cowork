-- Who a run's email actually went to, and whether those recipients came from
-- the task's Email setting ('task') or from the task prompt itself ('prompt',
-- see artifact_intent.sends_email). Recorded per run so the history stays
-- readable after the task is edited.

ALTER TABLE scheduled_runs ADD COLUMN delivery_to TEXT;
ALTER TABLE scheduled_runs ADD COLUMN delivery_source TEXT;
