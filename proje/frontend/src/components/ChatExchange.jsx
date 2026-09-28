import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import hljs from 'highlight.js/lib/core'
import python from 'highlight.js/lib/languages/python'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import bash from 'highlight.js/lib/languages/bash'
import json_ from 'highlight.js/lib/languages/json'
import yaml from 'highlight.js/lib/languages/yaml'
import sql from 'highlight.js/lib/languages/sql'
import css from 'highlight.js/lib/languages/css'
import xml from 'highlight.js/lib/languages/xml'
import dockerfile from 'highlight.js/lib/languages/dockerfile'
import markdown from 'highlight.js/lib/languages/markdown'
import {
  AlertCircle,
  CalendarClock,
  Check,
  ChevronDown,
  Copy,
  Download,
  ExternalLink,
  FileSpreadsheet,
  FileText,
  FileJson,
  FolderArchive,
  Globe,
  Loader2,
  Maximize2,
  Paperclip,
  Play,
  Square,
  Users,
} from 'lucide-react'

import {
  artifactBundleZipUrl,
  artifactUrl,
  executeCode,
  exportJobDocument,
  uploadUrl,
} from '../api.js'
import { AgentIcon } from '../agentIcons.jsx'
import {
  buildJobExport,
  ERROR_JOB_STATUSES,
  executionStrategyOf,
  getFinalAnswer,
  getJobTasks,
  getTaskRuntime,
  isLiveExchange,
  safeFilename,
  safeText,
  stableStreamingMarkdown,
  userFacingFailure,
  userFacingProgress,
} from '../stages.js'
import { cn } from '../lib/utils.js'
import JiraActionCard from './JiraActionCard.jsx'
import { MarkSlot, StatusDot } from './ui/badge.jsx'
import { Button } from './ui/button.jsx'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from './ui/collapsible.jsx'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from './ui/dialog.jsx'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from './ui/dropdown-menu.jsx'
import { SimpleTooltip } from './ui/tooltip.jsx'

hljs.registerLanguage('python', python)
hljs.registerLanguage('py', python)
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('js', javascript)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('ts', typescript)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('sh', bash)
hljs.registerLanguage('shell', bash)
hljs.registerLanguage('json', json_)
hljs.registerLanguage('yaml', yaml)
hljs.registerLanguage('yml', yaml)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('css', css)
hljs.registerLanguage('html', xml)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('dockerfile', dockerfile)
hljs.registerLanguage('docker', dockerfile)
hljs.registerLanguage('markdown', markdown)
hljs.registerLanguage('md', markdown)

const RUNNABLE_LANGUAGES = new Set(['python', 'py'])

/*
 * What belongs in the conversation, and what does not.
 *
 * The conversation carries the question and the answer. Everything else a
 * run produces -- per-task intermediate outputs, individual tool calls,
 * input file listings, token counts -- is execution detail, and it now
 * lives exclusively in the execution panel. Previously the chat rendered
 * all of it inline: a live Workforce run pushed four nested collapsibles,
 * every agent's scratch output and a numbered tool log between the user's
 * question and the answer to it, so reading a thread meant scrolling past
 * a debugging view to reach the content.
 *
 * What remains inline is only what answers "is this working, and what did
 * it do": a compact live status while the run is active, and one quiet
 * summary line under the finished answer that expands into the per-agent
 * breakdown on demand.
 */

const STATE_TONE = {
  done: 'text-success',
  failed: 'text-destructive',
  working: 'text-link',
  waiting: 'text-muted-foreground/60',
}

