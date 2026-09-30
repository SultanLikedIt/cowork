# Notes

## KNOWN LIMITATION: dynamically-created workers don't reliably invoke their own tools

**Status:** documented, not fixed. Low severity in practice -- both observed cases produced the *correct* answer, unlike the original Document Agent fabrication bug this is adjacent to.

**Issue:** a dynamically-created worker (via the NONE_FIT patch, see `camel_service._patched_handle_assignment_retry_and_fallback`) gets the real `new_worker_agent` toolset (search/code-execution/thinking), and its system message carries `DYNAMIC_WORKER_TOOL_HONESTY_CAVEAT` -- an explicit, hard-worded instruction to always call the relevant tool rather than reason to an answer. In testing, a dynamically-created "Coding Agent"/"Code Execution Agent" solved both a factorial task and a deliberately obscure arithmetic task (`47238 * 91847 - 3829471`, not a memorizable constant) with the **correct** result, but the ground-truth `tool_calls` log was empty both times -- it never actually called `execute_code`.

**What was tried, in order:**
1. A soft caveat ("actually use them whenever the task requires... rather than reasoning your way to an answer") -- didn't change behavior.
2. A hard-mandate rewrite ("you MUST actually call the relevant tool... even if you are confident you already know the answer... never acceptable" to skip) -- still didn't change behavior, confirmed on both the factorial task and the obscure-arithmetic task (ruling out "it's just a famous constant the model has memorized").
3. Considered forcing `tool_choice` at the API/model-config level (CAMEL's `model_config_dict` passes arbitrary keys straight through to the OpenAI-compatible client, so this is technically reachable). Not applied: forcing `tool_choice="required"` would apply to every turn the agent takes, including its final answer-formatting turn, which normally doesn't need a tool call -- real risk of breaking the agent's ability to ever return a plain-text final response (hang or malformed output). Didn't have time to verify this safely close to the demo, so left alone.

**Conclusion:** prompt-level instructions ("you must use your tool") are fundamentally a soft, unenforceable request against a model capable of producing a correct answer without the tool -- this is the same shape of limitation as the coordinator not reliably honoring "use Agent X by name" from an earlier session, just showing up in a different place. The strengthened caveat is kept in place (harmless, and may help in cases where the model *isn't* confident enough to skip the tool), but is not a guaranteed fix.

**Next step, if this needs to be closed properly:** investigate whether `tool_choice` can be scoped to only the *first* call in a dynamically-created worker's turn sequence (falling back to `"auto"` afterward) rather than forcing it universally -- would need tracing CAMEL's own per-turn call loop in `chat_agent.py` to confirm where a request-level override could be injected without disrupting the loop's own termination condition.

## FIXED: Recent-jobs sidebar is now persisted via SQLite

**Status:** fixed (`9d2c4b4`, "Add SQLite persistence and per-workspace conversation continuity"). Originally this note described `_JOBS` as a plain in-process dict wiped on every restart — no longer accurate.

`_JOBS: dict[str, dict[str, Any]] = {}` (`backend/main.py:595`) is still an in-memory dict, but it's now a rehydratable cache, not the source of truth: `backend/db.py` persists job rows to SQLite (`app.db`), and `main.py`'s `@app.on_event("startup")` handler `_rehydrate_from_db()` runs migrations, marks any job left incomplete by a crash/restart as interrupted (`db.mark_incomplete_jobs_interrupted()`), and repopulates `_JOBS` from `db.load_all_jobs()`. Manually-added agents are similarly rehydrated via `camel_service.load_manual_workers(db.load_manual_workers())`. `_JOB_CONTROLS`, `_JOB_INDEX`, `_WORKER_ROLES`, `_TIMING` remain in-memory-only and are deliberately *not* rehydrated (a rehydrated job has no live workforce/usage_tracker/recorder to attach — `_job_snapshot()`/`job_events()` treat `control is None` as "historical job").

As of this session's uncommitted diff, `backend/db.py` also gained a `DATA_DIR` env override (defaults to the file's own directory, same as before; set via `DATA_DIR=/data` in Docker) so the DB file, uploads, workspaces, and logs all land on a mounted volume instead of next to source code a Docker image COPYs in — bare-metal/local dev behavior is unchanged.

Still single-process only: SQLite plus in-memory `_JOB_CONTROLS` et al. won't survive or coordinate across multiple backend processes — a future multi-instance deployment would need those moved to something shared (e.g. Postgres/Redis) regardless of the persistence-on-restart problem now being solved.

## BLOCKED: Ollama tool-calling breaks under Workforce's structured-output enforcement

**Status:** BLOCKED pending real LiteLLM/vLLM gateway access. Highest-priority item to re-test once that access is available.

**Issue:** CAMEL's `Workforce` forces every worker's response into a strict JSON envelope (to parse task success/failure/content programmatically). On every local model tested against Ollama (`qwen2.5:3b`, `qwen2.5-coder:7b`), this conflicts with tool-calling: the Code Agent (wired to `CodeExecutionToolkit`) writes and describes code in its response text but never actually invokes `execute_code`. Confirmed via explicit tool-call logging (`orchestrator.TOOL_CALL_LOG`) rather than trusting the agent's self-reported summary — the log was empty across multiple runs despite the agent claiming it ran the code.

**Root cause:** `Workforce` has two mechanisms for forcing structured JSON output, and both were tested — both break tool-calling on Ollama:

1. **`use_structured_output_handler=True`** (the default). Every worker prompt gets a `**CRITICAL**: Your response must contain ONLY the JSON object... Do not include any explanatory text, comments, or content outside the JSON structure` instruction appended (`camel/societies/workforce/structured_output_handler.py`). Reproduced directly against `ChatAgent` (bypassing Workforce entirely) with this exact prompt wrapper and a registered tool: the model complies literally with "ONLY JSON" and skips the tool call, on both `qwen2.5:3b` and `qwen2.5-coder:7b`.
2. **`use_structured_output_handler=False`** (native `response_format=TaskResult`). Tested as an alternative — not only did tool-calling still fail, output quality got worse: one subtask response came back empty (rejected by content validation), and another leaked garbled internal serialization text into the result (e.g. `"', 'tool_calls': '[]', 'total_tokens': 1627}]..."`).

Separately, swapping to a bigger/coder-tuned model (`qwen2.5-coder:7b`) did not fix it, and revealed a second, distinct issue: without the JSON constraint, that model attempted a tool call but emitted it in the wrong wire format (plain `{"name": ..., "arguments": ...}` instead of Ollama's required `<tool_call>{...}</tool_call>` tags visible in the model's chat template via `/api/show`), so Ollama's server-side parser never recognized it as a real function call and it leaked through as plain text.

**Hypothesis (revised — see update below):** originally suspected Ollama-specific, on the theory that commercial-grade OpenAI-compatible inference servers (vLLM behind a LiteLLM/OpenRouter gateway) typically support strict JSON-mode output and tool-calling together correctly. **This hypothesis did not hold** — see the 2026-07-29 update.

**Update (2026-07-29) — reproduced on the real gateway, `use_structured_output_handler=False` variant:** the project moved to a real OpenRouter gateway (`MODEL_BASE_URL=https://openrouter.ai/api/v1`, `MODEL_NAME=qwen/qwen3-32b`, see `backend/.env`). The current app (post clean-rebuild, `backend/camel_service.py`) uses the default `use_structured_output_handler=True` and — per `backend/logs/tool_calls.jsonl` (job `test-job`) — `search_duckduckgo` *does* get invoked for real there, unlike on Ollama. So the default handler path is confirmed working against this gateway.

The remaining untested combination — `use_structured_output_handler=False` (native `response_format`) against this same gateway/model — was tested directly (scratch script building a one-worker Workforce with a tool-bearing Research Agent, torn down after the run). Result: **tool-calling still did not fire.** The Research Agent's task was explicitly to search for "the latest stable version of Python" and it returned `{"version": "3.12", "release_date": "2023-10-02"}` — a plausible-looking but wrong/stale answer (the earlier `use_structured_output_handler=True` run's real search on the same gateway had turned up 3.14.6 as current) — with zero entries recorded in the ground-truth `ToolCallRecorder` for that run. So `search_duckduckgo` was never actually called; the model answered from parametric memory and presented it as if it were the result of the search it was instructed to run. This rules out "native response_format on a real gateway" as a fix — the working configuration is specifically `use_structured_output_handler=True` (the CAMEL-side JSON-instruction wrapper), not the native structured-output mode, at least for `qwen/qwen3-32b` via OpenRouter.

