import { useMemo, useState } from 'react'

import { cn } from '../lib/utils.js'

const VISIBLE_STEPS = 20

const PROCESS_LABELS = {
  'tool:search_raw': 'Web search',
  'tool:web_search': 'Web search',
  'tool:search_web': 'Web search',
  'tool:read_page': 'Web read',
  'tool:web_read': 'Web read',
  'rerank:rerank': 'Reranker',
  'llm:web_search': 'LLM · search query',
  'llm:web_select': 'LLM · URL selection',
  'llm:web_answer': 'LLM · answer',
  'llm:web_plan': 'LLM · research plan',
  'llm:web_coverage': 'LLM · coverage check',
  'llm:mail_plan': 'LLM · mail search plan',
  'llm:mail_select': 'LLM · mail selection',
  'tool:read_mailbox': 'Mailbox read',
  'llm:social_plan': 'LLM · Reddit search plan',
  'llm:social_select': 'LLM · post selection',
  'tool:read_social': 'Reddit read',
  'llm:jira_plan': 'LLM · Jira search plan',
  'llm:jira_select': 'LLM · issue selection',
  'llm:jira_answer': 'LLM · Jira issue summary',
  'tool:read_jira': 'Jira read',
  'llm:github_plan': 'LLM · GitHub search plan',
  'llm:github_select': 'LLM · repository selection',
  'llm:github_answer': 'LLM · GitHub summary',
  'tool:read_github': 'GitHub read',
  'llm:knowledge_plan': 'LLM · document search plan',
  'llm:knowledge_answer': 'LLM · document summary',
  'tool:search_documents': 'Document search',
  'llm:coding_write': 'LLM · code writing',
  'llm:coding_fix': 'LLM · code fix',
  'llm:coding_answer': 'LLM · result explanation',
  'tool:run_python': 'Code run',
  'tool:profile_data': 'Data file profiling',
  'llm:artifact_intent': 'LLM · artifact intent',
}

const KIND_DOT = {
  tool: 'bg-link',
  rerank: 'bg-muted-foreground',
  llm: 'bg-foreground/70',
}