function formatElapsed(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const remaining = total % 60
  if (hours > 0)
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(remaining).padStart(2, '0')}`
  return `${minutes}:${String(remaining).padStart(2, '0')}`
}

/** Human duration for the settled summary line ("3m 35s"), not a timer. */
function formatDuration(seconds) {
  const total = Math.round(Number(seconds))
  if (!Number.isFinite(total) || total < 0) return null
  if (total < 60) return `${total}s`
  return `${Math.floor(total / 60)}m ${total % 60}s`
}

function useElapsedSeconds(job, active) {
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!active) return undefined
    const timer = window.setInterval(() => setTick((value) => value + 1), 1000)
    return () => window.clearInterval(timer)
  }, [active])

  if (!active && Number.isFinite(job.duration_seconds)) {
    return job.duration_seconds
  }
  // started_at is execution start, the same origin duration_seconds is
  // measured from; created_at (submission) is only the fallback for jobs
  // still queued or predating the field, so the counter never jumps
  // backwards when the run completes.
  const origin = Number(job.started_at) || Number(job.created_at)
  if (!Number.isFinite(origin)) return 0
  return Math.max(0, Date.now() / 1000 - origin)
}

// Same marks, same slot, same tones as the execution panel's task list --
// the two are the same information in two places, so they are drawn the
// same way rather than each inventing its own iconography.
function TaskStateIcon({ statusClass }) {
  return (
    <MarkSlot>
      {statusClass === 'done' && (
        <Check className={cn('h-3.5 w-3.5', STATE_TONE.done)} />
      )}
      {statusClass === 'failed' && (
        <AlertCircle className={cn('h-3.5 w-3.5', STATE_TONE.failed)} />
      )}
      {statusClass === 'working' && (
        <Loader2 className={cn('h-3.5 w-3.5 animate-spin', STATE_TONE.working)} />
      )}
      {statusClass !== 'done' &&
        statusClass !== 'failed' &&
        statusClass !== 'working' && (
          <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/35" />
        )}
    </MarkSlot>
  )
}

/**
 * Live run status: one semantic progress line and elapsed time.
 * Task-level detail lives in the execution panel, not here.
 */
function LiveRun({ job, tasks }) {
  const elapsed = useElapsedSeconds(job, true)
  const { phase, label } = userFacingProgress(job, tasks)
  const isThinking = phase === 'thinking'

  return (
    <div
      className="flex items-center gap-2 py-1"
      role="status"
      aria-live="polite"
      aria-label="Run status"
    >
      {isThinking ? (
        <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-muted-foreground" />
      ) : (
        <StatusDot pulse className="h-2 w-2 shrink-0 text-link" />
      )}
      <span className="text-[13px] text-muted-foreground">{label}</span>
      {/* The wait is the whole reason this matters: a deep run takes ten
          minutes and the composer's toggle is behind you by then, so
          without this there is no way to tell which kind of run you are
          waiting on. */}
      {job?.deep_research && (
        <span className="shrink-0 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-link">
          deep research
        </span>
      )}
      {!isThinking && (
        <span className="ml-auto shrink-0 font-mono text-xs tabular-nums text-muted-foreground/80">
          {formatElapsed(elapsed)}
        </span>
      )}
    </div>
  )
}

/**
 * The finished exchange's footer: what the run was, on the left, and what
 * you can do with its answer, on the right.
 *
 * The left half is the run's one-line receipt and the only place per-agent
 * detail is reachable from the conversation -- collapsed it is a sentence
 * of grey text, expanded it lists who did what, full width beneath the
 * row. A Single Agent run has no team and no plan, so it contributes
 * nothing rather than a summary of one, and the actions simply sit alone.
 */
// Bytes as a human reads them. Deliberately terse (no decimal for KB) --
// this is a secondary detail on a row whose primary content is the filename.
function formatBytes(bytes) {
  const size = Number(bytes)
  if (!Number.isFinite(size) || size <= 0) return ''
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

const ARTIFACT_ICONS = {
  xlsx: FileSpreadsheet,
  csv: FileSpreadsheet,
  json: FileJson,
  html: Globe,
  web_bundle: Globe,
  code_bundle: FolderArchive,
}

/**
 * The files a run actually produced.
 *
 * This exists because the deliverable of a request like "give me the Excel
 * file" is the FILE -- an answer that describes it is a failed answer. The
 * backend lists these by scanning the job workspace, so everything here is a
 * real file on disk; a run that only claimed to create something contributes
 * nothing and this block does not render at all.
 *
 * Kept deliberately quiet: one bordered container (these are objects, not
 * prose, so one boundary is doing real work), and inside it the hierarchy is
 * carried by type weight and color rather than by a card per row. A generated
 * web page opens in a new tab; everything else downloads, which is what the
 * server's Content-Disposition already asks the browser to do.
 */
// Charts the Coding Agent saved are shown, not only listed: a chart behind a
// download link is a chart nobody looks at.
const PREVIEW_KINDS = new Set(['png', 'jpg', 'jpeg'])

function ProducedFiles({ job, artifacts }) {
  if (artifacts.length === 0) return null
  const previews = artifacts.filter(
    (artifact) => PREVIEW_KINDS.has(String(artifact.kind || '').toLowerCase()) && !artifact.bundle,
  )

  return (
    <section className="mt-5 rounded-lg border border-border/70 p-3.5" aria-label="Files produced by this run">
      <h3 className="text-xs font-medium text-muted-foreground">
        {artifacts.length === 1 ? '1 file produced' : `${artifacts.length} files produced`}
      </h3>
      {previews.length > 0 && (
        <div className="mt-2.5 grid grid-cols-1 gap-2.5 sm:grid-cols-2">
          {previews.map((artifact) => (
            <a
              key={`preview-${artifact.path}`}
              href={artifactUrl(job.job_id, artifact.download_path)}
              download={artifact.filename}
              className="block overflow-hidden rounded-md border border-border/60 bg-white transition-opacity duration-150 hover:opacity-90"
              title={safeText(artifact.filename, 'chart')}
            >
              <img
                src={artifactUrl(job.job_id, artifact.download_path)}
                alt={safeText(artifact.filename, 'chart')}
                loading="lazy"
                className="h-auto w-full"
              />
            </a>
          ))}
        </div>
      )}
      <ul className="mt-2.5 flex flex-col gap-1">
        {artifacts.map((artifact) => {
          const kind = String(artifact.kind || '').toLowerCase()
          const Icon = ARTIFACT_ICONS[kind] || FileText
          const isPage = kind === 'web_bundle' || kind === 'html'
          const size = formatBytes(artifact.size_bytes)
          // A bundle (create_web_bundle / create_code_bundle) is already
          // reported as ONE entry with its member list attached -- show that
          // it is a project, not a single file, without a per-member row.
          const memberCount = Array.isArray(artifact.files) ? artifact.files.length : 0
          const isBundle = Boolean(artifact.bundle) && memberCount > 0 && artifact.zip_download_path

          // A bundle's whole point is the project, not its entry file alone --
          // downloading only server.js from an API bundle would be misleading.
          // So a bundle's primary action is the ZIP; a web bundle also keeps
          // "open in browser" as a second, explicit action for the page itself.
          if (isBundle) {
            return (
              <li key={artifact.path} className="group -mx-1.5 flex items-center gap-2.5 rounded-md px-1.5 py-1.5 transition-colors duration-150 hover:bg-accent">
                <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">
                  {safeText(artifact.bundle?.split('/').pop(), 'project')}
                </span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {memberCount} {memberCount === 1 ? 'file' : 'files'}
                </span>
                {size && (
                  <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{size}</span>
                )}
                {isPage && (
                  <a
                    href={artifactUrl(job.job_id, artifact.download_path)}
                    target="_blank"
                    rel="noreferrer"
                    className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground hover:text-link hover:underline"
                    title="Open the app in a new tab"
                  >
                    <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
                    Open
                  </a>
                )}
                <a
                  href={artifactBundleZipUrl(job.job_id, artifact.zip_download_path)}
                  download={`${artifact.bundle?.split('/').pop() || 'project'}.zip`}
                  className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground hover:text-link hover:underline"
                  title="Download the whole project as a ZIP"
                >
                  <Download className="h-3.5 w-3.5" aria-hidden="true" />
                  Download ZIP
                </a>
              </li>
            )
          }

          return (
            <li key={artifact.path}>
              <a
                href={artifactUrl(job.job_id, artifact.download_path)}
                target={isPage ? '_blank' : undefined}
                rel={isPage ? 'noreferrer' : undefined}
                download={isPage ? undefined : artifact.filename}
                className="group -mx-1.5 flex items-center gap-2.5 rounded-md px-1.5 py-1.5 transition-colors duration-150 hover:bg-accent"
              >
                <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate text-[13px] text-foreground group-hover:text-link group-hover:underline">
                  {safeText(artifact.filename, 'file')}
                </span>
                {size && (
                  <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{size}</span>
                )}
                {isPage ? (
                  <ExternalLink className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                ) : (
                  <Download className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                )}
              </a>
            </li>
          )
        })}
      </ul>
    </section>
  )
}

function RunFooter({ job, tasks, actions }) {
  const strategy = executionStrategyOf(job)
  if (strategy !== 'workforce' || tasks.length === 0) {
    return <div className="mt-4 flex items-center justify-end">{actions}</div>
  }

  const entries = tasks.map((task) => ({ task, runtime: getTaskRuntime(job, task) }))
  const byAgent = new Map()
  entries.forEach((entry) => {
    if (!entry.runtime.agentAssigned) return
    if (!byAgent.has(entry.runtime.agent)) byAgent.set(entry.runtime.agent, [])
    byAgent.get(entry.runtime.agent).push(entry)
  })
  const unassigned = entries.filter(({ runtime }) => !runtime.agentAssigned)
  const duration = formatDuration(
    job.usage?.total_duration_seconds ?? job.duration_seconds,
  )

  const facts = [
    // The composer's toggle clears itself on send (it is per-message), so
    // this is the only place that says which kind of run you are watching.
    // It matters: deep is 30 searches to 3, and ten minutes to one.
    job.deep_research ? 'deep research' : null,
    `${tasks.length} ${tasks.length === 1 ? 'task' : 'tasks'}`,
    byAgent.size > 0
      ? `${byAgent.size} ${byAgent.size === 1 ? 'agent' : 'agents'}`
      : null,
    duration,
  ].filter(Boolean)

  return (
    <Collapsible className="mt-4">
      <div className="flex items-center gap-2">
        <CollapsibleTrigger className="group inline-flex min-w-0 items-center gap-1.5 rounded-md px-1.5 py-1 text-xs text-muted-foreground transition-colors duration-150 hover:bg-accent hover:text-foreground">
          <span className="truncate">Workforce · {facts.join(' · ')}</span>
          <ChevronDown
            className="h-3.5 w-3.5 shrink-0 transition-transform duration-150 group-data-[state=open]:rotate-180"
            aria-hidden="true"
          />
        </CollapsibleTrigger>
        <div className="ml-auto shrink-0">{actions}</div>
      </div>
      <CollapsibleContent className="overflow-hidden data-[state=closed]:animate-accordion-up data-[state=open]:animate-accordion-down">
        <div className="mt-2.5 flex flex-col gap-3.5 rounded-lg border border-border bg-card/60 p-4">
          {[...byAgent.entries()].map(([agent, agentTasks]) => (
            <div key={agent}>
              {/* MarkSlot (w-4) + gap-2 puts the agent's name at 24px, and
                  the task list is indented by exactly that -- so each task
                  mark hangs under the name of the agent that ran it rather
                  than under its icon. Both values are on the scale; the
                  indent was previously a hand-measured 22px. */}
              <div className="flex items-center gap-2 text-[13px] font-medium leading-5 text-foreground">
                <MarkSlot className="text-muted-foreground">
                  <AgentIcon name={agent} width={14} height={14} />
                </MarkSlot>
                {agent}
              </div>
              <ul className="mt-1 flex flex-col pl-6">
                {agentTasks.map(({ task, runtime }) => (
                  <li
                    key={task.renderKey}
                    className="flex items-start gap-2 text-[13px] leading-5 text-muted-foreground"
                  >
                    <TaskStateIcon statusClass={runtime.statusClass} />
                    <span className="min-w-0 flex-1">{task.title}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {unassigned.length > 0 && (
            <p className="text-xs text-muted-foreground">
              {unassigned.length} {unassigned.length === 1 ? 'task was' : 'tasks were'}{' '}
              never assigned to an agent.
            </p>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}

const FAILURE_MESSAGES = {
  cancelled: 'This message was stopped before it completed.',
  timed_out: 'This message timed out before it completed.',
  interrupted: 'The server restarted before this message finished.',
  error: 'This message could not be completed.',
  failed: 'This message could not be completed.',
}

function parseErrorLine(stderr) {
  if (!stderr) return null
  const m = /line (\d+)/i.exec(stderr)
  return m ? parseInt(m[1], 10) : null
}

// react-markdown hands `code` both fenced blocks and inline spans. Only a
// fenced block carries a language- class or a newline; inline code is left
// completely alone so `uvicorn` in a sentence stays a plain <code>.
// `node` is react-markdown's mdast node; it must be pulled out of the
// props spread or React forwards it to the DOM as node="[object Object]".
function CodeBlock({ node, className, children, ...rest }) {
  const [copied, setCopied] = useState(false)
  const [execState, setExecState] = useState(null) // null | 'running' | {ok, stdout, stderr, error, duration_seconds}
  const abortRef = useRef(null)
  const rawSource = String(children ?? '')
  // Backend JSON double-escaping can leave literal \n instead of real newlines.
  // Only normalize when the source has no real newlines but contains \n escapes.
  const needsUnescape = !rawSource.includes('\n') && rawSource.includes('\\n')
  const source = needsUnescape
    ? rawSource.replace(/\\n/g, '\n').replace(/\\t/g, '\t')
    : rawSource
  const trimmed = source.replace(/\n$/, '')
  const language = /language-([\w+-]+)/.exec(className || '')?.[1] || null

  useEffect(() => {
    if (!copied) return undefined
    const timer = window.setTimeout(() => setCopied(false), 1600)
    return () => window.clearTimeout(timer)
  }, [copied])

  const highlighted = useMemo(() => {
    if (language) {
      const lang = language.toLowerCase()
      if (hljs.getLanguage(lang)) {
        try {
          return hljs.highlight(trimmed, { language: lang }).value
        } catch { /* fall through */ }
      }
    }
    // Auto-detect when no language tag is provided
    try {
      const result = hljs.highlightAuto(trimmed)
      if (result.relevance >= 3 && result.value) return result.value
    } catch { /* fall through */ }
    // Last resort: try as python (most common from Coding Agent)
    if (hljs.getLanguage('python')) {
      try { return hljs.highlight(trimmed, { language: 'python' }).value }
      catch { /* fall through */ }
    }
    return null
  }, [trimmed, language])

  if (!language && !source.includes('\n')) {
    return (
      <code className={className} {...rest}>
        {children}
      </code>
    )
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(trimmed)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  const canRun = RUNNABLE_LANGUAGES.has((language || '').toLowerCase())

  const run = async () => {
    if (execState === 'running') {
      abortRef.current?.abort()
      setExecState(null)
      return
    }
    const controller = new AbortController()
    abortRef.current = controller
    setExecState('running')
    try {
      const result = await executeCode(
        { code: trimmed, language: 'python' },
        { signal: controller.signal },
      )
      setExecState(result)
    } catch (err) {
      if (err.name === 'AbortError') {
        setExecState(null)
      } else {
        setExecState({ ok: false, stdout: '', stderr: '', error: err.message })
      }
    }
  }

  const isRunning = execState === 'running'
  const hasResult = execState && execState !== 'running'

  const errorLine = hasResult && !execState.ok
    ? (execState.line ?? parseErrorLine(execState.stderr))
    : null

  const lines = trimmed.split('\n')
  const lineCount = lines.length
  const gutterWidth = String(lineCount).length

  const buildLineElements = useCallback(() => {
    if (highlighted) {
      const parts = highlighted.split('\n')
      return parts.map((html, i) => {
        const ln = i + 1
        const isErr = errorLine === ln
        return (
          <div key={ln} className={cn('code-line', isErr && 'code-line-error')}>
            <span className="code-line-number" style={{ width: `${gutterWidth}ch` }}>
              {ln}
            </span>
            <span
              className="code-line-content"
              dangerouslySetInnerHTML={{ __html: html || '\n' }}
            />
          </div>
        )
      })
    }
    return lines.map((text, i) => {
      const ln = i + 1
      const isErr = errorLine === ln
      return (
        <div key={ln} className={cn('code-line', isErr && 'code-line-error')}>
          <span className="code-line-number" style={{ width: `${gutterWidth}ch` }}>
            {ln}
          </span>
          <span className="code-line-content">{text || '\n'}</span>
        </div>
      )
    })
  }, [highlighted, lines, errorLine, gutterWidth])

  return (
    <div className="code-block-container group/code">
      <div className="code-block-header">
        <span className="code-block-lang">
          {language || 'text'}
        </span>
        <div className="flex items-center gap-1">
          {canRun && (
            <button
              type="button"
              onClick={run}
              aria-label={isRunning ? 'Stop execution' : 'Run code'}
              className={cn(
                'code-block-action',
                isRunning
                  ? 'text-amber-400 opacity-100'
                  : 'text-emerald-400 opacity-0 group-hover/code:opacity-100',
              )}
            >
              {isRunning ? (
                <>
                  <Square className="h-3 w-3" aria-hidden="true" />
                  Stop
                </>
              ) : (
                <>
                  <Play className="h-3 w-3" aria-hidden="true" />
                  Run
                </>
              )}
            </button>
          )}
          <button
            type="button"
            onClick={copy}
            aria-label={`Copy ${language || 'code'} snippet`}
            className="code-block-action opacity-0 group-hover/code:opacity-100"
          >
            {copied ? (
              <>
                <Check className="h-3 w-3" aria-hidden="true" />
                Copied
              </>
            ) : (
              <>
                <Copy className="h-3 w-3" aria-hidden="true" />
                Copy
              </>
            )}
          </button>
        </div>
      </div>
      <div className="code-block-body scrollbar-slim">
        <pre className="code-block-pre">
          <code className={className}>
            {buildLineElements()}
          </code>
        </pre>
      </div>
      {isRunning && (
        <div className="code-block-output">
          <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            Running…
          </div>
        </div>
      )}
      {hasResult && (
        <div className="code-block-output">
          <div className="code-block-output-header">
            <span className={execState.ok ? 'text-emerald-400' : 'text-red-400'}>
              {execState.ok ? 'Output' : 'Error'}
            </span>
            {execState.duration_seconds != null && (
              <span className="text-muted-foreground/60">
                {execState.duration_seconds.toFixed(2)}s
              </span>
            )}
          </div>
          {(execState.stdout || (!execState.ok && !execState.stderr && execState.error)) && (
            <pre className="code-block-output-body">
              {execState.stdout || execState.error}
            </pre>
          )}
          {execState.stderr && (
            <pre className="code-block-output-body code-block-stderr">
              {execState.stderr}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

function download(content, filename, type) {
  const blob = new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

function isInternalArtifactHref(href) {
  if (!href || typeof href !== 'string') return false
  const value = href.trim()
  if (/^(https?:|mailto:|#)/i.test(value)) return false
  return /(?:^|\/)artifacts\/|\.(?:xlsx|xls|csv|json|docx|pptx|pdf|zip|html)$/i.test(value)
}

function cellText(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(cellText).join('')
  if (node?.props?.children != null) return cellText(node.props.children)
  return ''
}

function tableToMatrix(children) {
  const rows = []
  const walk = (nodes) => {
    if (nodes == null) return
    const list = Array.isArray(nodes) ? nodes : [nodes]
    for (const node of list) {
      if (!node || typeof node !== 'object') continue
      const type = node.type
      if (type === 'tr' || type?.displayName === 'tr') {
        const cells = []
        const cellNodes = Array.isArray(node.props?.children)
          ? node.props.children
          : [node.props?.children]
        for (const cell of cellNodes) {
          if (!cell || typeof cell !== 'object') continue
          const ct = cell.type
          if (ct === 'th' || ct === 'td' || ct?.displayName === 'th' || ct?.displayName === 'td') {
            cells.push(cellText(cell).trim())
          }
        }
        if (cells.length) rows.push(cells)
        continue
      }
      if (node.props?.children != null) walk(node.props.children)
    }
  }
  walk(children)
  return rows
}

function matrixToTsv(matrix) {
  return matrix.map((row) => row.join('\t')).join('\n')
}

function matrixToCsv(matrix) {
  return matrix
    .map((row) =>
      row
        .map((cell) => {
          const value = String(cell ?? '')
          if (/[",\n]/.test(value)) return `"${value.replace(/"/g, '""')}"`
          return value
        })
        .join(','),
    )
    .join('\n')
}