Separately, a `UnicodeEncodeError` (`'charmap' codec can't encode characters`) surfaced in `Workforce`'s internal error logging during this run — a Windows console encoding issue (cp1252 can't print some character in the log line), not related to the tool-calling result itself, and did not stop the task from completing.

**Conclusion:** for this project, stick with `use_structured_output_handler=True` (the current default in `build_workforce`) — it's the only variant of the two that has been confirmed, with ground-truth logging, to actually invoke tools on both Ollama-free real-gateway smoke tests and now this direct comparison. No code change needed; this was a verification task, not a bug.

## FIXED: Workforce's PIPELINE mode deadlocked on quality-check DECOMPOSE recovery

**Status:** fixed via monkeypatch (`orchestrator._patch_pipeline_decompose_recovery()`). The `PipelineTimeoutError` timeout wrapper stays in place as a defensive safety net, but should now rarely trigger.

**Issue:** `orchestrator.run_fixed_pipeline()` uses `Workforce.set_pipeline_tasks()` (`PIPELINE` mode) to enforce a deterministic 4-stage task order (research → report → review → outline) instead of relying on the coordinator's own auto-decomposition judgment. Task *ordering* was confirmed 100% reliable this way — verified across 3 consecutive runs, every dispatch followed the declared dependency order every time, even in runs that later failed. However, 2 of those 3 runs hung indefinitely partway through.

