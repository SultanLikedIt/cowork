import { useCallback, useMemo, useState } from 'react'
import {
  AlertCircle,
  Check,
  ChevronDown,
  FileText,
  Loader2,
  Plus,
  Wrench,
  X,
} from 'lucide-react'

import { AgentIcon } from '../agentIcons.jsx'
import {
  executionRoleOf,
  executionStrategyOf,
  getJobTasks,
  getTaskRuntime,
  isActiveJob,
  safeText,
  taskDisplayLabel,
} from '../stages.js'
import { cn } from '../lib/utils.js'
import { UsageSummary } from './JobDetails.jsx'
import { TimingLog, formatMs } from './TimingLog.jsx'
import { Badge, MarkSlot, StatusDot } from './ui/badge.jsx'
import { Button } from './ui/button.jsx'
import { Progress } from './ui/progress.jsx'
import { ScrollArea } from './ui/scroll-area.jsx'
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs.jsx'

const COMPLETE_TASK_STATES = new Set(['complete', 'completed', 'done', 'success'])
const FAILED_TASK_STATES = new Set(['error', 'failed'])
const RUNNING_TASK_STATES = new Set(['running', 'in_progress'])
const CANCELLED_TASK_STATES = new Set(['cancelled', 'canceled'])
const WAITING_TASK_STATES = new Set(['open', 'pending', 'queued', 'waiting'])

function progressState(runtime) {
  const status = runtime.status
  if (CANCELLED_TASK_STATES.has(status)) return { key: 'cancelled', label: 'Cancelled' }
  if (RUNNING_TASK_STATES.has(status)) return { key: 'running', label: 'Running' }
  if (COMPLETE_TASK_STATES.has(status)) return { key: 'done', label: 'Done' }
  if (FAILED_TASK_STATES.has(status)) return { key: 'failed', label: 'Failed' }
  return { key: 'pending', label: 'Pending' }
}

function agentRuntime(taskEntries) {
  if (taskEntries.length === 0) return null
  const statuses = taskEntries.map(({ runtime }) => runtime.status)
  if (statuses.some((status) => RUNNING_TASK_STATES.has(status))) {
    return { key: 'working', label: 'Working' }
  }
  if (statuses.some((status) => FAILED_TASK_STATES.has(status))) {
    return { key: 'failed', label: 'Failed' }
  }
  if (statuses.every((status) => COMPLETE_TASK_STATES.has(status))) {
    return { key: 'done', label: 'Done' }
  }
  if (statuses.some((status) => WAITING_TASK_STATES.has(status))) {
    return { key: 'waiting', label: 'Waiting' }
  }
  return null
}

function formatBytes(value) {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes < 0) return null
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function PanelEmpty({ children }) {
  return (
    <p className="text-[13px] leading-relaxed text-muted-foreground">{children}</p>
  )
}

function Section({ label, count, action, children }) {
  return (
    <section className="min-w-0">
      <div className="mb-2.5 flex items-center gap-2">
        <h3 className="eyebrow min-w-0 truncate">{label}</h3>
        {typeof count === 'number' && count > 0 && (
          <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground/70">
            {count}
          </span>
        )}
        {action && <div className="ml-auto shrink-0">{action}</div>}
      </div>
      {children}
    </section>
  )
}

const TASK_STATE_STYLES = {
  running: 'border-link/20 bg-link/[0.04]',
  done: 'border-border/40 bg-transparent',
  failed: 'border-destructive/20 bg-destructive/[0.04]',
  pending: 'border-border/30 bg-transparent',
  cancelled: 'border-border/30 bg-transparent',
}

function TaskMark({ state }) {
  return (
    <MarkSlot>
      {state === 'done' && <Check className="h-3.5 w-3.5 text-success" />}
      {state === 'running' && (
        <Loader2 className="h-3.5 w-3.5 animate-spin text-link" />
      )}
      {state === 'failed' && (
        <AlertCircle className="h-3.5 w-3.5 text-destructive" />
      )}
      {(state === 'pending' || state === 'cancelled') && (
        <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/35" />
      )}
    </MarkSlot>
  )
}