function MarkdownTable({ children, ...props }) {
  const [copyState, setCopyState] = useState('idle')
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    if (copyState !== 'copied') return undefined
    const timer = window.setTimeout(() => setCopyState('idle'), 1600)
    return () => window.clearTimeout(timer)
  }, [copyState])

  const matrix = useMemo(() => tableToMatrix(children), [children])

  const copyTable = async () => {
    const text = matrixToTsv(matrix)
    if (!text) return
    try {
      await navigator.clipboard.writeText(text)
      setCopyState('copied')
    } catch {
      setCopyState('error')
    }
  }

  const downloadCsv = () => {
    const csv = matrixToCsv(matrix)
    if (!csv) return
    download(csv, 'table.csv', 'text/csv;charset=utf-8')
  }

  const tableEl = <table {...props}>{children}</table>

  return (
    <div className="md-table group/table">
      <div className="md-table-toolbar">
        <SimpleTooltip label={copyState === 'copied' ? 'Copied' : 'Copy table'}>
          <button
            type="button"
            className="md-table-action"
            onClick={copyTable}
            aria-label="Copy table"
          >
            {copyState === 'copied' ? (
              <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" />
            ) : (
              <Copy className="h-3.5 w-3.5" aria-hidden="true" />
            )}
          </button>
        </SimpleTooltip>
        <SimpleTooltip label="Download CSV">
          <button
            type="button"
            className="md-table-action"
            onClick={downloadCsv}
            aria-label="Download table as CSV"
          >
            <Download className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </SimpleTooltip>
        <SimpleTooltip label="Expand table">
          <button
            type="button"
            className="md-table-action"
            onClick={() => setExpanded(true)}
            aria-label="Expand table"
          >
            <Maximize2 className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </SimpleTooltip>
      </div>
      <div className="table-scroll">{tableEl}</div>

      <Dialog open={expanded} onOpenChange={setExpanded}>
        <DialogContent className="max-h-[90vh] max-w-4xl overflow-hidden p-0 sm:rounded-xl">
          <DialogHeader className="border-b border-border px-5 py-3.5">
            <DialogTitle className="text-[14px]">Table</DialogTitle>
          </DialogHeader>
          <div className="prose-answer max-h-[calc(90vh-4rem)] overflow-auto p-4">
            <div className="table-scroll">
              <table {...props}>{children}</table>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}

