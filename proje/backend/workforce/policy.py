"""Workforce runtime budgets, timeouts, and recovery policy constants."""

from __future__ import annotations

import workforce_plan
from camel.societies.workforce.utils import RecoveryStrategy

WORKER_MAX_ITERATIONS = 6
MANAGEMENT_MAX_ITERATIONS = 1
MAX_TASK_ATTEMPTS = 2  # initial attempt plus one recovery attempt
MAX_RECOVERED_TASK_RESULT_CHARS = 20_000
FINAL_SYNTHESIS_TIMEOUT_SECONDS = 60
MAX_SYNTHESIS_INPUT_CHARS = 40_000
# Searches one agent may run. Sized to the shape of the work, which is
# NOT "one good query": for anything the agent cannot name up front, the
# first query is necessarily vague, and a vague query returns hub pages.
# Measured against the live SearXNG for "Galatasaray son maçtaki ilk 11":
#
#   "Galatasaray son maç ilk 11 2026"            -> galatasaray.org,
#     "Galatasaray son resmi maç 2026 Ağustos"      transfermarkt squad,
#     "Galatasaray maçı ilk 11 kadrosu Ağustos"     fotomac hub, wikipedia
#   "Galatasaray Çorum FK 14 Ağustos 2026 ilk 11" -> sahadan/gzt/flashscore
#                                                    lineup pages, top 3
#
# The query that works can only be written AFTER a read reveals the
# opponent and date. So the real loop is search -> read -> refine -> read,
# and a cap of 2 forbids the refinement outright: a live run produced
# exactly the winning query as its FOURTH search, got the exhausted stub,
# and reported "arama kotası dolduğu için ek arama yapılamamıştır". Four
# leaves room for one wasted opening query, the refined one, and a
# recovery, without approaching the job-wide MAX_WEB_SEARCHES_PER_JOB (20)
# backstop that let an earlier build burn minutes.
MAX_SEARCH_CALLS_PER_AGENT = 4

# Discovery searches for the per-agent closure `_budgeted_search_tool`
# builds. MUST stay equal to MAX_SEARCH_CALLS_PER_AGENT: pointing this at
# the job-wide limit instead is what let a single agent issue 20 queries.
MAX_INTERNAL_DISCOVERY_SEARCHES = MAX_SEARCH_CALLS_PER_AGENT

# Reads a search-capable worker may spend before it must answer. Web work
# is search-THEN-read, so a ceiling derived from the search budget alone
# does not describe the shape of the work: the reads are where the answer
# actually comes from. Matched to the search budget for the same reason --
# each refinement round costs one read to learn from and one to answer
# from.
MAX_READ_CALLS_PER_AGENT = 4

# A search-capable worker needs one turn per allowed search, one per
# allowed read, one for the over-budget refusal it is likely to spend a
# turn discovering, and one to write the answer. Every term has to be
# counted: this was MAX_SEARCH_CALLS_PER_AGENT + 2, which silently assumed
# reads were free, so an ordinary two-search/two-read lookup ran out of
# turns mid-tool-call.
#
# Running out mid-tool-call is not a soft cap. CAMEL breaks out with the
# tool-call response as the final one (chat_agent.py:3284-3288), so the
# task ends with NO text content, fails to parse as a TaskResult, and
# surfaces to the user as "Task processing failed" -- twice, since the
# recovery attempt burns itself the same way. That is what a too-small
# ceiling buys: not a cheaper run, a failed one. The per-agent budgets
# above are what bound cost; this only has to leave room to spend them
# and still speak. _worker_answer_without_tools is the safety net for
# when it happens anyway; this constant is what makes it rare.
SEARCH_WORKER_MAX_ITERATIONS = (
    MAX_SEARCH_CALLS_PER_AGENT + MAX_READ_CALLS_PER_AGENT + 2
)

# Worst-case wall time for ONE model call, taken from measured runs rather
# than picked: across the bench_result_*.json corpus, per-call wall time
# (runtime / model_call_count) ranges 1.9s on a warm small call to 16.1s
# sustained inside a Workforce job, with a 24.0s single-call maximum on a
# cold start. 25 rounds that measured maximum up, and is the only input to
# the timeout below that is empirical rather than derived.
WORST_CASE_MODEL_CALL_SECONDS = 25