function TaskCard({ task, runtime }) {
  const [expanded, setExpanded] = useState(false)
  const state = progressState(runtime)
  const label = taskDisplayLabel(task)
  const fullTitle = safeText(task.title, 'Task')
  const description = safeText(task.description, '')
  const hasDescription = description && description !== fullTitle

  return (
    <li
      className={cn(
        'rounded-lg border px-3 py-2.5 transition-colors duration-150',
        TASK_STATE_STYLES[state.key] || TASK_STATE_STYLES.pending,
      )}
    >
      <div className="flex items-start gap-2">
        <TaskMark state={state.key} />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span
            className={cn(
              'text-[13px] leading-5 text-foreground',
              state.key === 'done' && 'text-muted-foreground',
              state.key === 'pending' && 'text-muted-foreground/70',
              !expanded && 'line-clamp-2',
            )}
          >
            {label}
          </span>
          {runtime.agentAssigned && (
            <span className="text-[11px] text-muted-foreground/70">
              {runtime.agent}
            </span>
          )}
          {hasDescription && expanded && (
            <span className="mt-1 text-xs leading-snug text-muted-foreground/60">
              {description}
            </span>
          )}
        </span>
        {hasDescription && (
          <button
            type="button"
            onClick={() => setExpanded(!expanded)}
            className="mt-0.5 shrink-0 rounded p-0.5 text-muted-foreground/40 transition-colors duration-150 hover:text-muted-foreground"
            aria-label={expanded ? 'Collapse' : 'Expand'}
          >
            <ChevronDown
              className={cn(
                'h-3.5 w-3.5 transition-transform duration-200',
                expanded && 'rotate-180',
              )}
            />
          </button>
        )}
      </div>
    </li>
  )
}

const AGENT_STATE_BORDER = {
  working: 'border-link/25',
  failed: 'border-destructive/25',
  done: 'border-success/15',
  waiting: 'border-border/40',
}

// The backend's `description` for a fixed worker is its system prompt --
// written for the model, hundreds of words long, and truncated here into an
// unreadable fragment ("A research specialist that uses web_search/web_read
// to find and open current, verifia…"). These say the same thing in a line.
// Dynamic agents keep the backend text: it is the only description they have.
const FIXED_AGENT_BLURB = {
  'Research Agent': 'Searches the web and reads sources',
  'Document Agent': 'Writes Word, PDF, PowerPoint and spreadsheets',
  'Coding Agent': 'Writes and runs code',
  'Mail Agent': 'Reads your Outlook inbox and sent mail',
  'Social Agent': 'Reads public Reddit posts and comments',
}

function AgentCard({ worker, assignments, runtime }) {
  const identity =
    FIXED_AGENT_BLURB[worker.name] ||
    worker.description ||
    (worker.source === 'dynamic' ? 'Created during this run' : '')

  return (
    <li
      title={identity ? `${worker.name} — ${identity}` : worker.name}
      className={cn(
        'rounded-lg border bg-card/50 px-3 py-2.5 transition-colors duration-150',
        runtime ? AGENT_STATE_BORDER[runtime.key] || 'border-border/40' : 'border-border/40',
      )}
    >
      <div className="flex items-center gap-2.5">
        <span
          className={cn(
            'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg',
            runtime?.key === 'working'
              ? 'bg-link/10 text-link'
              : runtime?.key === 'failed'
                ? 'bg-destructive/10 text-destructive'
                : 'bg-muted/60 text-muted-foreground/70',
          )}
        >
          <AgentIcon name={worker.name} width={16} height={16} />
        </span>
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="flex items-center gap-2">
            <span className="min-w-0 truncate text-[13px] font-medium leading-5">
              {worker.name}
            </span>
            {runtime?.key === 'working' && (
              <Badge variant="running" className="shrink-0">
                <StatusDot pulse />
                Working
              </Badge>
            )}
            {runtime?.key === 'failed' && (
              <Badge variant="destructive" className="shrink-0">
                Failed
              </Badge>
            )}
          </span>
          {identity && (
            <span className="truncate text-[11px] text-muted-foreground/60">
              {identity}
            </span>
          )}
        </span>
        {assignments.length > 0 && (
          <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground/50">
            {assignments.length} {assignments.length === 1 ? 'task' : 'tasks'}
          </span>
        )}
      </div>
    </li>
  )
}

function FilesSection({ job }) {
  const attachments = Array.isArray(job?.attachments) ? job.attachments : []
  const workspaceFiles = Array.isArray(job?.workspace?.files) ? job.workspace.files : []
  const totalSize = job?.workspace?.total_size_bytes
  const total = attachments.length + workspaceFiles.length

  return (
    <Section label="Sources" count={total}>
      {total === 0 ? (
        <PanelEmpty>
          {job ? 'No files were attached to this run.' : 'Files attached to a run appear here.'}
        </PanelEmpty>
      ) : (
        <ul className="flex flex-col gap-px">
          {attachments.map((attachment, index) => (
            <li
              key={attachment.file_id || `${attachment.filename}-${index}`}
              className="flex items-center gap-2 rounded-md px-1.5 py-1.5 text-[13px]"
            >
              <FileText
                className="h-3.5 w-3.5 shrink-0 text-muted-foreground/70"
                aria-hidden="true"
              />
              <span className="min-w-0 flex-1 truncate">
                {safeText(attachment.filename, 'Unknown source')}
              </span>
              {attachment.kind === 'zip' && (
                <Badge variant="count" className="shrink-0">
                  zip
                </Badge>
              )}
            </li>
          ))}
          {workspaceFiles.map((file, index) => (
            <li
              key={`${file.path}-${index}`}
              className="flex items-center gap-2 rounded-md px-1.5 py-1.5 text-[13px] text-muted-foreground"
            >
              <FileText className="h-3.5 w-3.5 shrink-0 opacity-70" aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate">
                {safeText(file.path, 'Workspace file')}
              </span>
            </li>
          ))}
        </ul>
      )}
      {formatBytes(totalSize) && (
        <p className="mt-2 px-1.5 text-xs text-muted-foreground/80">
          {formatBytes(totalSize)} extracted
        </p>
      )}
    </Section>
  )
}

