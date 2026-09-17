"""HTTP API for scheduled tasks: /scheduled-tasks."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import db
import skills as skills_catalog
from scheduling import config, cron, prompts, store, watch
from scheduling.runner import Scheduler, compute_next_run

MAX_PROMPT_CHARS = 4000
PREVIEW_COUNT = 5


class WatchPayload(BaseModel):
    strategies: list[Literal["url_hash", "research_diff"]] = Field(
        default_factory=lambda: ["research_diff"]
    )
    urls: list[str] = Field(default_factory=list, max_length=watch.MAX_WATCH_URLS)
    criteria: str = Field(default="", max_length=1000)


class TaskCreate(BaseModel):
    workspace_id: str
    name: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    mode: Literal["single", "multi"] = "multi"
    deep_research: bool = False
    skill_ids: list[str] = Field(default_factory=list, max_length=32)
    trigger_kind: Literal["cron", "once"] = "cron"
    cron_expr: Optional[str] = None
    run_at: Optional[float] = None
    timezone: Optional[str] = None
    watch: Optional[WatchPayload] = None
    include_previous_summary: bool = True
    enabled: bool = True


class TaskUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    prompt: Optional[str] = Field(default=None, min_length=1, max_length=MAX_PROMPT_CHARS)
    mode: Optional[Literal["single", "multi"]] = None
    deep_research: Optional[bool] = None
    skill_ids: Optional[list[str]] = Field(default=None, max_length=32)
    trigger_kind: Optional[Literal["cron", "once"]] = None
    cron_expr: Optional[str] = None
    run_at: Optional[float] = None
    timezone: Optional[str] = None
    # Explicit null clears the watch; omitted leaves it unchanged.
    watch: Optional[WatchPayload] = None
    include_previous_summary: Optional[bool] = None
    enabled: Optional[bool] = None


class PreviewRequest(BaseModel):
    trigger_kind: Literal["cron", "once"] = "cron"
    cron_expr: Optional[str] = None
    run_at: Optional[float] = None
    timezone: Optional[str] = None


class ParseScheduleRequest(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    timezone: Optional[str] = None


class ParsedSchedule(BaseModel):
    trigger_kind: Literal["cron", "once", "unclear"] = "unclear"
    cron_expr: str = ""
    run_at_local: str = ""
    description: str = ""


PARSE_SYSTEM = (
    "You convert a schedule written in natural language (usually Turkish) into "
    "a machine schedule. Respond with JSON only: "
    '{"trigger_kind": "cron" | "once" | "unclear", "cron_expr": "...", '
    '"run_at_local": "YYYY-MM-DDTHH:MM", "description": "..."}.\n'
    "Recurring schedules -> trigger_kind cron with a standard 5-field cron "
    "expression (minute hour day-of-month month day-of-week; day-of-week 0 = "
    "Sunday, 1 = Monday). A single future moment -> trigger_kind once with "
    "run_at_local in the given timezone, resolved against CURRENT LOCAL TIME "
    "('yarın', 'cuma', 'haftaya'). When no time of day is given use 09:00. "
    "'hafta içi' = 1-5. 'sabah' = 09:00, 'öğlen' = 12:00, 'akşam' = 18:00. "
    "description: a short English restatement of the schedule, e.g. "
    "'Every Monday at 09:00'. If the text is not a schedule, trigger_kind unclear."
)


def _clean_urls(urls: list[str]) -> list[str]:
    cleaned: list[str] = []
    for url in urls:
        url = (url or "").strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(status_code=422, detail=f"Invalid watch URL: {url}")
        cleaned.append(url)
    return cleaned


def _schedule_fields(record: dict[str, Any], now: float) -> dict[str, Any]:
    """Validate the schedule part of a task and compute next_run_at."""
    timezone = (record.get("timezone") or config.default_timezone()).strip()
    try:
        cron.zone(timezone)
    except cron.CronError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    kind = record.get("trigger_kind") or "cron"
    if kind == "cron":
        expression = " ".join(str(record.get("cron_expr") or "").split())
        try:
            spec = cron.parse(expression)
        except cron.CronError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid schedule: {exc}") from exc
        if cron.next_fire_after(spec, now, timezone) is None:
            raise HTTPException(status_code=422, detail="This schedule never runs.")
        gap = cron.min_interval_seconds(expression, now, timezone)
        minimum = config.min_interval_seconds()
        if record.get("deep_research"):
            minimum = max(minimum, config.min_deep_interval_seconds())
        if gap is not None and gap < minimum:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Runs must be at least {int(minimum // 60)} minutes apart"
                    + (" when deep research is on" if record.get("deep_research") else "")
                    + "."
                ),
            )
        fields = {"trigger_kind": "cron", "cron_expr": expression, "run_at": None, "timezone": timezone}
    else:
        run_at = record.get("run_at")
        if run_at is None:
            raise HTTPException(status_code=422, detail="A one-time task needs run_at.")
        if float(run_at) < now - 60:
            raise HTTPException(status_code=422, detail="The time for this one-time task has already passed.")
        fields = {"trigger_kind": "once", "cron_expr": None, "run_at": float(run_at), "timezone": timezone}
    if not record.get("enabled", True):
        fields["next_run_at"] = None
    elif kind == "cron":
        fields["next_run_at"] = compute_next_run(fields, now)
    else:
        # A one-off set for "now" (within the last minute) still runs.
        fields["next_run_at"] = compute_next_run(fields, now - 60)
    return fields


def _watch_value(payload: Optional[WatchPayload]) -> Optional[dict[str, Any]]:
    if payload is None:
        return None
    raw = payload.model_dump()
    raw["urls"] = _clean_urls(raw["urls"])
    try:
        normalized = watch.normalize_config(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return normalized.model_dump() if normalized else None


def _describe_next(task: dict[str, Any], now: float) -> list[float]:
    if not task.get("enabled"):
        return []
    if task.get("trigger_kind") == "cron" and task.get("cron_expr"):
        return cron.next_fires(task["cron_expr"], now, task["timezone"], 3)
    return [task["next_run_at"]] if task.get("next_run_at") else []


def _public_task(task: dict[str, Any], now: float) -> dict[str, Any]:
    public = dict(task)
    public.pop("watch_state", None)
    public["upcoming_runs"] = _describe_next(task, now)
    return public


def build_router(
    get_scheduler: Callable[[], Scheduler],
    *,
    clock: Callable[[], float] = time.time,
    complete: Optional[Callable[..., Any]] = None,
) -> APIRouter:
    router = APIRouter(prefix="/scheduled-tasks", tags=["scheduled-tasks"])

    async def _require_task(task_id: str) -> dict[str, Any]:
        task = await asyncio.to_thread(store.get_task, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Scheduled task not found.")
        return task

    @router.get("")
    async def list_scheduled_tasks(workspace_id: Optional[str] = Query(default=None)):
        now = clock()
        tasks = await asyncio.to_thread(store.list_tasks, workspace_id)
        return {
            "scheduling_enabled": config.enabled(),
            "variables": list(prompts.VARIABLES),
            "tasks": [_public_task(task, now) for task in tasks],
            "unseen_runs": sum(task["unseen_runs"] for task in tasks),
        }

    @router.post("", status_code=201)
    async def create_scheduled_task(req: TaskCreate):
        now = clock()
        if not await asyncio.to_thread(db.workspace_exists, req.workspace_id):
            raise HTTPException(status_code=404, detail="Workspace not found.")
        count = await asyncio.to_thread(store.count_tasks, req.workspace_id)
        if count >= config.max_tasks_per_workspace():
            raise HTTPException(
                status_code=422,
                detail=f"A workspace can have at most {config.max_tasks_per_workspace()} scheduled tasks.",
            )
        record = req.model_dump()
        record.update(_schedule_fields(record, now))
        record["watch"] = _watch_value(req.watch)
        record["skill_ids"] = skills_catalog.skill_ids(skills_catalog.resolve_skills(req.skill_ids))
        record["name"] = req.name.strip()
        record["prompt"] = req.prompt.strip()
        record["task_id"] = str(uuid.uuid4())
        record["created_at"] = now
        record["updated_at"] = now
        await asyncio.to_thread(store.create_task, record)
        task = await asyncio.to_thread(store.get_task, record["task_id"])
        return _public_task(task, now)

    @router.post("/preview")
    async def preview_schedule(req: PreviewRequest):
        now = clock()
        fields = _schedule_fields({**req.model_dump(), "enabled": True}, now)
        if fields["trigger_kind"] == "cron":
            runs = cron.next_fires(fields["cron_expr"], now, fields["timezone"], PREVIEW_COUNT)
        else:
            runs = [fields["run_at"]]
        return {"timezone": fields["timezone"], "upcoming_runs": runs}

    @router.post("/parse-schedule")
    async def parse_schedule(req: ParseScheduleRequest):
        timezone = (req.timezone or config.default_timezone()).strip()
        try:
            tz = cron.zone(timezone)
        except cron.CronError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        local_now = datetime.fromtimestamp(clock(), tz)
        call = complete
        if call is None:
            from execution.langgraph_runtime.openai_chat import complete_chat as call
        try:
            parsed = await call(
                [
                    {"role": "system", "content": PARSE_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"CURRENT LOCAL TIME: {local_now.strftime('%Y-%m-%d %H:%M %A')} ({timezone})\n"
                            f"SCHEDULE TEXT: {req.text}"
                        ),
                    },
                ],
                stage="schedule_parse",
                max_tokens=1024,
                temperature=0,
                response_format=ParsedSchedule,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail="Could not read the schedule. Please try again.") from exc
        if not isinstance(parsed, ParsedSchedule) or parsed.trigger_kind == "unclear":
            raise HTTPException(
                status_code=422,
                detail="No schedule found in that text. Try something like 'every Monday at 9'.",
            )
        if parsed.trigger_kind == "cron":
            try:
                cron.parse(parsed.cron_expr)
            except cron.CronError as exc:
                raise HTTPException(status_code=422, detail="Could not read the schedule.") from exc
            return {
                "trigger_kind": "cron",
                "cron_expr": " ".join(parsed.cron_expr.split()),
                "run_at": None,
                "timezone": timezone,
                "description": parsed.description,
                "upcoming_runs": cron.next_fires(parsed.cron_expr, clock(), timezone, PREVIEW_COUNT),
            }
        try:
            run_at = datetime.fromisoformat(parsed.run_at_local).replace(tzinfo=tz).timestamp()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Could not read the schedule.") from exc
        return {
            "trigger_kind": "once",
            "cron_expr": None,
            "run_at": run_at,
            "timezone": timezone,
            "description": parsed.description,
            "upcoming_runs": [run_at],
        }

    @router.get("/{task_id}")
    async def get_scheduled_task(task_id: str):
        return _public_task(await _require_task(task_id), clock())

    @router.patch("/{task_id}")
    async def update_scheduled_task(task_id: str, req: TaskUpdate):
        now = clock()
        task = await _require_task(task_id)
        changes = req.model_dump(exclude_unset=True)
        merged = {**task, **{key: value for key, value in changes.items() if key != "watch"}}
        fields: dict[str, Any] = {"updated_at": now}
        for key in ("name", "prompt"):
            if key in changes:
                fields[key] = str(changes[key]).strip()
        if "mode" in changes:
            fields["mode"] = changes["mode"]
        for key in ("deep_research", "include_previous_summary"):
            if key in changes:
                fields[key] = int(bool(changes[key]))
        if "skill_ids" in changes:
            fields["skill_ids_json"] = _json_dump(
                skills_catalog.skill_ids(skills_catalog.resolve_skills(changes["skill_ids"] or []))
            )
        if "watch" in changes:
            value = _watch_value(req.watch)
            fields["watch_json"] = _json_dump(value) if value else None
            fields["watch_state_json"] = "{}"
        schedule_keys = {"trigger_kind", "cron_expr", "run_at", "timezone", "deep_research", "enabled"}
        if schedule_keys & set(changes):
            schedule = _schedule_fields(merged, now)
            fields.update(schedule)
        if "enabled" in changes:
            fields["enabled"] = int(bool(changes["enabled"]))
            if changes["enabled"]:
                fields["paused_reason"] = None
                fields["consecutive_failures"] = 0
        await asyncio.to_thread(store.update_task, task_id, fields)
        return _public_task(await _require_task(task_id), now)

    @router.delete("/{task_id}")
    async def delete_scheduled_task(task_id: str):
        await _require_task(task_id)
        await asyncio.to_thread(store.delete_task, task_id)
        return {"deleted": task_id}

    @router.post("/{task_id}/pause")
    async def pause_scheduled_task(task_id: str):
        await _require_task(task_id)
        await asyncio.to_thread(
            store.update_task,
            task_id,
            {"enabled": 0, "next_run_at": None, "paused_reason": None, "updated_at": clock()},
        )
        return _public_task(await _require_task(task_id), clock())

    @router.post("/{task_id}/resume")
    async def resume_scheduled_task(task_id: str):
        now = clock()
        task = await _require_task(task_id)
        schedule = _schedule_fields({**task, "enabled": True}, now)
        await asyncio.to_thread(
            store.update_task,
            task_id,
            {
                **schedule,
                "enabled": 1,
                "paused_reason": None,
                "consecutive_failures": 0,
                "updated_at": now,
            },
        )
        return _public_task(await _require_task(task_id), now)

    @router.post("/{task_id}/run-now")
    async def run_scheduled_task_now(task_id: str):
        if not config.enabled():
            raise HTTPException(status_code=503, detail="Scheduled tasks are turned off (SCHEDULING_ENABLED).")
        await _require_task(task_id)
        return await get_scheduler().run_now(task_id)

    @router.get("/{task_id}/runs")
    async def list_scheduled_runs(task_id: str, limit: int = Query(default=50, ge=1, le=200)):
        await _require_task(task_id)
        return {"runs": await asyncio.to_thread(store.list_runs, task_id, limit)}

    @router.post("/{task_id}/runs/seen")
    async def mark_scheduled_runs_seen(task_id: str):
        await _require_task(task_id)
        marked = await asyncio.to_thread(store.mark_runs_seen, task_id, clock())
        return {"marked": marked}

    return router


def _json_dump(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