# Explicit value for CAMEL's task_timeout_seconds (workforce.py:308),
# which we previously left at its 600.0 default.
#
# What it actually guards is easy to misread: it is not a per-task
# deadline but the dispatch loop's wait for ANY task to come back to the
# channel (_get_returned_task, workforce.py:3902). With N subtasks running
# against an inference backend that serializes requests, the LAST task to
# be served waits behind every other worker's turns, so the bound has to
# cover the whole batch, not one task's own work.
#
# Derived from the constants that determine that worst case, so it tracks
# them instead of coincidentally matching a vendor default: at most
# MAX_INITIAL_DECOMPOSITION_SUBTASKS workers run at once (the bounded
# decomposition policy is what caps parallel fan-out), each may take up to
# WORKER_MAX_ITERATIONS turns, and each turn costs at most
# WORST_CASE_MODEL_CALL_SECONDS. Raising the decomposition cap or the
# iteration cap now widens this automatically; before, either change would
# have silently started producing false timeouts.
#
# A false timeout here is expensive out of proportion to itself: the task
# was never stuck, only queued, and timing it out feeds RETRY/REPLAN,
# which puts another full worker run into the same serialized queue and
# makes the remaining workers more likely to time out too. That positive
# feedback loop is the failure this bound exists to prevent.
#
# Deliberately sized for the serialized worst case, which keeps it correct
# on a concurrent backend (vLLM) rather than tuned for today's: with real
# concurrency the queueing factor collapses toward 1, so this simply
# becomes generous. It is a safety net for genuinely stuck work, never a
# scheduler, so erring long costs nothing while erring short costs a
# retry storm.
# (defined just below MAX_INITIAL_DECOMPOSITION_SUBTASKS, which it needs)

# ThinkingToolkit is not bound to any worker -- see the new_worker_agent
# template in build_workforce for why it was removed rather than budgeted.
# There is deliberately no MAX_THINKING_CALLS_PER_AGENT: a per-job call
# budget was tried first and did not work, because a tool return is only a
# string and cannot terminate the agent loop.


# Completion budget for dynamic workers only. The app-wide default is 4096
# (model_config.get_model), which measurement shows is too small for this
# one stage and fails in a particularly bad way.
#
# What breaks: a truncated completion does not merely shorten the answer.
# When the model is mid-tool-call, the cut lands inside the `arguments`
# JSON string, and CAMEL parses that with a bare, unguarded
# `json.loads(tool_call.function.arguments)` (chat_agent.py:3777, inside
# _handle_batch_response). The resulting JSONDecodeError propagates through
# _get_model_response's `except Exception: raise` and is caught only by
# Worker._process_single_task (worker.py:98-102), which fails the ENTIRE
# subtask with "JSONDecodeError: Unterminated string starting at ...".
# One truncated argument therefore costs a whole subtask plus its retry.
#
# Measured against the real configured model (deepseek-v4-flash) using the
# actual failing subtask prompts from job 4ff0908d:
#   4096 cap: 3769 completion tokens -- fit, but only 8% headroom
#   8192 cap: 287 / 435 / 5752 completion tokens
# The 5752 sample exceeds 4096 by 40%, i.e. that call WOULD have truncated
# at the old ceiling -- which is exactly the observed production failure.
# 8192 clears the measured maximum with ~30% headroom and is the smallest
# standard step that does so.
#
# NOTE: finish_reason is NOT a usable detector here. Verified directly --
# the provider returns finish_reason="tool_calls", not "length", even when
# it truncates the arguments mid-string. Nothing downstream can tell a
# truncated tool call from a complete one, which is why this has to be
# prevented by budget rather than caught after the fact.
DYNAMIC_WORKER_MAX_TOKENS = 8192

# Output budget for the one call that writes the user-visible answer. See
# the get_model() call in synthesize_workforce_result for the measurement
# that set it; separate from the 4096 default because every other call in
# the system produces intermediate material, and this one does not.
SYNTHESIS_MAX_TOKENS = 8192

