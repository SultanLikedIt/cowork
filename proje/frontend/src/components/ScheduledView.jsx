import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  ArrowUpRight,
  CalendarClock,
  CornerDownRight,
  Link2,
  Mail,
  PanelLeft,
  Paperclip,
  Pause,
  Pencil,
  Play,
  Plus,
  Radar,
  Trash2,
} from 'lucide-react'

import {
  createScheduledTask,
  deleteScheduledTask,
  getScheduledRuns,
  getScheduledTasks,
  markScheduledRunsSeen,
  pauseScheduledTask,
  resumeScheduledTask,
  runScheduledTaskNow,
} from '../api.js'
import { cn } from '../lib/utils.js'
import {
  CHAIN_TEMPLATE,
  CONDITIONS,
  RUN_TRIGGER_LABELS,
  TASK_TEMPLATES,
  buildTaskTree,
  deliveryRecipientsLabel,
  deliveryStatusMeta,
  describeSchedule,
  draftFromTask,
  formatDateTime,
  formatRelative,
  newTaskDraft,
  runStatusMeta,
} from '../schedule.js'
import ScheduleEditorDialog from './ScheduleEditorDialog.jsx'
import { Badge, StatusDot } from './ui/badge.jsx'
import { Button } from './ui/button.jsx'
import { ScrollArea } from './ui/scroll-area.jsx'
import { SimpleTooltip } from './ui/tooltip.jsx'

const REFRESH_MS = 15000

function TaskStateBadge({ task }) {
  if (!task.enabled) {
    return <Badge variant={task.paused_reason ? 'warning' : 'secondary'}>Paused</Badge>
  }
  if (task.running) {
    return (
      <Badge variant="running">
        <StatusDot pulse />
        Running
      </Badge>
    )
  }
  if (task.last_status && task.last_status !== 'running') {
    const meta = runStatusMeta(task.last_status)
    return <Badge variant={meta.variant}>{meta.label}</Badge>
  }
  return null
}

function nextRunLabel(task) {
  if (!task.enabled) return 'Paused'
  if (task.trigger_kind === 'after_task') return ''
  if (task.next_run_at) return `Next ${formatRelative(task.next_run_at)}`
  return task.trigger_kind === 'once' ? 'Finished' : 'No upcoming run'
}

function TaskRow({ task, depth = 0, selected, onSelect }) {
  return (
    <button
      type="button"
      onClick={() => onSelect(task.task_id)}
      aria-current={selected ? 'true' : undefined}
      // Followers sit under the task they run after, one step (16px) per link.
      style={{ paddingLeft: `${12 + depth * 16}px` }}
      className={cn(
        'flex w-full flex-col gap-1 rounded-md py-2.5 pr-3 text-left transition-colors duration-150',
        selected ? 'bg-accent text-foreground' : 'hover:bg-accent/50',
      )}
    >
      <span className="flex items-center gap-2">
        {depth > 0 && (
          <CornerDownRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-label="Runs after the task above" />
        )}
        {task.watch && <Radar className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-label="Watch task" />}
        <span className={cn('min-w-0 flex-1 truncate text-[13px] leading-5', selected && 'font-medium')}>
          {task.name}
        </span>
        {task.unseen_runs > 0 && (
          <span
            className="flex h-4 min-w-4 items-center justify-center rounded-full bg-link px-1 text-[10px] font-semibold tabular-nums text-background"
            aria-label={`${task.unseen_runs} new result${task.unseen_runs === 1 ? '' : 's'}`}
          >
            {task.unseen_runs}
          </span>
        )}
      </span>
      <span className="flex items-center gap-2 text-xs text-muted-foreground">
        <span className="min-w-0 flex-1 truncate">{describeSchedule(task)}</span>
        <span className="shrink-0 tabular-nums">{nextRunLabel(task)}</span>
      </span>
    </button>
  )
}

function deliveryTitle(run, timezone) {
  const who = deliveryRecipientsLabel(run)
  const when =
    run.delivery_status === 'pending' && run.delivery_next_at
      ? `Next attempt ${formatRelative(run.delivery_next_at)}`
      : run.delivered_at
        ? `Sent ${formatDateTime(run.delivered_at, timezone)}`
        : ''
  return [who, when].filter(Boolean).join(' · ') || undefined
}