**Root cause:** when a worker's response fails Workforce's internal quality check, its automatic recovery system can choose the `DECOMPOSE` strategy (`camel/societies/workforce/workforce.py:1636`), which calls `_decompose_task(task)`. That method has a PIPELINE-mode short-circuit (`workforce.py:1333`):
```python
if self.mode == WorkforceMode.PIPELINE:
    return []
```
So in PIPELINE mode, `DECOMPOSE` recovery silently discards the failing task (0 subtasks, nothing re-queued) instead of retrying it. Any task that declared a dependency on the discarded one can then never see its dependency reach a completed state, and waits forever. Workforce's own internal `task_timeout_seconds` (default 600s) only logs a repeating warning (`"Timeout waiting for task return..."`) — it does not fail or retry the stuck task, so without an external timeout the process hangs indefinitely.

**Frequency:** hit 2 out of 3 runs before the fix — fairly common, since `qwen2.5:3b` fails Workforce's quality bar often enough to trigger DECOMPOSE recovery regularly.

**Quality threshold investigated:** `quality_sufficient` (`camel/societies/workforce/utils.py:300-313`) requires `quality_score >= 70 AND recovery_strategy is None` — hardcoded, not configurable via any `Workforce` constructor param. It's not a pure numeric cutoff: the LLM quality-evaluator can assign a passing score (even exactly 70) while still separately recommending a recovery action, which fails the task regardless of score. The scores that triggered the broken DECOMPOSE path in the 2 failed runs were 60 and 40 — clearly below threshold, not borderline near-misses. But recovery *strategy* is a separate, somewhat inconsistent LLM judgment call from the score: a score of 40 triggered `retry` in one run and `decompose` in another.