# Completion budget for task_agent's decomposition call. Same underlying
# failure signature as ROUTING_MAX_TOKENS/DYNAMIC_WORKER_MAX_TOKENS above:
# a reasoning-heavy model (deepseek-v4-flash) bills hidden reasoning
# tokens against the same completion budget, so the app-wide 4096 default
# is not "the decomposition is verbose", it is "the model spent the whole
# budget reasoning and never got to emit the <task> tags". Reliability
# triage (rel-20260813T105420Z-2a702b3b) found planning.output_tokens
# pinned at exactly 4096/4096/4096/4097 across every one of
# 2-independent/dynamic/capability-boundary/4-independent, AND at exactly
# 4096 on 3-independent and (in a separate run) `explicit` -- i.e. this
# stage saturates the ceiling routinely, not just on the runs that
# happened to crash (see _patched_decompose_task's None-response guard
# for the crash itself). No live measurement of this stage's true output
# ceiling exists yet (unlike DYNAMIC_WORKER_MAX_TOKENS's own comment,
# which cites one), so this reuses that already-vetted value -- "the
# smallest standard step" this codebase has already confirmed clears the
# same model's reasoning overhead for a structurally similar call --
# rather than inventing an unverified number. Re-check planning.
# output_tokens in a future reliability run to confirm this actually
# stopped saturating; raise further only if it still does.
#
# Raised 8192 -> 12288 as a pre-demo stopgap: the AttributeError guard
# below already proves reasoning-heavy models can still exhaust 8192
# on this call, and re-running the reliability triage to confirm a
# smaller number is out of scope for the next 2 days. A deliberate
# retry-without-reasoning was considered and rejected (see the
# retry_attempts=1 rationale in _patched_decompose_task) -- this is
# capacity, not a second chance.
PLANNING_MAX_TOKENS = 12288

# Dedicated planning deadline. Routing is 30s and synthesis is 60s;
# planning must not inherit WORKFORCE_STEP_TIMEOUT (~1000s). 25s is the
# measured worst-case single model call; going longer is hidden reasoning,
# not a better split.
PLANNING_STEP_TIMEOUT_SECONDS = workforce_plan.PLANNING_STEP_TIMEOUT_SECONDS

# --- Context budgets for CAMEL's own management calls ------------------
#
# CAMEL formats the FULL task content into the decomposition prompt
# (workforce.py:1337) and into every quality/failure analysis prompt
# (TASK_ANALYSIS_PROMPT's {task_content}/{task_result}). Our root task
# content is main._combined_content() output, which can carry up to
# ~20K chars of conversation history plus ~60K chars of workspace/file
# preview -- so without a budget, an 80K-char blob is replayed in full
# into planning AND into every quality check, for a job whose workers
# already receive that same context through their own task content and
# (for files) the job-scoped workspace tools. These caps bound what the
# *management* layer sees; they never truncate what a worker produces or
# what the user gets back.
MAX_PLANNER_CONTENT_CHARS = 6_000
MAX_ANALYSIS_CONTENT_CHARS = 2_000
MAX_ANALYSIS_RESULT_CHARS = 4_000
# Per-worker description budget in the roster shown to CREATE_NODE_PROMPT
# only -- see _creation_roster for why that prompt gets a bounded roster
# and assignment keeps the full one. Long enough to tell two workers apart,
# which is all that prompt uses the roster for.
MAX_CREATION_ROSTER_DESCRIPTION_CHARS = 200

# General backstop on subtasks accepted from ANY decomposition. CAMEL's
# stock decompose prompt pushes "Aggressive Parallelization" and will
# happily emit a subtask per bullet point; each extra subtask costs a
# worker run plus its own quality check, which is the dominant per-job
# cost. Complex jobs still decompose -- this only clips runaway fan-out.
MAX_SUBTASKS_PER_DECOMPOSITION = 5

# Tighter ceiling for the INITIAL decomposition only -- governed by
# workforce_plan's bounded JSON policy (1 coherent task, or 2-4 independent
# workstreams). Nothing in that policy legitimately produces a 5th subtask.
#
# Deliberately a SEPARATE constant rather than lowering the general one:
# MAX_SUBTASKS_PER_DECOMPOSITION is the shared backstop for any other
# decomposition CAMEL may perform (today only RecoveryStrategy.DECOMPOSE,
# workforce.py:1646 -- currently unreachable because recovery is
# downgraded to RETRY/REPLAN, see WORKFORCE_ALLOWED_RECOVERY_STRATEGIES),
# and that path is not governed by the bounded initial policy. Tying both
# to one number would silently impose this policy's ceiling on a path the
# policy says nothing about, and would change recovery behavior if
# DECOMPOSE were ever re-enabled.
MAX_INITIAL_DECOMPOSITION_SUBTASKS = 4

# See WORST_CASE_MODEL_CALL_SECONDS above for the derivation and for why
# this is sized against the serialized worst case. Defined here rather
# than beside it because it needs the decomposition cap, which bounds how
# many workers can be in flight at once.
WORKFORCE_TASK_TIMEOUT_SECONDS = float(
    MAX_INITIAL_DECOMPOSITION_SUBTASKS
    # The worst case is whichever worker may take the most turns, which is
    # the search-capable one -- taking the generic cap here would understate
    # the bound for exactly the jobs most likely to approach it.
    * max(WORKER_MAX_ITERATIONS, SEARCH_WORKER_MAX_ITERATIONS)
    * WORST_CASE_MODEL_CALL_SECONDS
)