// `pre` is overridden to a passthrough because CodeBlock renders its own
// <pre> inside the container: leaving remark's wrapper in place would nest
// <pre><div><pre>, which is invalid and double-boxes every snippet.
// `table` is wrapped so a wide table scrolls inside its own box instead of
// widening the whole answer column.
const MARKDOWN_COMPONENTS = {
  code: CodeBlock,
  pre: ({ children }) => <>{children}</>,
  table: ({ node, ...props }) => <MarkdownTable {...props} />,
  a: ({ href, children, node, ...props }) => {
    if (isInternalArtifactHref(href)) {
      return <span>{children}</span>
    }
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
        {children}
      </a>
    )
  },
  h2: ({ children, ...props }) => {
    const text = cellText(children).trim().toLowerCase()
    if (text === 'kaynaklar' || text === 'kaynakça' || text === 'sources') {
      return (
        <h2 className="prose-sources-heading" {...props}>
          {children}
        </h2>
      )
    }
    return <h2 {...props}>{children}</h2>
  },
  p: ({ children, ...props }) => {
    // "Kaynaklar:" as a plain paragraph (system appends this form).
    const text = cellText(children).trim()
    if (/^kaynak(?:lar|ça|ca)?\s*:?\s*$/i.test(text) || /^sources\s*:?\s*$/i.test(text)) {
      return (
        <p className="prose-sources-heading" {...props}>
          {children}
        </p>
      )
    }
    return <p {...props}>{children}</p>
  },
}