**Fix applied:** `orchestrator._patch_pipeline_decompose_recovery()` monkeypatches `Workforce._apply_recovery_strategy` to remap `RecoveryStrategy.DECOMPOSE` → `RecoveryStrategy.RETRY` whenever `self.mode == WorkforceMode.PIPELINE`. `RETRY` reposts the failing task directly to the same worker via `task.assigned_worker_id` (`workforce.py:1548-1562`), which survives the pre-recovery tracking cleanup and needs no decomposition at all — and it still respects the existing `failure_count >= 2` "give up and accept" safety net (`workforce.py:4878`), so a task that keeps failing can't loop forever either; it eventually gets accepted with its low-quality result rather than hanging. The remap only fires in `PIPELINE` mode, so `AUTO_DECOMPOSE`/free-form usage of the same `Workforce` class is unaffected.

**Verified:** 5 consecutive runs after the fix, all 5 completed fully (previously 1/3). Two of those 5 runs hit the exact scenario that used to deadlock — DECOMPOSE recovery fired, including twice in a row in one run — and both times correctly retried instead of hanging, with one run falling through cleanly to the pre-existing "retry limit reached" fallback once retries were exhausted.

**Fragility warning:** this monkeypatch depends on the internal structure of `camel-ai==0.2.80`'s `_apply_recovery_strategy` method (its exact signature, the `TaskAssignment`/`TaskAnalysisResult` shape, and the RETRY branch's reliance on `task.assigned_worker_id`). If the pinned `camel-ai` version is ever upgraded, **this patch needs to be re-verified** — vendored internals like this are not a stable public API and could change or be removed between versions, which would make the patch silently do nothing (falling back to the original broken DECOMPOSE-in-PIPELINE-mode behavior) rather than raising an error. Re-run the 5x verification in this note whenever `camel-ai` is upgraded.

**Next step:** re-test with the real LiteLLM/vLLM gateway model once available — if quality-check failures are rarer with a stronger model, the patch becomes a pure safety net rather than something regularly exercised.

## PLANNED: Selectively port UI pieces from eigent-ai/eigent (research done, not started)

**Status:** researched 2026-08-08, not started. Resume this from a fresh session -- the plan below is self-contained.

**Why:** after several rounds of incremental frontend polish (see the redesign/MCP-roadmap work already merged into `clean-rebuild`), the owner decided hand-rolled CSS iteration wasn't converging to a good enough visual result fast enough, and asked to look at [eigent-ai/eigent](https://github.com/eigent-ai/eigent) -- a much larger open-source "Cowork" product built on the *same* CAMEL-AI Workforce concept -- for reusable UI.

**What Eigent actually is:** an Electron desktop app (own Python backend, own auth via Stack Auth, terminal via node-pty, browser-agent workspace, remote control, connectors) -- **not** a drop-in replacement for our app. Stack: TypeScript + Tailwind + shadcn/ui (Radix primitives) + zustand + react-router + react-query + framer-motion. We're plain JSX + one big `styles.css`, no TypeScript, no Tailwind. License: **Apache-2.0** (freely reusable with attribution, no copyleft issue).

**Key finding that makes this feasible at all:** Eigent already maintains a genuine **web-only mode**, not just Electron -- `vite.config.web.ts` (`npm run dev:web` / `build:web`), and a `src/host` abstraction (`useHost()`) that makes every Electron-only call (`host?.electronAPI`, `host?.ipcRenderer`) conditionally null under web, specifically so the same React components run in both. There's also a literal `src/client/web/` vs `src/client/desktop/` split. This was clearly a deliberate design goal on their end, which is why porting is realistic instead of a fight against the architecture.

**Scope decision -- take vs. skip:**

