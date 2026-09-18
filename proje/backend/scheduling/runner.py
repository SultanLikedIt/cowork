"""The scheduler loop: start due tasks as ordinary jobs and settle their runs.

Runs inside the backend process (one uvicorn worker). It does not execute
anything itself -- ``submit`` is main.submit_job, the same path a user's
message takes, so a scheduled run gets exactly the routing, research and
document behaviour of a manual one. ``job_state`` reads that job back.

Restart safety comes from the database, not from this object: next_run_at
is persisted and claimed with a compare-and-set (store.claim_due), a run
left "running" by a dead process is settled on the next tick from its job's
"interrupted" status, and an interrupted run is retried once.

Chains (scheduling/chains.py): when a run settles, tasks that follow its task
get a "queued" run carrying that run's report and files. Queued runs are
started by the next tick, ahead of due cron tasks, within the same
concurrency limit.

Email (scheduling/delivery.py): when a settled run meets its task's delivery
condition it is marked pending, and each tick sends pending deliveries through
``deliver`` (main._deliver_scheduled_run), retrying with backoff on failure.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from scheduling import chains, config, cron, delivery, prompts, store, watch

logger = logging.getLogger("agent_webapp.scheduling")

JOB_TERMINAL = frozenset({"done", "partial", "failed", "error", "cancelled", "interrupted"})
FAILURE_STATUSES = frozenset({"failed", "error"})
SUCCESS_STATUSES = frozenset({"done", "partial", "no_change"})
MAX_RUN_FILES = 10
MAX_DELIVERIES_PER_TICK = 3


@dataclass(frozen=True)
class SubmittedJob:
    job_id: str
    conversation_id: Optional[str]


SubmitFn = Callable[..., Awaitable[SubmittedJob]]
JobStateFn = Callable[[str], Optional[dict[str, Any]]]
DiffFn = Callable[[str, str, str], Awaitable[Optional[watch.DiffResult]]]
# job_id -> (file_ids registered as uploads, notes about files left out)
CollectFilesFn = Callable[[str], Awaitable[tuple[list[str], list[str]]]]
# (task, run) -> outcome of one email attempt
DeliverFn = Callable[[dict[str, Any], dict[str, Any]], Awaitable[delivery.DeliveryOutcome]]
# (task, run) -> the delivery setting in force for this run: the task's own,
# or one read from the task prompt ("ve ali@x.com'a gonder"). Injected so the
# runner does not need to know how a job's intent was classified.
ResolveDeliveryFn = Callable[[dict[str, Any], dict[str, Any]], Optional[dict[str, Any]]]


def compute_next_run(task: dict[str, Any], after: float) -> Optional[float]:
    if task.get("trigger_kind") == "cron" and task.get("cron_expr"):
        return cron.next_fire_after(task["cron_expr"], after, task["timezone"])
    if task.get("trigger_kind") == "once":
        run_at = task.get("run_at")
        return float(run_at) if run_at and float(run_at) > after else None
    # after_task: never due on its own; started by its upstream's runs.
    return None


async def _no_files(job_id: str) -> tuple[list[str], list[str]]:
    return [], []


async def _no_delivery(task: dict[str, Any], run: dict[str, Any]) -> delivery.DeliveryOutcome:
    return delivery.DeliveryOutcome(False, "Email delivery is not available.", retryable=False)


def _task_delivery(task: dict[str, Any], run: dict[str, Any]) -> Optional[dict[str, Any]]:
    return task.get("delivery")


class Scheduler:
    def __init__(
        self,
        *,
        submit: SubmitFn,
        job_state: JobStateFn,
        clock: Callable[[], float] = time.time,
        fetch_text: Callable[[str], str] = watch.fetch_page_text,
        diff: Optional[DiffFn] = None,
        collect_files: CollectFilesFn = _no_files,
        deliver: DeliverFn = _no_delivery,
        resolve_delivery: ResolveDeliveryFn = _task_delivery,
    ) -> None:
        self._submit = submit
        self._job_state = job_state
        self._clock = clock
        self._fetch_text = fetch_text
        self._diff = diff or (lambda prev, new, criteria: watch.diff_results(prev, new, criteria))
        self._collect_files = collect_files
        self._deliver = deliver
        self._resolve_delivery = resolve_delivery
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
        await self._pause_orphans()
        await self.deliver_pending()

        free = await self._free_slots()
        if free <= 0:
            return
        # Queued chain runs first: they are already late by definition (their
        # upstream just finished), and a busy cron schedule must not starve them.
        for run in await asyncio.to_thread(store.queued_runs, free):
            await self._start_queued(run)

        free = await self._free_slots()
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

    async def _free_slots(self) -> int:
        running = await asyncio.to_thread(store.running_runs)
        return config.max_concurrent() - len(running)

    async def run_now(self, task_id: str) -> dict[str, Any]:
        task = await asyncio.to_thread(store.get_task, task_id)
        if task is None:
            raise KeyError(task_id)
        run_input: Optional[dict[str, Any]] = None
        if task.get("trigger_kind") == "after_task":
            run_input = await self._input_from_latest_upstream(task)
        return await self.start_run(
            task, fire_time=self._clock(), trigger="manual", run_input=run_input
        )

    async def _pause_orphans(self) -> None:
        for task in await asyncio.to_thread(store.orphaned_chain_tasks):
            await asyncio.to_thread(
                store.update_task,
                task["task_id"],
                {
                    "enabled": 0,
                    "paused_reason": "Paused because the task it runs after was deleted.",
                    "updated_at": self._clock(),
                },
            )

    async def _start_queued(self, run: dict[str, Any]) -> None:
        task = await asyncio.to_thread(store.get_task, run["task_id"])
        if task is None or not task.get("enabled"):
            await asyncio.to_thread(
                store.update_run,
                run["run_id"],
                {
                    "status": "skipped_condition",
                    "finished_at": self._clock(),
                    "seen_at": self._clock(),
                    "error": "The task was paused or deleted before this run could start.",
                },
            )
            return
        if await asyncio.to_thread(store.running_runs, [task["task_id"]]):
            # Stays queued; its own previous run finishes first.
            return
        await self.start_run(
            task,
            fire_time=float(run["scheduled_for"]),
            trigger=run.get("trigger") or "chained",
            run_input=run.get("input"),
            queued_run=run,
        )

    # -- starting ----------------------------------------------------------

    async def start_run(
        self,
        task: dict[str, Any],
        *,
        fire_time: float,
        trigger: str,
        retry_of: Optional[str] = None,
        run_input: Optional[dict[str, Any]] = None,
        queued_run: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await self._start_locked(
                task, fire_time, trigger, retry_of, run_input, queued_run
            )

    async def _start_locked(
        self,
        task: dict[str, Any],
        fire_time: float,
        trigger: str,
        retry_of: Optional[str],
        run_input: Optional[dict[str, Any]],
        queued_run: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        now = self._clock()
        record = dict(
            trigger=trigger,
            scheduled_for=fire_time,
            retry_of=retry_of,
            run_input=run_input,
            existing=queued_run,
        )
        if await asyncio.to_thread(store.running_runs, [task["task_id"]]):
            return await self._record_run(
                task,
                status="skipped_overlap",
                error="The previous run was still in progress.",
                **record,
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
                    status="error",
                    error="None of the watched pages could be opened: "
                    + "; ".join(f"{url}: {err}" for url, err in check.errors.items()),
                    **record,
                )
                await self._complete(task, run, "error", summary=None, result="")
                return run
            if not check.baseline and not check.changed:
                run = await self._record_run(
                    task,
                    status="no_change",
                    change_detected=False,
                    change_summary="No change on the watched pages.",
                    seen=True,
                    **record,
                )
                await self._complete(task, run, "no_change", summary=None, result="")
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
        context_parts = [
            (run_input or {}).get("context"),
            prompts.previous_summary_context(task, timezone),
        ]
        context = "\n\n".join(part for part in context_parts if part) or None
        file_ids = _merge_file_ids(
            [item.get("file_id") for item in task.get("attached_files") or []],
            (run_input or {}).get("file_ids") or [],
        )
        when = datetime.fromtimestamp(fire_time, cron.zone(timezone))
        title = f"📅 {task['name']} — {prompts.format_date(when)}"
        try:
            submitted = await self._submit(
                task=task,
                prompt=prompt,
                context_text=context,
                conversation_title=title,
                file_ids=file_ids,
            )
        except Exception as exc:  # noqa: BLE001 - a rejected submission is a failed run
            detail = getattr(exc, "detail", None) or str(exc)
            run = await self._record_run(
                task,
                status="error",
                error=f"The job could not be started: {detail}",
                **record,
            )
            await self._complete(task, run, "error", summary=None, result="")
            return run

        run = await self._record_run(
            task,
            status="running",
            job_id=submitted.job_id,
            conversation_id=submitted.conversation_id,
            change_detected=change_detected,
            change_summary=change_summary,
            started=True,
            **record,
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
                    task,
                    fire_time=float(run["scheduled_for"]),
                    trigger="retry",
                    retry_of=run["run_id"],
                    run_input=run.get("input"),
                )
                return
            status = "error"
        await self._complete(task, run, status, summary=summary, result=result)

    async def _complete(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        status: str,
        *,
        summary: Optional[str],
        result: str,
    ) -> None:
        await self._apply_outcome(task, run, status, summary=summary)
        try:
            await self._queue_delivery(task, run, status)
        except Exception:  # noqa: BLE001 - an email problem must not undo this run's outcome
            logger.exception("queueing email for run %s failed", run.get("run_id"))
        try:
            await self._trigger_downstream(task, run, status, result)
        except Exception:  # noqa: BLE001 - a chain problem must not undo this run's outcome
            logger.exception("triggering tasks after %s failed", task["task_id"])

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

    # -- email delivery ----------------------------------------------------

    async def _queue_delivery(self, task: dict[str, Any], run: dict[str, Any], status: str) -> None:
        has_result = bool(run.get("job_id")) and status in SUCCESS_STATUSES
        setting = self._resolve_delivery(task, run)
        if not delivery.should_deliver(
            setting,
            status,
            run.get("change_detected"),
            is_watch=_watch_config(task) is not None,
            has_result=has_result,
        ):
            return
        await asyncio.to_thread(
            store.update_run,
            run["run_id"],
            {
                "delivery_status": "pending",
                "delivery_next_at": self._clock(),
                "delivery_error": None,
                # Kept per run: the task's setting may change before the
                # history is read, and a prompt-derived run should still show
                # where its recipients came from.
                "delivery_to": ", ".join(setting.get("to") or []),
                "delivery_source": setting.get("source") or "task",
            },
        )

    async def deliver_pending(self) -> None:
        now = self._clock()
        for run in await asyncio.to_thread(store.due_deliveries, now, MAX_DELIVERIES_PER_TICK):
            task = await asyncio.to_thread(store.get_task, run["task_id"])
            if task is None or not (self._resolve_delivery(task, run) or {}).get("to"):
                await asyncio.to_thread(
                    store.update_run,
                    run["run_id"],
                    {"delivery_status": "failed", "delivery_error": "Email delivery was turned off for this task."},
                )
                continue
            try:
                outcome = await self._deliver(task, run)
            except Exception as exc:  # noqa: BLE001 - treat as a failed attempt, retried later
                logger.exception("email delivery for run %s raised", run["run_id"])
                outcome = delivery.DeliveryOutcome(False, f"{type(exc).__name__}: {exc}")
            attempts = int(run.get("delivery_attempts") or 0) + 1
            now = self._clock()
            if outcome.ok:
                fields: dict[str, Any] = {
                    "delivery_status": "sent",
                    "delivery_attempts": attempts,
                    "delivery_next_at": None,
                    "delivered_at": now,
                    "delivery_error": "; ".join(outcome.notes) or None,
                }
                logger.info("emailed run %s of task %s", run["run_id"], task["task_id"])
            elif not outcome.retryable or delivery.gives_up(attempts):
                fields = {
                    "delivery_status": "failed",
                    "delivery_attempts": attempts,
                    "delivery_next_at": None,
                    "delivery_error": outcome.error,
                }
                logger.warning("email for run %s failed: %s", run["run_id"], outcome.error)
            else:
                fields = {
                    "delivery_attempts": attempts,
                    "delivery_next_at": now + delivery.retry_delay(attempts),
                    "delivery_error": outcome.error,
                }
            await asyncio.to_thread(store.update_run, run["run_id"], fields)

    # -- chains ------------------------------------------------------------

    async def _trigger_downstream(
        self, task: dict[str, Any], run: dict[str, Any], status: str, result: str
    ) -> None:
        children = await asyncio.to_thread(store.downstream_tasks, task["task_id"])
        children = [child for child in children if child.get("enabled")]
        if not children:
            return
        now = self._clock()
        upstream_is_watch = _watch_config(task) is not None
        has_result = bool(run.get("job_id")) and status in SUCCESS_STATUSES
        members = await asyncio.to_thread(chains.chain_member_ids, task["task_id"])
        started_today = await asyncio.to_thread(store.chained_runs_since, members, now - 86400)
        files: Optional[tuple[list[str], list[str]]] = None

        for child in children:
            if await asyncio.to_thread(store.has_chained_run, child["task_id"], run["run_id"]):
                continue
            condition = chains.condition_of(child)
            skip_reason: Optional[str] = None
            if not chains.condition_met(
                condition,
                status,
                run.get("change_detected"),
                upstream_is_watch=upstream_is_watch,
                has_result=has_result,
            ):
                skip_reason = chains.condition_reason(condition, status)
            elif started_today >= config.max_chained_runs_per_day():
                skip_reason = (
                    f"This chain already started {started_today} runs in the last 24 hours "
                    f"(limit {config.max_chained_runs_per_day()})."
                )
            if skip_reason:
                await self._record_run(
                    child,
                    trigger="chained",
                    scheduled_for=now,
                    status="skipped_condition",
                    error=skip_reason,
                    parent_run_id=run["run_id"],
                    seen=True,
                )
                continue

            if child.get("pass_upstream_files") and has_result and files is None:
                files = await self._collect_files(run["job_id"])
            run_input = self._build_input(task, child, result if has_result else "", files)
            try:
                await self._record_run(
                    child,
                    trigger="chained",
                    scheduled_for=now,
                    status="queued",
                    parent_run_id=run["run_id"],
                    run_input=run_input,
                )
            except sqlite3.IntegrityError:
                continue  # already queued by a concurrent settle
            started_today += 1
            logger.info("queued task %s after %s", child["task_id"], task["task_id"])

    def _build_input(
        self,
        upstream: dict[str, Any],
        child: dict[str, Any],
        result: str,
        files: Optional[tuple[list[str], list[str]]],
    ) -> dict[str, Any]:
        run_input: dict[str, Any] = {
            "upstream_task_id": upstream["task_id"],
            "upstream_name": upstream["name"],
            "context": None,
            "file_ids": [],
            "notes": [],
        }
        if child.get("pass_upstream_result") and result.strip():
            report = prompts.summarize_result(result, chains.MAX_UPSTREAM_CONTEXT_CHARS)
            run_input["context"] = (
                f"Bu görev, \"{upstream['name']}\" görevi tamamlandıktan sonra çalışıyor. "
                "O görevin sonucu aşağıda; isteği yerine getirirken bu sonucu girdi "
                "olarak kullan.\n\n"
                f"ÖNCEKİ GÖREVİN SONUCU:\n{report}"
            )
        if child.get("pass_upstream_files") and files is not None:
            file_ids, notes = files
            run_input["file_ids"] = list(file_ids)
            run_input["notes"].extend(notes)
        return run_input

    async def _input_from_latest_upstream(self, task: dict[str, Any]) -> dict[str, Any]:
        upstream_id = task.get("upstream_task_id")
        upstream = await asyncio.to_thread(store.get_task, upstream_id) if upstream_id else None
        if upstream is None:
            return {"context": None, "file_ids": [], "notes": ["The upstream task no longer exists."]}
        latest = await asyncio.to_thread(store.latest_successful_run, upstream["task_id"])
        job = self._job_state(latest["job_id"]) if latest and latest.get("job_id") else None
        if job is None:
            return {
                "upstream_task_id": upstream["task_id"],
                "upstream_name": upstream["name"],
                "context": None,
                "file_ids": [],
                "notes": [f"\"{upstream['name']}\" has no successful run yet, so this run got no input from it."],
            }
        files = await self._collect_files(latest["job_id"]) if task.get("pass_upstream_files") else None
        return self._build_input(upstream, task, str(job.get("result") or ""), files)

    # -- records -----------------------------------------------------------

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
        parent_run_id: Optional[str] = None,
        run_input: Optional[dict[str, Any]] = None,
        existing: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        now = self._clock()
        seen_at = now if seen or status in {"missed", "skipped_overlap", "skipped_condition"} else None
        if existing is not None:
            # A queued chain run becomes this run instead of a second row.
            fields = {
                "job_id": job_id,
                "conversation_id": conversation_id,
                "started_at": now if started else None,
                "finished_at": None if status == "running" else now,
                "status": status,
                "change_detected": change_detected,
                "change_summary": change_summary,
                "error": error,
                "seen_at": seen_at,
            }
            await asyncio.to_thread(store.update_run, existing["run_id"], fields)
            return {**existing, **fields}
        run = {
            "run_id": str(uuid.uuid4()),
            "task_id": task["task_id"],
            "job_id": job_id,
            "conversation_id": conversation_id,
            "trigger": trigger,
            "scheduled_for": scheduled_for,
            "started_at": now if started else None,
            "finished_at": None if status in {"running", "queued"} else now,
            "status": status,
            "change_detected": change_detected,
            "change_summary": change_summary,
            "retry_of": retry_of,
            "error": error,
            "parent_run_id": parent_run_id,
            "input": run_input,
        }
        await asyncio.to_thread(store.insert_run, run)
        if seen_at is not None:
            await asyncio.to_thread(store.update_run, run["run_id"], {"seen_at": seen_at})
            run["seen_at"] = seen_at
        return run


def _merge_file_ids(*groups: list[Optional[str]]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for file_id in group:
            if file_id and file_id not in merged:
                merged.append(file_id)
    return merged[:MAX_RUN_FILES]


def _watch_config(task: dict[str, Any]) -> Optional[watch.WatchConfig]:
    try:
        return watch.normalize_config(task.get("watch"))
    except ValueError:
        logger.warning("scheduled task %s has an invalid watch config", task.get("task_id"))
        return None


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
