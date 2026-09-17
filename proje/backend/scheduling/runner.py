"""The scheduler loop: start due tasks as ordinary jobs and settle their runs.

Runs inside the backend process (one uvicorn worker). It does not execute
anything itself -- ``submit`` is main.submit_job, the same path a user's
message takes, so a scheduled run gets exactly the routing, research and
document behaviour of a manual one. ``job_state`` reads that job back.

Restart safety comes from the database, not from this object: next_run_at
is persisted and claimed with a compare-and-set (store.claim_due), a run
left "running" by a dead process is settled on the next tick from its job's
"interrupted" status, and an interrupted run is retried once.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from scheduling import config, cron, prompts, store, watch

logger = logging.getLogger("agent_webapp.scheduling")

JOB_TERMINAL = frozenset({"done", "partial", "failed", "error", "cancelled", "interrupted"})
FAILURE_STATUSES = frozenset({"failed", "error"})
SUCCESS_STATUSES = frozenset({"done", "partial", "no_change"})


@dataclass(frozen=True)
class SubmittedJob:
    job_id: str
    conversation_id: Optional[str]


SubmitFn = Callable[..., Awaitable[SubmittedJob]]
JobStateFn = Callable[[str], Optional[dict[str, Any]]]
DiffFn = Callable[[str, str, str], Awaitable[Optional[watch.DiffResult]]]


def compute_next_run(task: dict[str, Any], after: float) -> Optional[float]:
    if task.get("trigger_kind") == "cron" and task.get("cron_expr"):
        return cron.next_fire_after(task["cron_expr"], after, task["timezone"])
    if task.get("trigger_kind") == "once":
        run_at = task.get("run_at")
        return float(run_at) if run_at and float(run_at) > after else None
    return None


class Scheduler:
    def __init__(
        self,
        *,
        submit: SubmitFn,
        job_state: JobStateFn,
        clock: Callable[[], float] = time.time,
        fetch_text: Callable[[str], str] = watch.fetch_page_text,
        diff: Optional[DiffFn] = None,
    ) -> None:
        self._submit = submit
        self._job_state = job_state
        self._clock = clock
        self._fetch_text = fetch_text
        self._diff = diff or (lambda prev, new, criteria: watch.diff_results(prev, new, criteria))
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def run_forever(self) -> None:
        logger.info("scheduler started (tick=%ss)", config.tick_seconds())
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not stop scheduling
                logger.exception("scheduler tick failed")
            await asyncio.sleep(config.tick_seconds())

    # -- one tick ----------------------------------------------------------

    async def tick(self) -> None:
        now = self._clock()
        await self.reconcile()
        running = await asyncio.to_thread(store.running_runs)
        free = config.max_concurrent() - len(running)
        if free <= 0:
            return
        claimed = await asyncio.to_thread(store.claim_due, now, free, compute_next_run)
        for task, fire_time in claimed:
            late = now - fire_time
            if late > config.catch_up_window_seconds():
                await self._record_run(
                    task,
                    trigger="schedule",
                    scheduled_for=fire_time,
                    status="missed",
                    error=(
                        "The server was down at the scheduled time and the catch-up "
                        "window had passed, so this run was skipped."
                    ),
                )
                continue
            trigger = "catch_up" if late > max(120.0, 2 * config.tick_seconds()) else "schedule"
            await self.start_run(task, fire_time=fire_time, trigger=trigger)

    async def run_now(self, task_id: str) -> dict[str, Any]:
        task = await asyncio.to_thread(store.get_task, task_id)
        if task is None:
            raise KeyError(task_id)
        return await self.start_run(task, fire_time=self._clock(), trigger="manual")

    # -- starting ----------------------------------------------------------

    async def start_run(
        self,
        task: dict[str, Any],
        *,
        fire_time: float,
        trigger: str,
        retry_of: Optional[str] = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await self._start_locked(task, fire_time, trigger, retry_of)

    async def _start_locked(
        self,
        task: dict[str, Any],
        fire_time: float,
        trigger: str,
        retry_of: Optional[str],
    ) -> dict[str, Any]:
        now = self._clock()
        if await asyncio.to_thread(store.running_runs, [task["task_id"]]):
            return await self._record_run(
                task,
                trigger=trigger,
                scheduled_for=fire_time,
                status="skipped_overlap",
                error="The previous run was still in progress.",
                retry_of=retry_of,
            )

        change_detected: Optional[bool] = None
        change_summary: Optional[str] = None
        config_watch = _watch_config(task)
        if config_watch and "url_hash" in config_watch.strategies:
            check = await watch.check_url_hashes(
                config_watch.urls, task.get("watch_state") or {}, fetch=self._fetch_text
            )
            await asyncio.to_thread(
                store.update_task,
                task["task_id"],
                {"watch_state_json": _json(check.hashes), "updated_at": now},
            )
            if len(check.errors) == len(config_watch.urls):
                run = await self._record_run(
                    task,
                    trigger=trigger,
                    scheduled_for=fire_time,
                    status="error",
                    error="None of the watched pages could be opened: "
                    + "; ".join(f"{url}: {err}" for url, err in check.errors.items()),
                    retry_of=retry_of,
                )
                await self._apply_outcome(task, run, "error", summary=None)
                return run
            if not check.baseline and not check.changed:
                run = await self._record_run(
                    task,
                    trigger=trigger,
                    scheduled_for=fire_time,
                    status="no_change",
                    change_detected=False,
                    change_summary="No change on the watched pages.",
                    retry_of=retry_of,
                    seen=True,
                )
                await self._apply_outcome(task, run, "no_change", summary=None)
                return run
            if check.changed:
                change_detected = True
                change_summary = "A watched page changed."

        timezone = task["timezone"]
        prompt = prompts.render_prompt(
            task["prompt"],
            fire_time=fire_time,
            timezone=timezone,
            last_run_at=task.get("last_run_at"),
        )
        context = prompts.previous_summary_context(task, timezone)
        when = datetime.fromtimestamp(fire_time, cron.zone(timezone))
        title = f"📅 {task['name']} — {prompts.format_date(when)}"
        try:
            submitted = await self._submit(
                task=task,
                prompt=prompt,
                context_text=context,
                conversation_title=title,
            )
        except Exception as exc:  # noqa: BLE001 - a rejected submission is a failed run
            detail = getattr(exc, "detail", None) or str(exc)
            run = await self._record_run(
                task,
                trigger=trigger,
                scheduled_for=fire_time,
                status="error",
                error=f"The job could not be started: {detail}",
                retry_of=retry_of,
            )
            await self._apply_outcome(task, run, "error", summary=None)
            return run

        run = await self._record_run(
            task,
            trigger=trigger,
            scheduled_for=fire_time,
            status="running",
            job_id=submitted.job_id,
            conversation_id=submitted.conversation_id,
            change_detected=change_detected,
            change_summary=change_summary,
            retry_of=retry_of,
            started=True,
        )
        await asyncio.to_thread(
            store.update_task,
            task["task_id"],
            {
                "last_run_at": fire_time,
                "last_job_id": submitted.job_id,
                "last_status": "running",
                "updated_at": now,
            },
        )
        logger.info(
            "scheduled task %s (%s) started job %s", task["task_id"], trigger, submitted.job_id
        )
        return run

    # -- settling ----------------------------------------------------------

    async def reconcile(self) -> None:
        for run in await asyncio.to_thread(store.running_runs):
            job = self._job_state(run["job_id"]) if run.get("job_id") else None
            if job is None:
                await self._settle(run, "error", result="", error="The job record no longer exists.")
                continue
            status = str(job.get("status") or "")
            if status in JOB_TERMINAL:
                await self._settle(run, status, result=str(job.get("result") or ""), error=job.get("error"))

    async def _settle(
        self, run: dict[str, Any], job_status: str, *, result: str, error: Optional[str]
    ) -> None:
        now = self._clock()
        task = await asyncio.to_thread(store.get_task, run["task_id"])
        if task is None:
            await asyncio.to_thread(
                store.update_run, run["run_id"], {"status": job_status, "finished_at": now}
            )
            return

        status = job_status
        fields: dict[str, Any] = {"finished_at": now, "error": error if status in FAILURE_STATUSES | {"interrupted"} else None}
        summary: Optional[str] = None
        if status in {"done", "partial"}:
            summary = prompts.summarize_result(result, config.summary_max_chars())
            config_watch = _watch_config(task)
            if config_watch and "research_diff" in config_watch.strategies:
                diff = await self._diff(
                    str(task.get("last_result_summary") or ""), result, config_watch.criteria
                )
                if diff is not None:
                    fields["change_detected"] = diff.material_change
                    fields["change_summary"] = diff.summary
                    if not diff.material_change and not run.get("change_detected"):
                        status = "no_change"
                        fields["seen_at"] = now
        fields["status"] = status
        await asyncio.to_thread(store.update_run, run["run_id"], fields)
        run = {**run, **fields}

        if status == "interrupted" and task.get("enabled"):
            already = await asyncio.to_thread(store.has_retry, run["run_id"])
            if not already and not run.get("retry_of"):
                logger.warning("scheduled run %s interrupted; retrying once", run["run_id"])
                await self.start_run(
                    task, fire_time=float(run["scheduled_for"]), trigger="retry", retry_of=run["run_id"]
                )
                return
            status = "error"
        await self._apply_outcome(task, run, status, summary=summary)

    async def _apply_outcome(
        self, task: dict[str, Any], run: dict[str, Any], status: str, *, summary: Optional[str]
    ) -> None:
        now = self._clock()
        fields: dict[str, Any] = {"last_status": status, "updated_at": now}
        if summary:
            fields["last_result_summary"] = summary
        if status in SUCCESS_STATUSES:
            fields["consecutive_failures"] = 0
        elif status in FAILURE_STATUSES:
            current = await asyncio.to_thread(store.get_task, task["task_id"]) or task
            failures = int(current.get("consecutive_failures") or 0) + 1
            fields["consecutive_failures"] = failures
            if failures >= config.max_consecutive_failures():
                fields["enabled"] = 0
                fields["paused_reason"] = (
                    f"Paused automatically after {failures} failed runs in a row."
                )
                logger.warning(
                    "scheduled task %s paused after %d failures", task["task_id"], failures
                )
        await asyncio.to_thread(store.update_task, task["task_id"], fields)

    async def _record_run(
        self,
        task: dict[str, Any],
        *,
        trigger: str,
        scheduled_for: float,
        status: str,
        job_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        change_detected: Optional[bool] = None,
        change_summary: Optional[str] = None,
        error: Optional[str] = None,
        retry_of: Optional[str] = None,
        started: bool = False,
        seen: bool = False,
    ) -> dict[str, Any]:
        now = self._clock()
        run = {
            "run_id": str(uuid.uuid4()),
            "task_id": task["task_id"],
            "job_id": job_id,
            "conversation_id": conversation_id,
            "trigger": trigger,
            "scheduled_for": scheduled_for,
            "started_at": now if started else None,
            "finished_at": None if status == "running" else now,
            "status": status,
            "change_detected": change_detected,
            "change_summary": change_summary,
            "retry_of": retry_of,
            "error": error,
        }
        await asyncio.to_thread(store.insert_run, run)
        if seen or status in {"missed", "skipped_overlap"}:
            await asyncio.to_thread(store.update_run, run["run_id"], {"seen_at": now})
            run["seen_at"] = now
        return run


def _watch_config(task: dict[str, Any]) -> Optional[watch.WatchConfig]:
    try:
        return watch.normalize_config(task.get("watch"))
    except ValueError:
        logger.warning("scheduled task %s has an invalid watch config", task.get("task_id"))
        return None


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