*Take (maps directly onto pieces we already have or are actively building):*
- `src/components/ui/*` -- the shadcn/Radix primitive set (button, dialog, dropdown-menu, tabs, select, switch, tooltip, etc.) -- foundation layer, mostly self-contained.
- `tailwind.config.js` + their design tokens (colors/radius/spacing scale) -- reference for visual language, or adopt Tailwind itself (see risk below).
- `src/components/ChatBox/*` (`MessageItem`, `BottomBox`, `TaskBox`, `ProjectChatContainer`) -- maps directly onto our `ChatExchange.jsx` / `Composer.jsx` / the `WorkforcePlanStrip` we just built.
- `src/components/AddWorker/*` (`ToolSelect.tsx`) -- a **real, working** tool-selection UI for adding an agent, not a placeholder like our current "Coming soon" MCP roadmap cards. Directly relevant to the MCP work already planned (see the Docker/MCP-roadmap history above) -- worth studying even if not copied verbatim.
- `src/components/TopBar`, `ProjectPageSidebar`, `HistorySidebar` -- maps onto our `Sidebar.jsx` / `RightPanel.jsx`.
- `src/pages/Home` -- maps onto our `HomeView.jsx` / `WorkspacePicker.jsx`, but it's more complex than we need (Projects/Spaces/Tasks/Triggers tabs) -- take the landing/picker pattern, not the whole page.

*Skip entirely (Electron-only or features we don't have a backend for):*
- `electron/` folder, anything under `Terminal`/`TerminalAgentWorkspace` (xterm/node-pty), `Browser`/`BrowserAgentWorkspace`, `RemoteControl`, `Channels`, `Connectors` pages, `Login.tsx`/`SignUp.tsx` + `@stackframe/react` auth (we have no auth system), the `WorkFlow` visual node-graph (`@xyflow/react` -- nice but complex, defer), Monaco editor, i18n.

**Technical risks to plan around, not skip past:**
1. **TypeScript vs. our plain JSX.** Recommend stripping types on port rather than introducing TypeScript project-wide -- smaller footprint, matches current codebase.
2. **zustand + react-query vs. our local React state.** Don't adopt these wholesale just because Eigent uses them -- re-wire ported components to our existing `App.jsx` state/`api.js` calls. Adopting their state-management stack is a separate, much bigger decision than "port some components."
3. **Their live-update mechanism is a WebSocket subscription (`useExecutionSubscription`); ours is polling/SSE (the streaming-token feature already merged).** Map ported components' data needs onto our existing mechanism, don't import theirs.
4. **License housekeeping:** Apache-2.0 requires preserving copyright/license headers in copied files and should get a `THIRD_PARTY_NOTICES.md` (or similar) noting UI is adapted from `eigent-ai/eigent` (Apache-2.0). Low friction, don't skip it.

**Recommended execution plan (do NOT do this on `clean-rebuild` directly):**
1. New branch off `clean-rebuild` (e.g. `eigent-ui-port`) so the current, already-working UI is never at risk -- explicit ask from the owner ("elimizdeki şeyi de kaybetmeyelim").
2. Add Tailwind + the `components/ui` shadcn primitives as a purely additive dependency first; verify `npm run build` and that existing pages render unchanged (old `styles.css` untouched, so this should be non-destructive by construction).
3. Port **one** component end-to-end as a proof of concept before going further -- best candidate is the `ChatBox` family into `ChatExchange.jsx`/`Composer.jsx`, since that's the screen the owner cares most about ("chat ekranları"). Get explicit sign-off on that one before porting anything else.
4. Only after sign-off, continue with `AddWorker`/`ToolSelect` (real tool-selection UI, relevant to the MCP roadmap work) and the Sidebar/TopBar pieces.
5. Merge back to `clean-rebuild` incrementally (small PRs/merges per component), the same verify-in-browser-before-merging discipline used for every prior UI round in this project -- don't batch the whole port into one giant unreviewed diff.

**Next step:** start step 1 (branch + Tailwind/shadcn primitives, additive only) in a fresh session.
