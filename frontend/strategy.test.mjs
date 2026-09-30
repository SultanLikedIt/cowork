// Execution-strategy resolution and the two UI rules that depend on it:
//   * what the chat trace is allowed to claim ran (Workforce plan / planner
//     preparation / agent preparation), and
//   * when an agent may be captioned "Dynamically created agent".
// Imports the real stages.js so it cannot drift from what the app runs; the
// RightPanel source rule is exercised through a direct mirror of the
// component's own expression, kept adjacent to the assertions it protects.
import {
  activityText,
  executionLabelOf,
  executionRoleOf,
  executionStrategyOf,
  USER_FACING_PROGRESS,
  usageTokensReported,
  userFacingProgress,
} from './src/stages.js'

let failures = 0
const check = (name, ok, detail = '') => {
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${ok ? '' : ` -- ${detail}`}`)
  if (!ok) failures++
}
const eq = (name, actual, expected) =>
  check(name, actual === expected, `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`)

const job = (usage, extra = {}) => ({ job_id: 'j1', mode: 'multi', usage, ...extra })

// --- executionStrategyOf ----------------------------------------------
eq('single strategy is reported', executionStrategyOf(job({ execution_strategy: 'single' })), 'single')
eq('direct strategy is reported', executionStrategyOf(job({ execution_strategy: 'direct' })), 'direct')
eq('specialist strategy is reported', executionStrategyOf(job({ execution_strategy: 'specialist' })), 'specialist')
eq('workforce strategy is reported', executionStrategyOf(job({ execution_strategy: 'workforce' })), 'workforce')

// A multi-mode job the router sent down the DIRECT path must NOT be treated
// as a Workforce run -- this is the regression that pinned the trace on
// "Agent preparation / 0-0 tasks" for the whole run.
eq(
  'mode=multi + routed direct is direct, not workforce',
  executionStrategyOf({ job_id: 'j', mode: 'multi', usage: { execution_strategy: 'direct' } }),
  'direct',
)

// Fallbacks: legacy jobs and the optimistic composer stub carry no strategy.
eq('legacy multi job falls back to workforce', executionStrategyOf({ mode: 'multi', usage: {} }), 'workforce')
eq('single job falls back to single', executionStrategyOf({ mode: 'single', usage: {} }), 'single')
eq('missing usage falls back on mode', executionStrategyOf({ mode: 'multi' }), 'workforce')
eq('unknown strategy value falls back on mode', executionStrategyOf(job({ execution_strategy: 'turbo' })), 'workforce')
eq('null-safe', executionStrategyOf(null), 'single')
eq('explicitly unavailable token usage is not reported', usageTokensReported({ usage_available: false, total_tokens: 0 }), false)
eq('provider token usage is reported', usageTokensReported({ usage_available: true, total_tokens: 0 }), true)
eq('legacy non-zero token usage is reported', usageTokensReported({ total_tokens: 42 }), true)
eq('routed Direct is never labelled Workforce', executionLabelOf(job({ execution_strategy: 'direct' })), 'Direct')
eq('single is labelled Single', executionLabelOf(job({ execution_strategy: 'single' })), 'Single')
eq('specialist label is factual', executionLabelOf(job({ execution_strategy: 'specialist' })), 'Specialist')

// --- executionRoleOf ---------------------------------------------------
eq(
  'specialist role is surfaced',
  executionRoleOf(job({ execution_strategy: 'specialist', execution_strategy_role: 'Research Agent' })),
  'Research Agent',
)
eq('absent role is empty', executionRoleOf(job({ execution_strategy: 'workforce' })), '')
eq('null-safe role', executionRoleOf(null), '')

// --- factual activity status ------------------------------------------
eq(
  'web activity is rendered without exposing model reasoning',
  activityText({ stage: 'searching', detail: 'official rates' }),
  'Searching the web · official rates',
)
eq(
  'browser fallback activity is explicit',
  activityText({ stage: 'browsing', detail: 'https://example.com' }),
  'Using browser · https://example.com',
)
eq('unknown activity is ignored safely', activityText({ stage: 'thinking', detail: 'secret' }), '')
eq('missing activity is empty', activityText(null), '')

// --- userFacingProgress (chat progress labels) -------------------------
eq(
  'optimistic stub shows thinking',
  userFacingProgress({ _optimistic: true, mode: 'multi' }).label,
  USER_FACING_PROGRESS.thinking,
)
eq(
  'queued job with no tasks shows thinking',
  userFacingProgress({ status: 'queued', mode: 'multi' }).label,
  USER_FACING_PROGRESS.thinking,
)
eq(
  'streaming maps to preparing response',
  userFacingProgress({ status: 'running', streaming_text: 'Hello' }).label,
  USER_FACING_PROGRESS.preparing,
)
eq(
  'web search maps to researching sources',
  userFacingProgress(
    { status: 'running', current_activity: { stage: 'searching', detail: 'secret query' } },
  ).label,
  USER_FACING_PROGRESS.researching,
)
eq(
  'building maps to creating document',
  userFacingProgress(
    { status: 'running', current_activity: { stage: 'building', detail: 'report.xlsx' } },
  ).label,
  USER_FACING_PROGRESS.creating,
)
check(
  'raw activity detail is not surfaced in chat label',
  !userFacingProgress({
    status: 'running',
    current_activity: { stage: 'opening', detail: 'https://example.com' },
  }).label.includes('example.com'),
)

// --- What the chat trace may render (mirrors ChatExchange) -------------
const traceFor = (j, isActive) => {
  const s = executionStrategyOf(j)
  if (isActive) return 'live-progress'
  return s === 'workforce' ? 'plan-strip' : 'none'
}
eq('active workforce run shows live progress', traceFor(job({ execution_strategy: 'workforce' }), true), 'live-progress')
eq('active specialist run shows live progress', traceFor(job({ execution_strategy: 'specialist' }), true), 'live-progress')
eq('active direct run shows live progress', traceFor(job({ execution_strategy: 'direct' }), true), 'live-progress')
eq('finished direct run shows no Workforce plan', traceFor(job({ execution_strategy: 'direct' }), false), 'none')
eq('finished specialist run shows no Workforce plan', traceFor(job({ execution_strategy: 'specialist' }), false), 'none')
eq('finished workforce run keeps its plan strip', traceFor(job({ execution_strategy: 'workforce' }), false), 'plan-strip')

// --- When "dynamic" may be claimed (mirrors RightPanel) ----------------
const sourceFor = ({ workersState, j, name }) =>
  workersState === 'ready' && executionStrategyOf(j) === 'workforce' && name !== executionRoleOf(j)
    ? 'dynamic'
    : 'unknown'

const wf = job({ execution_strategy: 'workforce' })
const spec = job({ execution_strategy: 'specialist', execution_strategy_role: 'Research Agent' })
const direct = job({ execution_strategy: 'direct' })

eq(
  'roster still loading never claims dynamic',
  sourceFor({ workersState: 'loading', j: wf, name: 'Research Agent' }),
  'unknown',
)
eq(
  'roster failed to load never claims dynamic',
  sourceFor({ workersState: 'error', j: wf, name: 'Research Agent' }),
  'unknown',
)
eq(
  'specialist run never claims dynamic (no worker-creation path exists)',
  sourceFor({ workersState: 'ready', j: spec, name: 'Research Agent' }),
  'unknown',
)
eq(
  'direct run never claims dynamic',
  sourceFor({ workersState: 'ready', j: direct, name: 'Assistant' }),
  'unknown',
)
eq(
  'the router-named specialist is never called dynamic',
  sourceFor({ workersState: 'ready', j: spec, name: 'Research Agent' }),
  'unknown',
)
eq(
  'a genuinely unknown agent in a loaded workforce run is dynamic',
  sourceFor({ workersState: 'ready', j: wf, name: 'Translation Agent' }),
  'dynamic',
)

// The caption must only appear for a proven-dynamic agent.
const captionFor = (source, description) =>
  description || (source === 'dynamic' ? 'Dynamically created agent' : '')
eq('unknown source renders no claim', captionFor('unknown', ''), '')
eq('proven dynamic still gets its caption', captionFor('dynamic', ''), 'Dynamically created agent')
eq('a real description always wins', captionFor('unknown', 'A research specialist...'), 'A research specialist...')

console.log(failures ? `\n${failures} FAILED` : '\nAll execution-strategy UI checks passed')
process.exit(failures ? 1 : 0)