export function formatMs(value) {
  const ms = Number(value)
  if (!Number.isFinite(ms) || ms < 0) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`
  const minutes = Math.floor(ms / 60_000)
  return `${minutes}m ${Math.round((ms % 60_000) / 1000)}s`
}

function processLabel(event) {
  const kind = String(event?.kind || '')
  const name = String(event?.name || '')
  const known = PROCESS_LABELS[`${kind}:${name}`]
  if (known) return known
  if (kind === 'llm') return `LLM · ${name.replace(/_/g, ' ')}`
  return name.replace(/_/g, ' ') || 'Step'
}

function startMs(event) {
  const parsed = Date.parse(event?.started_at || '')
  return Number.isFinite(parsed) ? parsed : null
}

function stepDetail(event) {
  if (event.detail) return String(event.detail)
  if (event.kind === 'llm' && (event.input_tokens || event.output_tokens)) {
    return `${Number(event.input_tokens || 0).toLocaleString()} in · ${Number(
      event.output_tokens || 0,
    ).toLocaleString()} out tokens`
  }
  return ''
}

function Dot({ kind, className }) {
  return (
    <span
      className={cn('h-1.5 w-1.5 shrink-0 rounded-full', KIND_DOT[kind] || 'bg-muted-foreground', className)}
      aria-hidden="true"
    />
  )
}

export function TimingLog({ job }) {
  const [expanded, setExpanded] = useState(false)
  const timeline = job?.usage?.timeline
  const events = useMemo(
    () =>
      (Array.isArray(timeline) ? timeline : []).filter(
        (event) => typeof event?.duration_ms === 'number',
      ),
    [timeline],
  )

  const groups = useMemo(() => {
    const byLabel = new Map()
    for (const event of events) {
      const label = processLabel(event)
      const group = byLabel.get(label) || {
        label,
        kind: event.kind,
        count: 0,
        totalMs: 0,
        failed: 0,
      }
      group.count += 1
      group.totalMs += event.duration_ms
      if (event.ok === false) group.failed += 1
      byLabel.set(label, group)
    }
    return [...byLabel.values()].sort((a, b) => b.totalMs - a.totalMs)
  }, [events])

  if (events.length === 0) {
    return (
      <p className="text-[13px] leading-relaxed text-muted-foreground">
        No timed steps have been recorded for this run yet.
      </p>
    )
  }

  const durationSeconds = job?.usage?.total_duration_seconds ?? job?.duration_seconds
  const runMs =
    typeof durationSeconds === 'number' && durationSeconds > 0 ? durationSeconds * 1000 : null
  const scaleMs = runMs || Math.max(...groups.map((group) => group.totalMs), 1)
  const starts = events.map(startMs).filter((value) => value !== null)
  const origin = starts.length ? Math.min(...starts) : null
  const hidden = Math.max(0, events.length - VISIBLE_STEPS)
  const shown = expanded ? events : events.slice(0, VISIBLE_STEPS)

  return (
    <div className="flex flex-col gap-4">
      <div>
        <ul className="flex flex-col gap-2.5">
          {groups.map((group) => {
            const share = Math.min(100, (group.totalMs / scaleMs) * 100)
            return (
              <li key={group.label} className="min-w-0">
                <div className="flex items-baseline justify-between gap-3 text-[12px]">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <Dot kind={group.kind} />
                    <span className="truncate">{group.label}</span>
                    <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground/70">
                      ×{group.count}
                    </span>
                  </span>
                  <span className="shrink-0 font-medium tabular-nums">
                    {formatMs(group.totalMs)}
                  </span>
                </div>
                <div className="mt-1 h-1 overflow-hidden rounded-full bg-muted/60">
                  <div
                    className={cn('h-full rounded-full', KIND_DOT[group.kind] || 'bg-muted-foreground')}
                    style={{ width: `${share}%` }}
                  />
                </div>
                <div className="mt-0.5 flex justify-between gap-2 text-[11px] tabular-nums text-muted-foreground/70">
                  <span>
                    avg {formatMs(group.totalMs / group.count)}
                    {group.failed ? ` · ${group.failed} failed` : ''}
                  </span>
                  {runMs && <span>{Math.round(share)}% of run</span>}
                </div>
              </li>
            )
          })}
        </ul>
        <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground/70">
          Steps can overlap (pages open in parallel), so shares may add up to more than 100%.
        </p>
      </div>

      <div className="min-w-0">
        <div className="mb-1.5 text-[11px] font-medium text-muted-foreground">
          Step log · {events.length} steps
        </div>
        <ol className="flex flex-col divide-y divide-border/40 rounded-lg border border-border/50">
          {shown.map((event, index) => {
            const start = startMs(event)
            const offset =
              origin !== null && start !== null ? `+${((start - origin) / 1000).toFixed(1)}s` : ''
            const detail = stepDetail(event)
            return (
              <li
                key={`${event.started_at}-${event.name}-${index}`}
                className="flex items-start gap-2 px-2.5 py-1.5 text-[11px]"
              >
                <span className="w-12 shrink-0 text-right tabular-nums text-muted-foreground/60">
                  {offset}
                </span>
                <Dot kind={event.kind} className="mt-[5px]" />
                <span className="min-w-0 flex-1">
                  <span
                    className={cn(
                      'block truncate',
                      event.ok === false ? 'text-destructive/80' : 'text-foreground/85',
                    )}
                  >
                    {processLabel(event)}
                    {event.ok === false ? ' · failed' : ''}
                  </span>
                  {detail && (
                    <span className="block truncate text-muted-foreground/60" title={detail}>
                      {detail}
                    </span>
                  )}
                </span>
                <span className="shrink-0 tabular-nums text-foreground/80">
                  {formatMs(event.duration_ms)}
                </span>
              </li>
            )
          })}
        </ol>
        {hidden > 0 && (
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            className={cn(
              'mt-2 w-full rounded-lg border border-border/50 bg-card/40 px-2.5 py-1.5',
              'text-[11px] text-muted-foreground transition-colors',
              'hover:bg-card/70 hover:text-foreground',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
            )}
            aria-expanded={expanded}
          >
            {expanded ? 'Show less' : `Show ${hidden} more`}
          </button>
        )}
      </div>
    </div>
  )
}