function RunRow({ run, timezone, onOpen }) {
  const meta = runStatusMeta(run.status)
  const email = deliveryStatusMeta(run.delivery_status)
  const openable = Boolean(run.job_id)
  const when = run.started_at || run.scheduled_for
  return (
    <li className="flex flex-col gap-1.5 py-3">
      <div className="flex items-center gap-2">
        <Badge variant={meta.variant}>
          {run.status === 'running' && <StatusDot pulse />}
          {meta.label}
        </Badge>
        {run.change_detected === true && <Badge variant="default">Changed</Badge>}
        {email && (
          <Badge variant={email.variant} title={deliveryTitle(run, timezone)}>
            <Mail className="h-3 w-3" aria-hidden="true" />
            {email.label}
          </Badge>
        )}
        <span className="text-[13px] tabular-nums">{formatDateTime(when, timezone)}</span>
        <span className="text-xs text-muted-foreground">{RUN_TRIGGER_LABELS[run.trigger] || run.trigger}</span>
        {run.status === 'queued' && <StatusDot pulse className="text-link" />}
        {!run.seen_at && openable && ['done', 'partial', 'failed', 'error'].includes(run.status) && (
          <span className="h-1.5 w-1.5 rounded-full bg-link" aria-label="New" />
        )}
        {openable && (
          <Button
            type="button"
            variant="ghost"
            size="xs"
            onClick={() => onOpen(run)}
            className="ml-auto text-muted-foreground"
          >
            Open
            <ArrowUpRight className="h-3.5 w-3.5" aria-hidden="true" />
          </Button>
        )}
      </div>
      {run.change_summary && (
        <p className="whitespace-pre-line text-xs leading-relaxed text-muted-foreground">{run.change_summary}</p>
      )}
      {run.error && (
        <p
          className={cn(
            'text-xs leading-relaxed',
            ['skipped_condition', 'skipped_overlap', 'missed', 'no_change'].includes(run.status)
              ? 'text-muted-foreground'
              : 'text-destructive',
          )}
        >
          {run.error}
        </p>
      )}
      {run.delivery_error && run.delivery_status !== 'sent' && (
        <p
          className={cn(
            'text-xs leading-relaxed',
            run.delivery_status === 'failed' ? 'text-destructive' : 'text-muted-foreground',
          )}
        >
          Email: {run.delivery_error}
        </p>
      )}
      {run.input?.upstream_name && (
        <p className="text-xs text-muted-foreground">
          Input from {run.input.upstream_name}
          {run.input.file_ids?.length ? ` · ${run.input.file_ids.length} file${run.input.file_ids.length === 1 ? '' : 's'}` : ''}
          {run.input.context ? ' · report' : ''}
        </p>
      )}
      {run.input?.notes?.length > 0 && (
        <ul className="flex flex-col gap-0.5">
          {run.input.notes.map((note) => (
            <li key={note} className="text-xs leading-relaxed text-muted-foreground">
              {note}
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function EmptyState({ onCreate, onCreateChain, creatingChain }) {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-10 px-6 py-12 sm:px-8">
      <div className="flex flex-col gap-3">
        <span className="flex h-10 w-10 items-center justify-center rounded-[11px] border border-border/60 bg-muted/60">
          <CalendarClock className="h-5 w-5 text-foreground/80" aria-hidden="true" />
        </span>
        <h2 className="text-[28px] font-semibold leading-tight tracking-[-0.02em]">
          Let the agent do recurring work
        </h2>
        <p className="max-w-xl text-[15px] leading-relaxed text-muted-foreground">
          Save a request once and it runs on a schedule — a weekly brief, a morning
          snapshot, or a watch that only speaks up when something changes. Every result
          lands here and as a chat in this workspace.
        </p>
        <div>
          <Button type="button" onClick={() => onCreate(newTaskDraft())} className="mt-2">
            <Plus className="h-4 w-4" aria-hidden="true" />
            New scheduled task
          </Button>
        </div>
      </div>

      <div className="flex flex-col gap-3">
        <h3 className="eyebrow">Start from an example</h3>
        <div className="grid gap-3 sm:grid-cols-3">
          {TASK_TEMPLATES.map((template) => (
            <button
              key={template.name}
              type="button"
              onClick={() => onCreate(newTaskDraft(template))}
              className="group flex flex-col gap-2 rounded-lg border border-border/60 bg-card/60 p-4 text-left transition-colors duration-150 hover:border-border hover:bg-card"
            >
              <span className="flex items-center gap-2 text-[13px] font-medium">
                {template.watch ? (
                  <Radar className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                ) : (
                  <CalendarClock className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                )}
                {template.name}
              </span>
              <span className="text-xs text-muted-foreground">
                {describeSchedule({ trigger_kind: 'cron', cron_expr: scheduleCron(template) })}
              </span>
              <span className="line-clamp-3 text-xs leading-relaxed text-muted-foreground/80">
                {template.prompt}
              </span>
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-col gap-3">
        <h3 className="eyebrow">Or chain two tasks</h3>
        <button
          type="button"
          onClick={onCreateChain}
          disabled={creatingChain}
          className="flex flex-col gap-3 rounded-lg border border-border/60 bg-card/60 p-4 text-left transition-colors duration-150 hover:border-border hover:bg-card disabled:opacity-60 sm:flex-row sm:items-center sm:gap-6"
        >
          <span className="flex flex-col gap-1 sm:w-64 sm:shrink-0">
            <span className="flex items-center gap-2 text-[13px] font-medium">
              <Link2 className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
              {creatingChain ? 'Creating…' : CHAIN_TEMPLATE.name}
            </span>
            <span className="text-xs leading-relaxed text-muted-foreground">{CHAIN_TEMPLATE.description}</span>
          </span>
          <span className="flex min-w-0 flex-1 flex-col gap-1.5 text-xs text-muted-foreground">
            <span className="flex items-center gap-2">
              <Radar className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span className="truncate">{CHAIN_TEMPLATE.upstream.name} · every day at 10:00</span>
            </span>
            <span className="flex items-center gap-2 pl-4">
              <CornerDownRight className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span className="truncate">{CHAIN_TEMPLATE.follower.name} · only when it changed</span>
            </span>
          </span>
        </button>
      </div>
    </div>
  )
}

// Templates carry editor state; describeSchedule reads a saved task.
function scheduleCron({ schedule }) {
  const [hour, minute] = schedule.time.split(':').map(Number)
  if (schedule.kind === 'weekdays') return `${minute} ${hour} * * 1-5`
  if (schedule.kind === 'weekly') return `${minute} ${hour} * * ${schedule.weekday}`
  return `${minute} ${hour} * * *`
}

function TaskDetail({
  task,
  tasks,
  runs,
  runsState,
  busy,
  onRunNow,
  onTogglePause,
  onEdit,
  onDelete,
  onOpenRun,
  onSelectTask,
}) {
  const followers = tasks.filter((item) => item.upstream_task_id === task.task_id)
  const upstream = tasks.find((item) => item.task_id === task.upstream_task_id) || null
  const condition = CONDITIONS.find((item) => item.value === task.condition)
  const [confirmDelete, setConfirmDelete] = useState(false)
  useEffect(() => setConfirmDelete(false), [task.task_id])

  return (
    <div className="mx-auto flex w-full max-w-[46rem] flex-col gap-8 px-6 py-8 sm:px-8">
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-start gap-3">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <h2 className="text-xl font-semibold tracking-[-0.01em]">{task.name}</h2>
            <p className="text-[13px] text-muted-foreground">
              {describeSchedule(task)}
              {task.enabled && task.next_run_at && (
                <> · next {formatDateTime(task.next_run_at, task.timezone)}</>
              )}
            </p>
          </div>
          <div className="flex items-center gap-1">
            <Button type="button" size="sm" onClick={onRunNow} disabled={busy || task.running}>
              <Play className="h-3.5 w-3.5" aria-hidden="true" />
              Run now
            </Button>
            <SimpleTooltip label={task.enabled ? 'Pause' : 'Resume'}>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                onClick={onTogglePause}
                disabled={busy}
                aria-label={task.enabled ? 'Pause task' : 'Resume task'}
              >
                {task.enabled ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
              </Button>
            </SimpleTooltip>
            <SimpleTooltip label="Edit">
              <Button type="button" variant="ghost" size="icon-sm" onClick={onEdit} aria-label="Edit task">
                <Pencil className="h-4 w-4" />
              </Button>
            </SimpleTooltip>
            <SimpleTooltip label="Delete">
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                onClick={() => setConfirmDelete(true)}
                aria-label="Delete task"
                className="hover:text-destructive"
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </SimpleTooltip>
          </div>
        </div>

        {confirmDelete && (
          <div className="flex flex-wrap items-center gap-3 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2">
            <span className="flex-1 text-[13px] text-destructive">
              Delete this task and its run history? Chats it created stay.
            </span>
            <Button type="button" size="xs" variant="destructive" onClick={onDelete} disabled={busy}>
              Delete
            </Button>
            <Button type="button" size="xs" variant="ghost" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
          </div>
        )}

        {task.paused_reason && (
          <div className="flex items-start gap-2.5 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-[13px] leading-relaxed text-warning">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            {task.paused_reason}
          </div>
        )}

        {(task.trigger_kind === 'after_task' || followers.length > 0) && (
          <dl className="grid grid-cols-[auto_1fr] items-baseline gap-x-4 gap-y-2 text-[13px]">
            {task.trigger_kind === 'after_task' && (
              <>
                <dt className="text-muted-foreground">Runs after</dt>
                <dd className="flex flex-wrap items-baseline gap-x-2">
                  {upstream ? (
                    <button
                      type="button"
                      onClick={() => onSelectTask(upstream.task_id)}
                      className="font-medium text-link underline-offset-4 hover:underline"
                    >
                      {upstream.name}
                    </button>
                  ) : (
                    <span className="text-muted-foreground">a deleted task</span>
                  )}
                  {condition && <span className="text-muted-foreground">{condition.label.toLowerCase()}</span>}
                  <span className="text-xs text-muted-foreground">
                    {[
                      task.pass_upstream_result && 'gets its report',
                      task.pass_upstream_files && 'gets its files',
                    ]
                      .filter(Boolean)
                      .join(' · ') || 'gets no input from it'}
                  </span>
                </dd>
              </>
            )}
            {followers.length > 0 && (
              <>
                <dt className="text-muted-foreground">Starts</dt>
                <dd className="flex flex-wrap gap-x-3 gap-y-1">
                  {followers.map((follower) => (
                    <button
                      key={follower.task_id}
                      type="button"
                      onClick={() => onSelectTask(follower.task_id)}
                      className="font-medium text-link underline-offset-4 hover:underline"
                    >
                      {follower.name}
                    </button>
                  ))}
                </dd>
              </>
            )}
          </dl>
        )}

        <p className="whitespace-pre-wrap rounded-md bg-muted/40 px-4 py-3 text-[13px] leading-relaxed">
          {task.prompt}
        </p>

        <div className="flex flex-wrap gap-2">
          <Badge variant="outline">{task.mode === 'multi' ? 'Workforce' : 'Single Agent'}</Badge>
          {task.deep_research && <Badge variant="outline">Deep research</Badge>}
          {task.skill_ids.length > 0 && (
            <Badge variant="outline">
              {task.skill_ids.length} skill{task.skill_ids.length === 1 ? '' : 's'}
            </Badge>
          )}
          {task.watch && (
            <Badge variant="outline">
              <Radar className="h-3 w-3" aria-hidden="true" />
              Flags changes only
            </Badge>
          )}
          {task.include_previous_summary && <Badge variant="outline">Focuses on what is new</Badge>}
          {task.delivery?.to?.length > 0 && (
            <Badge variant="outline" title={[...task.delivery.to, ...(task.delivery.cc || [])].join(', ')}>
              <Mail className="h-3 w-3" aria-hidden="true" />
              Emails {task.delivery.to.length === 1 ? task.delivery.to[0] : `${task.delivery.to.length} people`}
            </Badge>
          )}
          {task.attached_files?.map((file) => (
            <Badge key={file.file_id} variant="outline" title="Attached to every run">
              <Paperclip className="h-3 w-3" aria-hidden="true" />
              {file.filename}
            </Badge>
          ))}
        </div>
      </div>

      <div className="flex flex-col">
        <div className="flex items-center gap-2 pb-1">
          <h3 className="eyebrow">Runs</h3>
          {runs.length > 0 && <Badge variant="count">{runs.length}</Badge>}
        </div>
        {runsState === 'loading' && runs.length === 0 ? (
          <p className="py-3 text-[13px] text-muted-foreground">Loading runs…</p>
        ) : runs.length === 0 ? (
          <p className="py-3 text-[13px] leading-relaxed text-muted-foreground">
            No runs yet.{' '}
            {task.trigger_kind === 'after_task'
              ? `It starts after ${upstream ? upstream.name : 'its upstream task'} finishes, or use Run now to try it with that task's latest result.`
              : task.enabled && task.next_run_at
                ? `The first one starts ${formatRelative(task.next_run_at)}, or use Run now to try it.`
                : 'Use Run now to try it.'}
          </p>
        ) : (
          <ul className="divide-y divide-border/60">
            {runs.map((run) => (
              <RunRow key={run.run_id} run={run} timezone={task.timezone} onOpen={onOpenRun} />
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

export default function ScheduledView({
  workspaceId,
  workspaceName,
  skillCatalog,
  maxActiveSkills,
  editorSeed = null,
  onEditorSeedConsumed,
  onOpenRun,
  onUnseenChange,
  onToggleNav,
  navOpen = false,
}) {
  const [tasks, setTasks] = useState([])
  const [state, setState] = useState('loading')
  const [loadError, setLoadError] = useState(null)
  const [schedulingEnabled, setSchedulingEnabled] = useState(true)
  const [selectedId, setSelectedId] = useState(null)
  const [runs, setRuns] = useState([])
  const [runsState, setRunsState] = useState('idle')
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState(null)
  const [editor, setEditor] = useState({ open: false, task: null, draft: null })
  const [creatingChain, setCreatingChain] = useState(false)
  const tree = useMemo(() => buildTaskTree(tasks), [tasks])

  const loadTasks = useCallback(
    async (signal) => {
      try {
        const data = await getScheduledTasks(workspaceId, { signal })
        setTasks(data.tasks || [])
        setSchedulingEnabled(data.scheduling_enabled !== false)
        onUnseenChange?.(data.unseen_runs || 0)
        setState('ready')
        setLoadError(null)
      } catch (error) {
        if (error?.name === 'AbortError') return
        setState('error')
        setLoadError(error.message || 'Unable to load scheduled tasks.')
      }
    },
    [workspaceId, onUnseenChange],
  )

  const loadRuns = useCallback(async (taskId, signal) => {
    if (!taskId) return
    setRunsState((previous) => (previous === 'ready' ? 'ready' : 'loading'))
    try {
      const data = await getScheduledRuns(taskId, { signal })
      setRuns(data.runs || [])
      setRunsState('ready')
    } catch (error) {
      if (error?.name !== 'AbortError') setRunsState('error')
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    setState('loading')
    setSelectedId(null)
    loadTasks(controller.signal)
    const timer = window.setInterval(() => loadTasks(controller.signal), REFRESH_MS)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [loadTasks])

  // Keep a selection whenever there are tasks: the detail pane is the point
  // of this screen, not an optional drill-down.
  useEffect(() => {
    if (tasks.length === 0) return
    if (!selectedId || !tasks.some((task) => task.task_id === selectedId)) {
      setSelectedId(tree[0].task.task_id)
    }
  }, [tasks, tree, selectedId])

  const selected = useMemo(
    () => tasks.find((task) => task.task_id === selectedId) || null,
    [tasks, selectedId],
  )

  useEffect(() => {
    if (!selectedId) return undefined
    const controller = new AbortController()
    setRuns([])
    setRunsState('loading')
    loadRuns(selectedId, controller.signal)
    const timer = window.setInterval(() => loadRuns(selectedId, controller.signal), REFRESH_MS)
    return () => {
      controller.abort()
      window.clearInterval(timer)
    }
  }, [selectedId, loadRuns])

  // Viewing a task's results is what "seen" means.
  const unseenForSelected = selected?.unseen_runs || 0
  useEffect(() => {
    if (!selectedId || unseenForSelected === 0) return
    markScheduledRunsSeen(selectedId)
      .then(() => Promise.all([loadTasks(), loadRuns(selectedId)]))
      .catch(() => {})
  }, [selectedId, unseenForSelected, loadTasks, loadRuns])

  useEffect(() => {
    if (!editorSeed) return
    setEditor({ open: true, task: null, draft: editorSeed })
    onEditorSeedConsumed?.()
  }, [editorSeed, onEditorSeedConsumed])

  const act = async (action) => {
    setBusy(true)
    setActionError(null)
    try {
      await action()
      await loadTasks()
      if (selectedId) await loadRuns(selectedId)
    } catch (error) {
      setActionError(error.message || 'That action failed. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  const openCreate = (draft) => setEditor({ open: true, task: null, draft })

  const createChainTemplate = async () => {
    if (creatingChain) return
    setCreatingChain(true)
    setActionError(null)
    try {
      const upstream = await createScheduledTask({ ...CHAIN_TEMPLATE.upstream, workspace_id: workspaceId })
      const follower = await createScheduledTask({
        ...CHAIN_TEMPLATE.follower,
        workspace_id: workspaceId,
        upstream_task_id: upstream.task_id,
      })
      await loadTasks()
      setSelectedId(follower.task_id)
    } catch (error) {
      setActionError(error.message || 'Unable to create the example chain.')
    } finally {
      setCreatingChain(false)
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex h-14 shrink-0 items-center gap-3 border-b border-border/70 px-4">
        {onToggleNav && (
          <SimpleTooltip label={navOpen ? 'Hide chat list' : 'Show chat list'}>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              onClick={onToggleNav}
              aria-label={navOpen ? 'Hide chat list' : 'Show chat list'}
              aria-pressed={navOpen}
              className={cn('text-muted-foreground hover:text-foreground', navOpen && 'bg-accent text-foreground')}
            >
              <PanelLeft className="h-4 w-4" aria-hidden="true" />
            </Button>
          </SimpleTooltip>
        )}
        <div className="flex min-w-0 flex-1 items-baseline gap-2">
          <h1 className="text-[15px] font-semibold tracking-tight">Scheduled</h1>
          <span className="truncate text-[13px] text-muted-foreground">{workspaceName}</span>
        </div>
        {tasks.length > 0 && (
          <Button type="button" size="sm" onClick={() => openCreate(newTaskDraft())}>
            <Plus className="h-4 w-4" aria-hidden="true" />
            New task
          </Button>
        )}
      </header>

      {!schedulingEnabled && (
        <div className="mx-6 mt-4 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-[13px] text-warning" role="status">
          Scheduling is turned off on this server (SCHEDULING_ENABLED), so tasks will not run.
        </div>
      )}
      {(loadError || actionError) && (
        <div
          className="mx-6 mt-4 flex items-start gap-2.5 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-[13px] text-destructive"
          role="alert"
        >
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          {actionError || loadError}
        </div>
      )}

      {state === 'loading' && tasks.length === 0 ? (
        <p className="px-8 py-10 text-[13px] text-muted-foreground">Loading scheduled tasks…</p>
      ) : tasks.length === 0 ? (
        <ScrollArea className="surface-grid min-h-0 flex-1">
          <EmptyState onCreate={openCreate} onCreateChain={createChainTemplate} creatingChain={creatingChain} />
        </ScrollArea>
      ) : (
        <div className="flex min-h-0 flex-1 max-lg:flex-col">
          <div className="flex min-h-0 shrink-0 flex-col border-border/70 max-lg:max-h-[40%] max-lg:border-b lg:w-[22rem] lg:border-r">
            <ScrollArea className="min-h-0 flex-1">
              <div className="flex flex-col gap-px p-3">
                {tree.map(({ task, depth }) => (
                  <TaskRow
                    key={task.task_id}
                    task={task}
                    depth={depth}
                    selected={task.task_id === selectedId}
                    onSelect={setSelectedId}
                  />
                ))}
              </div>
            </ScrollArea>
          </div>
          <ScrollArea className="min-h-0 flex-1">
            {selected && (
              <TaskDetail
                task={selected}
                tasks={tasks}
                onSelectTask={setSelectedId}
                runs={runs}
                runsState={runsState}
                busy={busy}
                onRunNow={() => act(() => runScheduledTaskNow(selected.task_id))}
                onTogglePause={() =>
                  act(() =>
                    selected.enabled
                      ? pauseScheduledTask(selected.task_id)
                      : resumeScheduledTask(selected.task_id),
                  )
                }
                onEdit={() => setEditor({ open: true, task: selected, draft: draftFromTask(selected) })}
                onDelete={() =>
                  act(async () => {
                    await deleteScheduledTask(selected.task_id)
                    setSelectedId(null)
                  })
                }
                onOpenRun={onOpenRun}
              />
            )}
          </ScrollArea>
        </div>
      )}

      <ScheduleEditorDialog
        open={editor.open}
        task={editor.task}
        initialDraft={editor.draft}
        workspaceId={workspaceId}
        skillCatalog={skillCatalog}
        maxActiveSkills={maxActiveSkills}
        tasks={tasks}
        onClose={() => setEditor({ open: false, task: null, draft: null })}
        onSaved={(saved) => {
          setEditor({ open: false, task: null, draft: null })
          setSelectedId(saved.task_id)
          loadTasks()
        }}
      />
    </div>
  )
}