function Markdown({ children }) {
  // Promote a plain first title line ("… Gol Krallığı – İlk 10") to h1 so
  // chat answers read like the research brief, not a wall of body text.
  const source = typeof children === 'string' ? children : String(children ?? '')
  const promoted = promoteLeadingTitle(source)
  return (
    <div className="prose-answer">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>
        {promoted}
      </ReactMarkdown>
    </div>
  )
}

function promoteLeadingTitle(markdown) {
  const text = (markdown || '').replace(/^\uFEFF/, '')
  if (!text.trim()) return text
  const lines = text.split('\n')
  let first = lines[0].trim()
  // A title the model emphasised ("**Erzincan 1 Haftalık Namaz Vakitleri
  // (03-09 Eylül 2026)**") is still a title. It only ever surfaced as small
  // bold text because the leading "*" put it in the same bucket as list
  // items -- and it is the line that now leads the answer, since the server
  // drops the opening echo of the user's own request.
  const bolded = first.match(/^\*\*(.+?)\*\*$/)
  if (bolded) {
    first = bolded[1].trim()
  } else if (
    !first || first.startsWith('#') || first.startsWith('|') ||
    first.startsWith('-') || first.startsWith('*')
  ) {
    return text
  }
  // Status / file-handoff lines must stay body text — never hero headings.
  if (
    /^(indirilebilir|downloadable|dosya|file(s)?\s+produced|artifacts\/)/i.test(first) ||
    first.includes('oluşturuldu') ||
    first.includes('olusturuldu')
  ) {
    return text
  }
  if (first.length > 110 || first.includes('://')) return text
  lines[0] = `# ${first}`
  return lines.join('\n')
}