// A long research run records dozens of calls, and everything below the
// panel — tokens, timings — ended up several screens down. Newest first,
// the rest behind one click.
const VISIBLE_TOOL_CALLS = 8

function ToolsSection({ job }) {
  const calls = Array.isArray(job?.tool_calls) ? job.tool_calls : []
  const [expanded, setExpanded] = useState(false)

  // Newest first: during a live run the latest call is what you are watching.
  const ordered = useMemo(() => [...calls].reverse(), [calls])
  const hidden = Math.max(0, ordered.length - VISIBLE_TOOL_CALLS)
  const shown = expanded ? ordered : ordered.slice(0, VISIBLE_TOOL_CALLS)

  return (
    <Section label="Tool activity" count={calls.length}>
      {calls.length === 0 ? (
        <PanelEmpty>No tool calls were recorded for this run.</PanelEmpty>
      ) : (
        <ul className="flex flex-col gap-1.5">
          {shown.map((call, index) => {
            const args = call?.args && typeof call.args === 'object' ? call.args : null
            const query =
              args &&
              (args.query || args.q || args.search_query || args.keyword || args.path || args.url)
            return (
              <li
                key={`${call.timestamp || call.tool_name}-${index}`}
                className={cn(
                  'rounded-lg border bg-card/50 px-2.5 py-2',
                  call?.error ? 'border-destructive/20' : 'border-border/50',
                )}
              >
                <div className="flex items-start gap-2">
                  <Wrench
                    className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground/60"
                    aria-hidden="true"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline justify-between gap-2 font-mono text-[11px] text-foreground/80">
                      <span className="min-w-0 truncate">{safeText(call.tool_name, 'Tool')}</span>
                      {typeof call?.duration_ms === 'number' && (
                        <span className="shrink-0 tabular-nums text-muted-foreground/70">
                          {formatMs(call.duration_ms)}
                        </span>
                      )}
                    </div>
                    {query != null && (
                      <div className="mt-0.5 truncate text-[11px] text-muted-foreground/60">
                        {String(query)}
                      </div>
                    )}
                    {call?.error && (
                      <div className="mt-0.5 text-[11px] text-destructive/80">
                        {safeText(call.error, 'Tool failed')}
                      </div>
                    )}
                  </div>
                </div>
              </li>
            )
          })}
        </ul>
      )}
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
    </Section>
  )
}

