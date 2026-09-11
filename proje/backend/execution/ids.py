"""Task id helpers shared by DIRECT and SPECIALIST single-task paths."""


def child_task_id(job_id: str) -> str:
    """The id a single-task run must use for its one unit of work.

    A real decomposition gives every subtask an id distinct from the job/root
    id (e.g. "{job_id}.0"). Two consumers rely on that distinction and treat
    `task_id == job_id` as "this is the inert Workforce ROOT task, not a real
    unit of work":

      * main._apply_event's "task_created" handler routes it to
        timing["root_created"] instead of timing["tasks"][task_id], so the
        task never gets a `created` timestamp and its assignment-latency line
        is silently skipped.
      * the frontend's stages.getJobTasks -> excludeRootTask filters it out
        of the plan entirely, leaving tasks.length == 0 for the whole run.

    Both single-task paths (SPECIALIST via _run_task_direct, and DIRECT via
    run_single_agent) therefore have to name their task like a subtask rather
    than like the root. One helper so the two paths cannot drift apart.
    """
    return f"{job_id}.0"