function StreamingMarkdown({ children }) {
  const stable = stableStreamingMarkdown(children)
  return (
    // Same wrapper as the settled answer (see Markdown) so the streamed and
    // final renders share one layout box; only the content inside changes
    // when the run completes.
    <div aria-live="polite">{stable && <Markdown>{stable}</Markdown>}</div>
  )
}

/**
 * One exchange in the chat log: the user's message, then either the live
 * run status or the settled answer, then the actions that apply to it.
 */
/**
 * The offer to continue this thread in Workforce.
 *
 * Deliberately quiet: it sits under the answer as one line, not a modal or
 * a banner, because it is a suggestion about a run the user may not want.
 * The bar marks how long the offer stays up; it counts down to the offer
 * disappearing, never to running anything -- the run only ever starts from
 * the button.
 */
function SwitchToWorkforce({ onSwitch }) {
  const [dismissed, setDismissed] = useState(false)
  const [elapsed, setElapsed] = useState(0)

  useEffect(() => {
    const started = Date.now()
    const id = setInterval(() => {
      const seconds = (Date.now() - started) / 1000
      setElapsed(seconds)
      if (seconds >= 20) setDismissed(true)
    }, 100)
    return () => clearInterval(id)
  }, [])

  if (dismissed) return null
  const progress = Math.min(100, (elapsed / 20) * 100)

  return (
    <div className="mt-3 overflow-hidden rounded-lg border border-border/60 bg-card/60">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2.5">
        <Users className="h-3.5 w-3.5 shrink-0 text-link" aria-hidden="true" />
        <span className="min-w-0 flex-1 text-[13px] leading-5 text-muted-foreground">
          This needed the web, a file, or code — Single Agent has none of
          those.
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          <Button
            type="button"
            size="xs"
            variant="ghost"
            onClick={() => setDismissed(true)}
            className="text-muted-foreground"
          >
            Dismiss
          </Button>
          <Button
            type="button"
            size="xs"
            onClick={() => {
              setDismissed(true)
              onSwitch?.()
            }}
          >
            Retry with Workforce
          </Button>
        </div>
      </div>
      <div className="h-0.5 w-full bg-transparent" aria-hidden="true">
        <div
          className="h-full bg-link/40 transition-[width] duration-100 ease-linear"
          style={{ width: `${100 - progress}%` }}
        />
      </div>
    </div>
  )
}

