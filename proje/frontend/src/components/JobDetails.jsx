import { ChevronDown } from 'lucide-react'

import { safeText, usageTokensReported } from '../stages.js'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from './ui/collapsible.jsx'

// Number(null) and Number('') are both 0, so a job with no recorded
// duration used to render a confident "0.0s" instead of admitting it had
// nothing to report -- which read as "this run took no time" on a job that
// visibly took minutes. Only a real number counts as a duration.
function formatDuration(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    return 'Not reported'
  }
  if (value < 60) return `${value.toFixed(1)}s`
  const minutes = Math.floor(value / 60)
  return `${minutes}m ${(value % 60).toFixed(0)}s`
}

// Fixed display order for known stages; anything else (e.g. a future stage,
// or a job run before per-stage tracking existed) is appended after these
// in whatever order the backend returned it, so nothing is silently dropped.
const STAGE_ORDER = [
  'routing',
  'planning',
  'assignment',
  'research',
  'document',
  'review',
  'outline',
  'quality',
  // Recovery stages are listed after the initial ones they mirror: a
  // non-zero recovery bucket means work was redone, which reads very
  // differently from the same number of initial calls.
  'recovery_planning',
  'recovery_assignment',
  'synthesis',
  'dynamic_worker',
  'manual_worker',
  'single_agent',
]

const STAGE_LABELS = {
  routing: 'Routing',
  planning: 'Planning',
  assignment: 'Assignment',
  research: 'Research',
  document: 'Document',
  review: 'Review',
  outline: 'Outline',
  coding: 'Coding',
  quality: 'Quality',
  recovery_planning: 'Planning (recovery)',
  recovery_assignment: 'Assignment (recovery)',
  synthesis: 'Synthesis',
  dynamic_worker: 'Dynamic worker',
  manual_worker: 'Manual worker',
  single_agent: 'Single agent',
  web_search: 'Web · query writing',
  web_select: 'Web · URL selection',
  web_answer: 'Web · answer',
  web_plan: 'Web · research plan',
  web_coverage: 'Web · coverage check',
  artifact_intent: 'Artifact intent',
}

function formatStageTime(seconds) {
  const value = Number(seconds)
  return value > 0 ? formatDuration(value) : '—'
}