export default function RightPanel({
  job,
  mode = 'single',
  workers = [],
  workersState = 'ready',
  onAddAgent,
  open = true,
  onClose,
}) {
  const effectiveMode = job?.mode || mode
  const isSingle = effectiveMode === 'single'
  const strategy = job ? executionStrategyOf(job) : null
  const tasks = strategy === 'direct' ? [] : getJobTasks(job)

  const taskEntries = tasks.map((task) => ({
    task,
    runtime: getTaskRuntime(job, task),
  }))
  const agentTasks = new Map()
  taskEntries.forEach((entry) => {
    if (!entry.runtime.agentAssigned) return
    if (!agentTasks.has(entry.runtime.agent)) agentTasks.set(entry.runtime.agent, [])
    agentTasks.get(entry.runtime.agent).push(entry)
  })
  const assignedAgentNames = new Set(agentTasks.keys())
  const knownNames = new Set(workers.map((worker) => worker.name))
  const rosterLoaded = workersState === 'ready'
  const canCreateDynamicWorkers = strategy === 'workforce'
  const routedSpecialist = job ? executionRoleOf(job) : ''
  const multiPool = [
    ...workers,
    ...[...assignedAgentNames]
      .filter((name) => !knownNames.has(name))
      .map((name) => ({
        name,
        source:
          rosterLoaded && canCreateDynamicWorkers && name !== routedSpecialist
            ? 'dynamic'
            : 'unknown',
      })),
  ]
  const pool = isSingle ? [{ name: 'Assistant', source: 'fixed' }] : multiPool

  const doneCount = taskEntries.filter(
    ({ runtime }) => progressState(runtime).key === 'done',
  ).length
  const percent = tasks.length > 0 ? Math.round((doneCount / tasks.length) * 100) : 0
  const running = isActiveJob(job)

  return (
    <aside
      aria-label="Execution panel"
      className={cn(
        'flex h-full shrink-0 flex-col border-l border-border/70 bg-panel',
        'transition-[width] duration-200',
        open ? 'w-[360px]' : 'invisible w-0 overflow-hidden border-l-0',
        'max-lg:fixed max-lg:inset-y-0 max-lg:right-0 max-lg:z-40 max-lg:w-[360px] max-lg:border-l max-lg:shadow-2xl max-lg:transition-transform',
        open ? 'max-lg:translate-x-0' : 'max-lg:translate-x-full',
      )}
    >
      <div className="flex h-14 shrink-0 items-center gap-2 border-b border-border/70 pl-4 pr-2">
        <h2 className="min-w-0 flex-1 truncate text-[13px] font-semibold tracking-tight">
          Execution
        </h2>
        {onClose && (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            onClick={onClose}
            aria-label="Close execution panel"
          >
            <X className="h-4 w-4" />
          </Button>
        )}
      </div>

      <Tabs defaultValue="activity" className="flex min-h-0 flex-1 flex-col">
        <div className="px-3 pt-3">
          <TabsList className="w-full">
            <TabsTrigger value="activity">Activity</TabsTrigger>
            <TabsTrigger value="details">Details</TabsTrigger>
          </TabsList>
        </div>

        <ScrollArea className="min-h-0 flex-1">
          <div className="p-4">
            <TabsContent value="activity" className="flex flex-col gap-6">
              <Section label="Tasks" count={tasks.length}>
                {tasks.length > 0 ? (
                  <>
                    {running && (
                      <div className="mb-3">
                        <div className="mb-1.5 flex items-baseline justify-between gap-3">
                          <span className="text-[13px] text-muted-foreground">
                            {doneCount} of {tasks.length} complete
                          </span>
                          <span className="text-[13px] font-medium tabular-nums">
                            {percent}%
                          </span>
                        </div>
                        <Progress value={percent} className="h-1" />
                      </div>
                    )}
                    <ol className="flex flex-col gap-1.5">
                      {taskEntries.map(({ task, runtime }) => (
                        <TaskCard key={task.renderKey} task={task} runtime={runtime} />
                      ))}
                    </ol>
                  </>
                ) : (
                  <PanelEmpty>
                    {!job
                      ? 'A Workforce run breaks the request into tasks here.'
                      : strategy === 'workforce'
                        ? 'Planning the workflow…'
                        : strategy === 'specialist'
                          ? 'Routed straight to one specialist, with no planning step.'
                          : 'Answered directly, with no planning step.'}
                  </PanelEmpty>
                )}
              </Section>

              <Section
                label="Agents"
                count={pool.length}
                action={
                  !isSingle && onAddAgent ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="xs"
                      onClick={onAddAgent}
                      aria-label="Add a new agent"
                      className="-my-1 gap-1"
                    >
                      <Plus className="h-3 w-3" aria-hidden="true" />
                      Add
                    </Button>
                  ) : null
                }
              >
                {pool.length > 0 ? (
                  <ul className="flex flex-col gap-1.5">
                    {pool.map((worker) => {
                      const assignments = agentTasks.get(worker.name) || []
                      const runtime = agentRuntime(assignments)
                      return (
                        <AgentCard
                          key={worker.name}
                          worker={worker}
                          assignments={assignments}
                          runtime={runtime}
                        />
                      )
                    })}
                  </ul>
                ) : (
                  <PanelEmpty>No agents available yet.</PanelEmpty>
                )}
              </Section>
            </TabsContent>

            <TabsContent value="details" className="flex flex-col gap-6">
              {Array.isArray(job?.skills) && job.skills.length > 0 && (
                <Section label="Skills applied" count={job.skills.length}>
                  <ul className="flex flex-col gap-2">
                    {job.skills.map((skill) => (
                      <li key={skill.skill_id}>
                        <div className="text-[13px] font-medium leading-5">
                          {skill.name}
                        </div>
                        <div className="text-xs leading-snug text-muted-foreground">
                          {skill.description}
                        </div>
                      </li>
                    ))}
                  </ul>
                </Section>
              )}
              {/* Usage first: it is the run's summary — which model, how
                  long, what it cost — and answers "what happened here"
                  on its own. Sources and tool calls are the supporting
                  detail you scroll to once that raises a question. */}
              <Section label="Usage">
                <UsageSummary job={job} compact />
              </Section>
              <Section label="Timing">
                <TimingLog job={job} />
              </Section>
              <FilesSection job={job} />
              <ToolsSection job={job} />
            </TabsContent>
          </div>
        </ScrollArea>
      </Tabs>
    </aside>
  )
}