export default function ChatExchange({ job, onSwitchToWorkforce, onRepeat }) {
  const [copyState, setCopyState] = useState('idle')
  const [exporting, setExporting] = useState(null)
  const [exportError, setExportError] = useState(null)

  useEffect(() => {
    setCopyState('idle')
    setExporting(null)
    setExportError(null)
  }, [job.job_id])

  useEffect(() => {
    if (copyState !== 'copied') return undefined
    const timer = window.setTimeout(() => setCopyState('idle'), 1600)
    return () => window.clearTimeout(timer)
  }, [copyState])

  const tasks = getJobTasks(job)
  const isActive = isLiveExchange(job)
  const finalAnswer = getFinalAnswer(job, tasks)
  const hasFinalAnswer = Boolean(finalAnswer)
  const isError = ERROR_JOB_STATUSES.has(job.status)

  const copyResult = async () => {
    if (!hasFinalAnswer) return
    try {
      await navigator.clipboard.writeText(finalAnswer)
      setCopyState('copied')
    } catch {
      setCopyState('error')
    }
  }

  const downloadResult = (format) => {
    if (!hasFinalAnswer) return
    const markdown = format === 'markdown'
    download(
      buildJobExport(job, tasks, format),
      safeFilename(job.topic, markdown ? 'md' : 'txt'),
      markdown ? 'text/markdown;charset=utf-8' : 'text/plain;charset=utf-8',
    )
  }

  const downloadDocument = async (format) => {
    if (!hasFinalAnswer || exporting) return
    setExporting(format)
    setExportError(null)
    try {
      const blob = await exportJobDocument(job.job_id, {
        format,
        title: job.topic,
        content: finalAnswer,
      })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = safeFilename(job.topic, format)
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 0)
    } catch (error) {
      setExportError(error?.message || 'Could not generate the file. Please try again.')
    } finally {
      setExporting(null)
    }
  }

  const attachments = Array.isArray(job.attachments) ? job.attachments : []
  const artifacts = Array.isArray(job.artifacts) ? job.artifacts : []

  return (
    <div className="flex flex-col gap-5">
      {/* The user's message. A tinted block rather than a rounded bubble:
          at this column width a long prompt in a bubble reads as a wall of
          pill, and the left rule ties it to the answer below it. */}
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-lg rounded-br-sm bg-secondary px-4 py-2.5 text-[15px] leading-relaxed text-foreground">
          {safeText(job.topic, 'Untitled message')}
          {attachments.length > 0 && (
            /* Each name links to the file itself. Showing the name alone
               is no help days later, when the question is not what the
               file was called but what was in it. */
            <p className="mt-1.5 flex flex-wrap items-center gap-x-1 gap-y-0.5 text-xs text-muted-foreground">
              <Paperclip className="h-3 w-3 shrink-0" aria-hidden="true" />
              {attachments.map((file, index) => (
                <Fragment key={file.file_id || index}>
                  {index > 0 && <span aria-hidden="true">·</span>}
                  {file.file_id ? (
                    <a
                      href={uploadUrl(file.file_id)}
                      target="_blank"
                      rel="noreferrer"
                      className="underline decoration-dotted underline-offset-2 hover:text-foreground"
                      title={`Open ${safeText(file.filename, 'file')}`}
                    >
                      {safeText(file.filename, 'file')}
                    </a>
                  ) : (
                    <span>{safeText(file.filename, 'file')}</span>
                  )}
                </Fragment>
              ))}
            </p>
          )}
        </div>
      </div>

      <div className="min-w-0">
        {isActive && (
          <div className={cn(job.streaming_text && 'mb-5')}>
            <LiveRun job={job} tasks={tasks} />
          </div>
        )}

        {isActive && job.streaming_text && (
          <StreamingMarkdown>{job.streaming_text}</StreamingMarkdown>
        )}

        {/* A failed run states what happened and where to look, in that
            order. userFacingFailure already strips stack-trace-shaped
            detail; the full text stays in the execution panel rather
            than being pasted into the conversation. */}
        {!isActive && isError && (
          <div
            role="alert"
            className="flex items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/[0.07] p-3.5"
          >
            <AlertCircle
              className="mt-px h-4 w-4 shrink-0 text-destructive"
              aria-hidden="true"
            />
            <div className="min-w-0 flex-1">
              <p className="text-[13px] font-medium text-foreground">
                {FAILURE_MESSAGES[job.status] ||
                  'The agent could not complete this task.'}
              </p>
              {job.status === 'cancelled' && job.cancellation_note ? (
                <p className="mt-1 text-[13px] leading-relaxed text-muted-foreground">
                  {safeText(job.cancellation_note, '')}
                </p>
              ) : null}
              {job.error && job.status !== 'timed_out' ? (
                <p className="mt-1 text-[13px] leading-relaxed text-muted-foreground">
                  {userFacingFailure(job.error, '')}
                </p>
              ) : null}
              {/* Partial work is still work: surface whatever the run did
                  produce instead of discarding it behind the error. */}
              {hasFinalAnswer && (
                <Collapsible className="mt-2.5">
                  <CollapsibleTrigger className="group inline-flex items-center gap-1.5 text-xs font-medium text-link hover:underline">
                    <ChevronDown
                      className="h-3.5 w-3.5 transition-transform duration-150 group-data-[state=open]:rotate-180"
                      aria-hidden="true"
                    />
                    Show what was produced before it stopped
                  </CollapsibleTrigger>
                  <CollapsibleContent className="overflow-hidden data-[state=closed]:animate-accordion-up data-[state=open]:animate-accordion-down">
                    <div className="mt-3">
                      <Markdown>{finalAnswer}</Markdown>
                    </div>
                  </CollapsibleContent>
                </Collapsible>
              )}
            </div>
          </div>
        )}

        {!isActive &&
          !isError &&
          (hasFinalAnswer ? (
            <Markdown>{finalAnswer}</Markdown>
          ) : (
            <p className="text-[13px] leading-relaxed text-muted-foreground" role="status">
              This message finished without producing an answer. The execution panel
              shows what each agent did.
            </p>
          ))}

        {/* Single Agent has no tools at all, so an ask that needed the
            web, a file, or code was answered from memory. The backend
            marks that (suggests_workforce); this is the only place the
            user can act on it, since the mode selector is hidden once a
            thread is locked. It arms the next message rather than resending
            -- a Workforce run costs minutes, and it is not ours to start. */}
        {!isActive && job.suggests_workforce && onSwitchToWorkforce && (
          <SwitchToWorkforce
            onSwitch={() => onSwitchToWorkforce(job.topic)}
          />
        )}

        {/* A Jira change this run proposed. Nothing was written: the card
            shows the exact change and writes it only on Approve. */}
        {!isActive &&
          Array.isArray(job.jira_actions) &&
          job.jira_actions.map((action) => (
            <JiraActionCard key={action.action_id} action={action} />
          ))}

        {/* Below the answer, above the footer: the answer explains the work,
            this is the work. Rendered for failed runs too -- a run that
            produced a real file before stopping still produced it. */}
        {!isActive && <ProducedFiles job={job} artifacts={artifacts} />}

        {exportError && (
          <p className="mt-2 text-xs text-destructive" role="alert">
            {exportError}
          </p>
        )}

        {/* One footer row per exchange. The like/dislike buttons that used
            to lead this row are gone -- nothing was ever recorded, so they
            were an affordance that only pretended to do something. */}
        {!isActive && (
          <RunFooter
            job={job}
            tasks={tasks}
            actions={
              <div className="flex items-center gap-0.5">
                {/* Turns a result the user was happy with into a recurring
                    task, prefilled with the request exactly as it ran. */}
                {onRepeat && hasFinalAnswer && !isError && (
                  <SimpleTooltip label="Repeat on a schedule">
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      onClick={() => onRepeat(job)}
                      aria-label="Repeat on a schedule"
                    >
                      <CalendarClock className="h-4 w-4" aria-hidden="true" />
                    </Button>
                  </SimpleTooltip>
                )}
                {copyState === 'error' && (
                  <span className="mr-1 text-xs text-destructive" role="alert">
                    Copy failed
                  </span>
                )}
                <SimpleTooltip label={copyState === 'copied' ? 'Copied' : 'Copy answer'}>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    onClick={copyResult}
                    disabled={!hasFinalAnswer}
                    aria-label="Copy answer"
                  >
                    {copyState === 'copied' ? (
                      <Check className="h-4 w-4 text-success" aria-hidden="true" />
                    ) : (
                      <Copy className="h-4 w-4" aria-hidden="true" />
                    )}
                  </Button>
                </SimpleTooltip>

                {hasFinalAnswer && (
                  <DropdownMenu>
                    <SimpleTooltip label="Export">
                      <DropdownMenuTrigger asChild>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          aria-label="Export this answer"
                          className="data-[state=open]:bg-accent data-[state=open]:text-foreground"
                        >
                          <Download className="h-4 w-4" aria-hidden="true" />
                        </Button>
                      </DropdownMenuTrigger>
                    </SimpleTooltip>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem onSelect={() => downloadResult('markdown')}>
                        Markdown
                      </DropdownMenuItem>
                      <DropdownMenuItem onSelect={() => downloadResult('text')}>
                        Plain text
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        disabled={Boolean(exporting)}
                        onSelect={() => downloadDocument('docx')}
                      >
                        {exporting === 'docx' ? 'Preparing…' : 'Word document'}
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        disabled={Boolean(exporting)}
                        onSelect={() => downloadDocument('pdf')}
                      >
                        {exporting === 'pdf' ? 'Preparing…' : 'PDF'}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                )}
              </div>
            }
          />
        )}
      </div>
    </div>
  )
}