# Explicit value for ChatAgent's `step_timeout` (chat_agent.py:508), which
# defaults to Constants.TIMEOUT_THRESHOLD -- a silent 180.0s.
#
# This is a SECOND, independent deadline from the transport one, and it is
# the outer of the two: step_timeout wraps the whole astep in an
# asyncio.wait_for (chat_agent.py:3040-3051), while
# MODEL_CALL_TIMEOUT_SECONDS bounds the individual HTTP request inside it.
# Raising only the transport timeout therefore changed nothing observable
# -- the 180s asyncio guard still fired first and the 600s HTTP value was
# unreachable in practice. Both layers have to clear the orchestration
# bound for that bound to actually govern.
#
# What made this expensive rather than merely wrong: the timeout raises out
# of astep, Worker._process_single_task catches it (worker.py:98-102) and
# marks the whole subtask FAILED with the exception text as its result, so
# a worker that was only QUEUED behind its siblings on a serializing
# backend cost a full subtask plus a quality analysis plus a RETRY that
# re-entered the same queue. Observed live as
# "TimeoutError: Async step timed out after 180.0s" with dynamic_worker=15
# and quality=6 for a 3-subtask run.
#
# Applied to every agent this module builds. Agents that must fail faster
# than the batch still pass their own tighter value explicitly (routing
# 30s, synthesis 60s) and are deliberately left alone; what this replaces
# is the *inherited default* on agents that never opted into one.
WORKFORCE_STEP_TIMEOUT_SECONDS = WORKFORCE_TASK_TIMEOUT_SECONDS

# A fast-path (DIRECT/SPECIALIST) job has already been routed to exactly
# the capability it needs, so CAMEL's escalating recovery strategies
# (reassign to another worker / spin up a dynamic worker / decompose)
# cannot improve the outcome -- they just rebuild the full orchestration
# the routing decision existed to avoid. Measured on a real specialist
# research job: one weak-but-usable worker answer triggered replan ->
# create_worker -> 3 coordinator assignment calls, turning a ~4-call job
# into a 9-call one where recovery was 64% of all tokens spent. Fast-path
# jobs get at most this many bounded repairs, and only as a same-worker
# retry. See _patched_analyze_task.
FAST_PATH_MAX_REPAIRS = 1

# Minimum characters of substantive worker output for a fast-path result
# to count as "usable". Below this a repair is genuinely justified (the
# worker produced nothing to preserve); above it we keep the answer and
# let synthesis surface any limitations rather than paying for a rebuild.
FAST_PATH_USABLE_RESULT_CHARS = 200

# --- The only recovery strategies a WORKFORCE job may use --------------
#
# Recovery becomes deterministic: repair the task that already exists, or
# nothing. RETRY and REPLAN both repost the SAME Task object -- same id,
# same accumulated failure_count -- so every attempt keeps counting toward
# the one MAX_TASK_ATTEMPTS ceiling and the run always converges.
#
# Everything else is downgraded to RETRY (see _patched_analyze_task):
#
#   * DECOMPOSE splits the failed task into fresh subtasks, and a new Task
#     starts at failure_count = 0. On a task that failed or timed out for
#     reasons decomposition cannot fix (a slow endpoint, a worker that
#     cannot do the job), the planner tends to emit near-identical
#     children, so the run buys a brand-new full budget for the same work
#     it was already failing at -- effective recovery progress resets to
#     zero. That is the 20+ call / 80K+ token / 8-9 minute shape.
#   * CREATE_WORKER spins up another dynamic worker for a task that
#     already HAS an assignee, and can do so again on the next failure.
#   * REASSIGN preserves the task, but costs a coordinator call to move
#     work between workers whose capabilities the router already
#     considered -- non-deterministic spend for no new capability.
#
# Deliberately a guard on the returned strategy, not a prompt change: the
# quality/failure model may still recommend whatever it likes, it simply
# cannot spend the run's budget on anything but a same-task repair.
#
# Explicitly NOT affected: assignment-time worker creation. A task the
# coordinator marks NONE_FIT still reaches _handle_task_assignment_
# fallbacks and still gets a real dynamic worker -- that path runs through
# _patched_handle_assignment_retry_and_fallback and never touches
# _analyze_task at all. See docs/archive/NOTES.md; that behavior is a demo requirement
# and stays intact.
WORKFORCE_ALLOWED_RECOVERY_STRATEGIES = frozenset(
    {RecoveryStrategy.RETRY, RecoveryStrategy.REPLAN}
)


