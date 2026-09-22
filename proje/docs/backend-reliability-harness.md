# Backend reliability harness

The harness is a deterministic-first release gate around the backend's existing regression scripts and manually-triggered provider scenarios. It does not change application behavior and never starts a live suite by itself.

## Architecture

- `backend/reliability_harness/scenarios.json` is the reusable scenario catalog. Compact groups expand into individual scenarios, including exact 5k/10k/30k prompt payloads.
- `defaults.json` centralizes PASS/WARN/FAIL resource thresholds. A scenario's `thresholds` object can override any default.
- `adapters.py` runs isolated regression commands or submits/polls the real API. Live attachment fixtures are generated in memory and uploaded through the public endpoint.
- `evaluate.py` applies hard invariants, expected outcomes, duplicate-call detection, and resource thresholds.
- `report.py` writes raw JSON, JSONL, aggregate JSON, and a Markdown report with rankings and repeat variance.
- `cli.py` handles selection, repetition, destructive-suite gating, cost/scope estimation, and comparisons.

Generated output defaults to `backend/reliability-results/<batch-id>/` and is gitignored. Harness code, configuration, scenarios, tests, and this document are tracked.

## Commands

Run these from the repository root with the backend virtual environment. On Windows PowerShell:

```powershell
# Quick deterministic release check
backend\.venv\Scripts\python.exe -m backend.reliability_harness run --suite quick

# Every deterministic regression and stress probe
backend\.venv\Scripts\python.exe -m backend.reliability_harness run --suite deterministic

# List scenarios and category totals
backend\.venv\Scripts\python.exe -m backend.reliability_harness list

# Compare two prior result bundles
backend\.venv\Scripts\python.exe -m backend.reliability_harness compare backend\reliability-results\BASE\results.json backend\reliability-results\CANDIDATE\results.json
```

The server must already be running for live scenarios. Live execution is always an explicit command:

```powershell
# One live scenario (repeat three times for routing/assignment variance)
backend\.venv\Scripts\python.exe -m backend.reliability_harness run --suite live --scenario live.workforce.3-independent --repeat 3

# Selected critical, bounded live suite
backend\.venv\Scripts\python.exe -m backend.reliability_harness run --suite critical-live

# Normal non-destructive live catalog
backend\.venv\Scripts\python.exe -m backend.reliability_harness run --suite live

# Full high-volume suite; the extra flag prevents accidental execution
backend\.venv\Scripts\python.exe -m backend.reliability_harness run --suite destructive-live --allow-destructive-live

# Estimate scope without making requests
backend\.venv\Scripts\python.exe -m backend.reliability_harness estimate --suite destructive-live --repeat 3
```

The estimator reports a conservative planning envelope of 6,000 input and 1,000 output tokens per expected model call, plus a cost formula. Dollar cost remains model/provider-rate dependent rather than embedding a stale price.

Use `--config path/to/config.json` for a different threshold profile and `--output <directory>` to choose the artifact directory. A scenario override is merged over defaults, so expensive Workforce and large-file cases do not need to share Direct-mode ceilings.

## Outcomes and invariants

`FAIL` dominates `WARN`, which dominates `PASS`. Limits cover total duration, calls, input/total tokens, recovery, tools, duplicates, raw file rows/characters, and measurable context growth. Threshold overruns do not hide the original job status.

The evaluator treats a non-terminal final job, empty successful output, unexpected strategy/status, command failure, and terminal job with live tasks as defects. It also surfaces truncation, repeat calls, hidden failures, recovery amplification, upload errors, and skipped validation. Tool arguments/results are bounded and common secret fields are redacted in normalized output.

The deterministic stress tests separately verify:

- five concurrent isolated workspaces;
- five overlapping Direct/Workforce jobs with persistence reload;
- path traversal rejection and byte-identical source files;
- per-job row/character budget isolation;
- bounded forward-progressing table traversal;
- repeated-read signaling;
- thread-safe in-memory and JSONL tool-call recording.

## Metrics and blind spots

The result schema reserves every requested timing and quality field; unavailable architecture signals remain absent or `null`, never invented. The current job API exposes strategy/outcome, total duration, task plan/state/output/attempts, assignments, usage by stage, total tokens, ground-truth tool calls, attachments/workspace metadata, result/error, and cancellation state.

Exact routing/planning/assignment/worker-creation/synthesis durations are currently log-only or unavailable; exact recovery type and quality score are not in the job snapshot; per-turn context size is not exposed. Reports state these blind spots. Adding application telemetry would be a separate product/backend change and is intentionally outside this harness-only hardening pass.

## Intent routing eval

One classifier call (`artifact_intent.classify_artifact_intent`) decides, for every Workforce job, whether it wants a file, the web, code, the mailbox, Reddit / Hacker News, Jira, GitHub, or an email sent. Each new integration adds a flag to that one call, and a wrong flag is a wrong answer (the wrong agents run, or the right evidence is dropped). `reliability_harness/intent_eval.py` checks it against real requests.

- **Cases:** `reliability_harness/intent_cases.json` holds about 55 real, mostly Turkish, requests. Each names only the flags it cares about (`expect`) and optionally the agents that must run (`expect_roles`). The agent list is derived by the same functions the app uses, with every integration assumed enabled. Cases tagged `known-issue` record a known misroute by flags only (see their `note`).
- **Run it by hand:** before merging a change to the classifier prompt or a new integration. It uses the real model (one call per run; the full set at `--repeat 3` is 165 calls, a few cents), so it never runs in CI:

```powershell
docker compose exec backend python -m reliability_harness.intent_eval            # all cases, 3 runs each
docker compose exec backend python -m reliability_harness.intent_eval --tag github --repeat 5
docker compose exec backend python -m reliability_harness.intent_eval --case github.trending --repeat 8
docker compose exec backend python -m reliability_harness.intent_eval --list     # no model calls
```

- **Verdicts:** `PASS` (every run right), `FLAKY` (some runs wrong: the prompt is ambiguous for that request), `FAIL` (every run wrong), `ERROR` (the classifier did not answer, e.g. a 30 s timeout, and the app fell back to regex; not counted against the prompt). Exit code 0 / 1 (any wrong answer) / 2 (errors only). Per-run detail goes to `backend/reliability-results/intent-*.json`.
- **Without a model:** `tests/test_intent_eval.py` runs in the normal test suite. It validates the case file, checks that every `expect_roles` is what routing actually produces for the expected flags (so a case can pass at all), and checks the scoring.
- **Adding a case:** when a real request misroutes, add it here first, watch it fail, then fix the prompt or the routing and rerun the whole set. A prompt change often moves other cases (on 2026-09-22 a GitHub fix broke the Hacker News case until both were named).

> Note: the older `command` scenarios in `scenarios.json` still point at `backend/test_*.py` and a host `.venv`; the tests now live in `backend/tests/` and run in the container (`python -m pytest tests`). The intent eval does not depend on those scenarios.