function StageBreakdown({ byStage, tokensReported }) {
  const entries = Object.entries(byStage || {}).filter(
    ([, stats]) => Number(stats?.model_call_count) > 0,
  )
  if (!entries.length) return null

  entries.sort((a, b) => {
    const ai = STAGE_ORDER.indexOf(a[0])
    const bi = STAGE_ORDER.indexOf(b[0])
    if (ai === -1 && bi === -1) return a[0].localeCompare(b[0])
    if (ai === -1) return 1
    if (bi === -1) return -1
    return ai - bi
  })

  return (
    <Collapsible className="mt-3">
      <CollapsibleTrigger className="group flex w-full items-center gap-1.5 rounded-md py-1 text-xs font-medium text-muted-foreground transition-colors duration-150 hover:text-foreground">
        <ChevronDown
          className="h-3.5 w-3.5 transition-transform duration-150 group-data-[state=open]:rotate-180"
          aria-hidden="true"
        />
        Stage breakdown
      </CollapsibleTrigger>
      <CollapsibleContent className="overflow-hidden data-[state=closed]:animate-accordion-up data-[state=open]:animate-accordion-down">
        {/* Wide numeric table in a narrow panel: scroll it inside its own
            box rather than letting it widen the whole sidebar. */}
        <div className="scrollbar-slim mt-2 overflow-x-auto rounded-md border border-border">
          <table className="w-full border-collapse text-[11px] tabular-nums">
            <thead>
              <tr className="bg-muted/50 text-muted-foreground">
                <th className="px-2 py-1.5 text-left font-medium">Stage</th>
                <th className="px-2 py-1.5 text-right font-medium">Calls</th>
                <th className="px-2 py-1.5 text-right font-medium">Time</th>
                <th className="px-2 py-1.5 text-right font-medium">Input</th>
                <th className="px-2 py-1.5 text-right font-medium">Output</th>
                <th className="px-2 py-1.5 text-right font-medium">Total</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([stage, stats]) => (
                <tr key={stage} className="border-t border-border">
                  <td className="whitespace-nowrap px-2 py-1.5">
                    {STAGE_LABELS[stage] || stage}
                  </td>
                  <td className="px-2 py-1.5 text-right">
                    {Number(stats.model_call_count || 0).toLocaleString()}
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right">
                    {formatStageTime(stats.duration_seconds)}
                  </td>
                  <td className="px-2 py-1.5 text-right text-muted-foreground">
                    {tokensReported
                      ? Number(stats.input_tokens || 0).toLocaleString()
                      : 'Not reported'}
                  </td>
                  <td className="px-2 py-1.5 text-right text-muted-foreground">
                    {tokensReported
                      ? Number(stats.output_tokens || 0).toLocaleString()
                      : 'Not reported'}
                  </td>
                  <td className="px-2 py-1.5 text-right">
                    {tokensReported
                      ? Number(stats.total_tokens || 0).toLocaleString()
                      : 'Not reported'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}

const STRATEGY_LABELS = {
  single: 'Single',
  direct: 'Direct',
  specialist: 'Specialist',
  workforce: 'Workforce',
}

// A "workforce" strategy reached by timeout/error is a fallback, not a
// routing decision -- surfacing that distinction here is what makes a
// degraded router visible instead of looking like normal operation.
const ROUTING_OUTCOME_NOTES = {
  timeout: 'router timed out',
  error: 'router unavailable',
}

function strategyDisplay(usage) {
  const strategy = usage.execution_strategy
  if (!strategy) return null
  const label = STRATEGY_LABELS[strategy] || strategy
  const base = usage.execution_strategy_role
    ? `${label} (${usage.execution_strategy_role})`
    : label
  const note = ROUTING_OUTCOME_NOTES[usage.routing_outcome]
  return note ? `${base} — ${note}` : base
}

/** One label/value row. Hierarchy is weight+colour, not a box per metric. */
function Metric({ label, value, emphasis = false }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <dt className="shrink-0 text-xs text-muted-foreground">{label}</dt>
      <dd
        className={
          emphasis
            ? 'min-w-0 text-right text-[13px] font-semibold tabular-nums'
            : 'min-w-0 text-right text-[13px] tabular-nums text-foreground/90'
        }
      >
        {value}
      </dd>
    </div>
  )
}

export function UsageSummary({ job, compact = false }) {
  const usage = job?.usage || {}
  const provider = usage.provider || job?.model_provider
  const model = usage.model_name || job?.model_name
  const calls = usage.model_call_count ?? job?.model_calls ?? 0
  const duration = usage.total_duration_seconds ?? job?.duration_seconds
  const tokensReported = usageTokensReported(usage)
  const input = Number(usage.input_tokens || 0)
  const output = Number(usage.output_tokens || 0)
  const total = Number(usage.total_tokens || input + output)
  const cost = Number(usage.estimated_cost_usd)
  const hasCost =
    usage.estimated_cost_usd !== null &&
    usage.estimated_cost_usd !== undefined &&
    Number.isFinite(cost)

  const hasData = Boolean(provider || model || calls || duration || total)

  // Long model ids like "deepseek/deepseek-v4-flash" have no spaces, so
  // `overflow-wrap: anywhere` (previously the only thing keeping them in
  // their box) breaks mid-word ("deepse" / "ek-v4-flash"). A zero-width
  // space after each "/" gives the browser a sensible break point instead,
  // so long names wrap at the segment boundary the way a human would.
  const modelDisplay = safeText(model, 'Not reported').replace(/\//g, '/​')

  const metrics = [
    ['Model', modelDisplay],
    ['Provider', safeText(provider, 'Not reported')],
    ['Model calls', Number(calls).toLocaleString()],
    ['Input tokens', tokensReported ? input.toLocaleString() : 'Not reported'],
    ['Output tokens', tokensReported ? output.toLocaleString() : 'Not reported'],
    ['Total tokens', tokensReported ? total.toLocaleString() : 'Not reported'],
    ['Estimated cost', hasCost ? `$${cost.toFixed(6)}` : 'Not reported'],
    ['Duration', formatDuration(duration)],
  ]
  const strategyLabel = strategyDisplay(usage)
  if (strategyLabel) {
    metrics.push(['Execution strategy', strategyLabel])
  }
  // What the run was actually able to do, and why it stopped. These are the
  // two questions asked first when a job behaves unexpectedly, and both were
  // previously only answerable by reading server logs.
  if (Array.isArray(usage.capabilities) && usage.capabilities.length > 0) {
    metrics.push(['Capabilities', usage.capabilities.join(', ')])
  }
  if (usage.tool_call_count) {
    metrics.push([
      'Tool calls',
      usage.tool_error_count
        ? `${usage.tool_call_count} (${usage.tool_error_count} failed)`
        : String(usage.tool_call_count),
    ])
  }
  if (usage.artifact_count) {
    metrics.push(['Files produced', String(usage.artifact_count)])
  }

  if (compact) {
    if (!hasData) {
      return (
        <p className="text-[13px] leading-relaxed text-muted-foreground">
          No usage has been reported for this run yet.
        </p>
      )
    }
    // The model line leads, because it is the one fact here a reader is
    // likely to be checking; the counts follow as a plain label/value list.
    // It is no longer boxed -- the panel section heading above already
    // frames this block, so a second frame was chrome for its own sake.
    const compactMetrics = metrics.slice(2)
    return (
      <div>
        <div className="mb-3">
          <div className="break-words text-[13px] font-medium leading-snug">
            {modelDisplay}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {safeText(provider, 'Provider not reported')}
          </div>
        </div>
        <dl className="divide-y divide-border/50">
          {compactMetrics.map(([label, value]) => (
            <Metric
              key={label}
              label={label}
              value={value}
              emphasis={label === 'Total tokens'}
            />
          ))}
        </dl>
        <StageBreakdown byStage={usage.by_stage} tokensReported={tokensReported} />
      </div>
    )
  }

  if (!hasData) return null

  return (
    <section aria-labelledby="usage-title">
      <h2 id="usage-title" className="text-sm font-semibold tracking-tight">
        Usage
      </h2>
      <p className="mt-0.5 text-[13px] text-muted-foreground">
        Provider-reported usage aggregated across model calls.
      </p>
      <dl className="mt-3 divide-y divide-border/60">
        {metrics.map(([label, value]) => (
          <Metric key={label} label={label} value={value} />
        ))}
      </dl>
      <StageBreakdown byStage={usage.by_stage} tokensReported={tokensReported} />
    </section>
  )
}

// Cancelling now happens from the composer's send/stop toggle button (see
// Composer.jsx), which is always visible instead of only appearing inline
// under the thinking indicator -- this standalone control is no longer
// used anywhere.
