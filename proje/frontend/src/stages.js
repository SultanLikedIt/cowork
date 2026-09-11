export const ACTIVE_JOB_STATUSES = new Set(['queued', 'running'])
export const SUCCESS_JOB_STATUSES = new Set(['done', 'completed', 'partial'])
export const ERROR_JOB_STATUSES = new Set([
  'error',
  'failed',
  'timed_out',
  'cancelled',
  // Set by the backend's startup orphan sweep for a job left queued/running
  // when a prior process died -- see db.mark_incomplete_jobs_interrupted.
  // Without this it falls through to the default "running" status icon,
  // which is actively misleading (it did not finish and never will).
  'interrupted',
])

const STATUS_CLASS_ALIASES = {
  open: 'waiting',
  pending: 'waiting',
  queued: 'waiting',
  running: 'working',
  in_progress: 'working',
  complete: 'done',
  completed: 'done',
  success: 'done',
  partial: 'done',
  error: 'failed',
  failed: 'failed',
  cancelled: 'failed',
  canceled: 'failed',
}

export function isActiveJob(job) {
  return Boolean(job && ACTIVE_JOB_STATUSES.has(job.status))
}

/** True while the exchange should show the live assistant placeholder. */
export function isLiveExchange(job) {
  if (!job) return false
  if (job._optimistic) return true
  return isActiveJob(job)
}

const RESEARCH_ACTIVITY_STAGES = new Set([
  'searching',
  'opening',
  'browsing',
  'reading',
])

export const USER_FACING_PROGRESS = {
  thinking: 'Thinking…',
  processing: 'Processing request',
  researching: 'Researching sources',
  analyzing: 'Analyzing information',
  preparing: 'Preparing response',
  creating: 'Creating document',
}

/**
 * Map backend job/task/activity signals to a single corporate progress label
 * for the conversation. Raw tool URLs, queries, and task titles stay out.
 */
export function userFacingProgress(job, tasks = []) {
  if (!job) {
    return { phase: 'processing', label: USER_FACING_PROGRESS.processing }
  }

  if (job._optimistic) {
    return { phase: 'thinking', label: USER_FACING_PROGRESS.thinking }
  }

  if (
    job.status === 'queued' &&
    !job.streaming_text &&
    !job.current_activity?.stage
  ) {
    const resolvedTasks = tasks.length > 0 ? tasks : getJobTasks(job)
    if (resolvedTasks.length === 0) {
      return { phase: 'thinking', label: USER_FACING_PROGRESS.thinking }
    }
  }

  if (typeof job.streaming_text === 'string' && job.streaming_text.length > 0) {
    return { phase: 'preparing', label: USER_FACING_PROGRESS.preparing }
  }

  const activityStage = job.current_activity?.stage
  if (activityStage === 'building') {
    return { phase: 'creating', label: USER_FACING_PROGRESS.creating }
  }
  if (RESEARCH_ACTIVITY_STAGES.has(activityStage)) {
    return { phase: 'researching', label: USER_FACING_PROGRESS.researching }
  }

  const strategy = executionStrategyOf(job)
  const resolvedTasks = tasks.length > 0 ? tasks : getJobTasks(job)

  if (strategy === 'workforce' && resolvedTasks.length > 0) {
    const hasWorking = resolvedTasks.some((task) => {
      const runtime = getTaskRuntime(job, task)
      return runtime.statusClass === 'working'
    })
    if (hasWorking) {
      return { phase: 'analyzing', label: USER_FACING_PROGRESS.analyzing }
    }
  }

  if (isActiveJob(job)) {
    return { phase: 'processing', label: USER_FACING_PROGRESS.processing }
  }

  return { phase: 'processing', label: USER_FACING_PROGRESS.processing }
}

/** Concise task title for sidebar rows; full title stays in title/tooltip. */
export function taskDisplayLabel(task, maxLength = 120) {
  const title = safeText(task?.title, 'Task')
  if (title.length <= maxLength) return title
  const cut = title.slice(0, maxLength)
  const lastSpace = cut.lastIndexOf(' ')
  const trimmed =
    lastSpace > maxLength * 0.6 ? cut.slice(0, lastSpace) : cut.trimEnd()
  return `${trimmed}…`
}

// New jobs mirror the explicit product mode exactly: Single or Workforce.
// Direct/Specialist remain readable so historical persisted jobs still render.
export const EXECUTION_STRATEGIES = new Set(['single', 'workforce', 'direct', 'specialist'])

export function executionStrategyOf(job) {
  const strategy = job?.usage?.execution_strategy
  if (EXECUTION_STRATEGIES.has(strategy)) return strategy
  // No strategy recorded: either a job that predates routing, or the
  // optimistic stub the composer creates before the first snapshot lands.
  // Mode is the honest fallback for optimistic and legacy records.
  return job?.mode === 'multi' ? 'workforce' : 'single'
}

// The fixed worker a SPECIALIST job was routed to ("Research Agent" /
// "Document Agent"). Empty for every other strategy.
export function executionRoleOf(job) {
  return safeText(job?.usage?.execution_strategy_role, '')
}

export function executionLabelOf(job) {
  const strategy = executionStrategyOf(job)
  if (strategy === 'workforce') return 'Workforce'
  if (strategy === 'specialist') return 'Specialist'
  if (strategy === 'direct') return 'Direct'
  return 'Single'
}

const ACTIVITY_LABELS = {
  searching: 'Searching the web',
  opening: 'Opening page',
  browsing: 'Using browser',
  reading: 'Reading web content',
  building: 'Building file',
  done: 'Step complete',
}

export function activityText(activity) {
  if (!activity || typeof activity !== 'object') return ''
  const label = ACTIVITY_LABELS[activity.stage]
  if (!label) return ''
  const detail = safeText(activity.detail, '')
  return detail ? `${label} · ${detail}` : label
}

export function tabForJob(job) {
  if (!job) return 'Home'
  return 'Task'
}

export function safeStatusLabel(status) {
  if (!status || typeof status !== 'string') return 'Unknown'
  return status
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(' ')
}

export function safeText(value, fallback = 'Unknown') {
  if (typeof value === 'string' && value.trim()) return value.trim()
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return fallback
}

export function usageTokensReported(usage) {
  if (usage?.usage_available === true) return true
  if (usage?.usage_available === false) return false
  // Backward compatibility for jobs persisted before usage_available existed.
  return ['input_tokens', 'output_tokens', 'total_tokens'].some(
    (key) => Number(usage?.[key] || 0) > 0,
  )
}

const INTERNAL_ERROR_PATTERN = /(?:\b[A-Za-z]+Error\s*:|assigned to worker|\b[0-9a-f]{8}-[0-9a-f-]{27,}\b)/i

export function userFacingFailure(value, fallback = 'Task failed') {
  const detail = safeText(value, '')
  if (/timed?\s*out|timeout/i.test(detail)) return 'Task timed out'
  if (!detail || INTERNAL_ERROR_PATTERN.test(detail)) return fallback
  return detail
}

export function statusClass(status) {
  const normalized =
    typeof status === 'string' ? status.trim().toLowerCase() : ''
  const mapped = STATUS_CLASS_ALIASES[normalized] || normalized
  return ['waiting', 'working', 'done', 'failed'].includes(mapped)
    ? mapped
    : 'unknown'
}

function dependencyId(value) {
  if (typeof value === 'string' || typeof value === 'number') {
    return String(value)
  }
  if (value && typeof value === 'object') {
    return safeText(value.id || value.task_id, '')
  }
  return ''
}

function normalizeTask(task, index) {
  const source = task && typeof task === 'object' ? task : {}
  const id = safeText(source.id || source.task_id, `task-${index + 1}`)
  const dependencies = Array.isArray(source.dependencies)
    ? source.dependencies.map(dependencyId).filter(Boolean)
    : []
  return {
    ...source,
    id,
    renderKey: `${id}-${index}`,
    title: safeText(source.title, safeStatusLabel(id)),
    description: safeText(source.description, ''),
    preferred_agent: safeText(
      source.preferred_agent || source.preferredAgent,
      '',
    ),
    dependencies,
    expected_output: safeText(
      source.expected_output || source.expectedOutput,
      '',
    ),
  }
}

function uniqueKeys(...objects) {
  const keys = []
  objects.forEach((object) => {
    if (!object || typeof object !== 'object' || Array.isArray(object)) return
    Object.keys(object).forEach((key) => {
      if (!keys.includes(key)) keys.push(key)
    })
  })
  return keys
}

// The backend's task_created handler adds the job's own root task (id ===
// job_id, representing the whole request) into task_plan alongside its
// real subtasks, but nothing ever marks that root entry itself done --
// completion is only ever recorded via CAMEL's AllTasksCompletedEvent, a
// separate event type _apply_event doesn't map onto any per-task state. So
// the root entry stays "queued"/pending forever, even after every real
// subtask finishes. It was never a real, individually-trackable subtask
// (it has no assigned agent and no output of its own -- see getFinalAnswer,
// which never counts it since its output is always empty), so it's
// filtered out here rather than special-cased into "done" -- one place,
// so every consumer of getJobTasks (task list, progress checklist, running
// view) sees the same thing without needing its own copy of this filter.
function excludeRootTask(tasks, job) {
  const jobId = job?.job_id
  if (!jobId) return tasks
  return tasks.filter((task) => task.id !== jobId)
}

export function getJobTasks(job) {
  const plan = Array.isArray(job?.task_plan)
    ? job.task_plan
    : job?.task_plan?.tasks
  if (Array.isArray(plan) && plan.length > 0) {
    return excludeRootTask(plan.map(normalizeTask), job)
  }

  const dynamicIds = uniqueKeys(job?.task_states, job?.task_outputs)
  const ids =
    dynamicIds.length > 0 ? dynamicIds : uniqueKeys(job?.stages)
  if (ids.length > 0) {
    return excludeRootTask(
      ids.map((id, index) =>
        normalizeTask(
          {
            id,
            title: safeStatusLabel(id),
            dependencies: [],
          },
          index,
        ),
      ),
      job,
    )
  }

  return parseResult(job?.result).map((section, index) =>
    normalizeTask(
      {
        id: section.key,
        title: section.label,
        result: section.text,
      },
      index,
    ),
  )
}

function metadataForTask(job, taskId) {
  const candidates = [
    job?.task_metadata?.[taskId],
    job?.task_details?.[taskId],
    job?.tasks?.[taskId],
  ]
  return (
    candidates.find(
      (value) => value && typeof value === 'object' && !Array.isArray(value),
    ) || {}
  )
}

export function getTaskRuntime(job, task) {
  const metadata = metadataForTask(job, task.id)
  const status =
    job?.task_states?.[task.id] ??
    task.status ??
    metadata.status ??
    job?.stages?.[task.id] ??
    'waiting'
  const assignedAgent =
    task.assigned_agent ||
    task.assignedAgent ||
    task.assigned_to ||
    metadata.assigned_agent ||
    metadata.assigned_to ||
    metadata.agent

  return {
    status: safeText(status, 'unknown').toLowerCase(),
    statusLabel: safeStatusLabel(safeText(status, 'unknown')),
    statusClass: statusClass(status),
    agent: safeText(assignedAgent, ''),
    agentAssigned: Boolean(assignedAgent),
  }
}

export function getTaskOutput(job, task) {
  const value =
    job?.task_outputs?.[task.id] ??
    task.output ??
    task.result ??
    metadataForTask(job, task.id).output
  return typeof value === 'string' ? value.trim() : ''
}

export function getFinalTaskIds(tasks) {
  const upstreamIds = new Set(
    tasks.flatMap((task) =>
      Array.isArray(task.dependencies) ? task.dependencies : [],
    ),
  )
  return tasks
    .filter((task) => !upstreamIds.has(task.id))
    .map((task) => task.id)
}

// CAMEL's AUTO_DECOMPOSE root task sets its .result to a plain string-join
// of its subtasks' results, delimited by "--- Subtask <uuid>.N Result ---".
// When application-level synthesis produces nothing (a partial run, or a
// synthesis call that came back empty) that raw join IS job.result, and
// getFinalAnswer handed it to the chat verbatim -- so the answer opened
// with a bare internal task id. Strip the delimiters and keep every body
// they separate: this removes scaffolding, never content.
const SUBTASK_DELIMITER = /^[ \t]*-{2,}[ \t]*Sub(?:task)?[ \t]+\S+[ \t]+Result[ \t]*-{2,}[ \t]*$/gim

export function stripSubtaskScaffolding(text) {
  if (typeof text !== 'string' || !text.includes('Result ---')) return text
  const cleaned = text
    .split(SUBTASK_DELIMITER)
    .map((section) => section.trim())
    .filter(Boolean)
    .join('\n\n')
  return cleaned || text
}

// Some model calls echo a response-wrapper shape (e.g. a model that was
// ever shown a `{"content": "..."}` tool/output schema starts imitating it
// unprompted), so job.result can literally be the JSON-serialized string
// '{"content":"actual answer\n..."}' instead of plain text. Backends never
// unwrap this -- see stages.js callers -- so it must happen once, here, at
// the single point every render/export/copy path already funnels through.
// Deliberately narrow: only unwraps when the *entire* trimmed string parses
// as a JSON object with a string "content" field. Anything else -- normal
// prose, markdown, malformed JSON, or a legitimate JSON answer that isn't
// this exact wrapper shape -- passes through unchanged, so real user/model
// text that merely looks JSON-ish is never touched.
export function normalizeAssistantText(value) {
  if (typeof value !== 'string') return value
  const trimmed = value.trim()
  if (!trimmed.startsWith('{') || !trimmed.endsWith('}')) return value

  let parsed
  try {
    parsed = JSON.parse(trimmed)
  } catch {
    return value
  }

  if (
    parsed &&
    typeof parsed === 'object' &&
    !Array.isArray(parsed) &&
    typeof parsed.content === 'string'
  ) {
    return parsed.content
  }
  return value
}

function computeFinalAnswer(job, tasks) {
  const explicit = job?.final_answer || job?.final_output
  if (typeof explicit === 'string' && explicit.trim()) return explicit.trim()

  // A terminal Workforce job's result is the application-level synthesis,
  // while task_outputs remain the detailed execution trace. Prefer the clean
  // response here so internal task wording cannot become the chat answer.
  if (
    SUCCESS_JOB_STATUSES.has(job?.status) &&
    typeof job?.result === 'string' &&
    job.result.trim()
  ) {
    return stripSubtaskScaffolding(job.result.trim())
  }

  const finalIds = new Set(getFinalTaskIds(tasks))
  const finalTasks = tasks.filter((task) => finalIds.has(task.id))
  const synthesisTasks = finalTasks.filter((task) =>
    getTaskRuntime(job, task).agent.toLowerCase().includes('synthesis'),
  )
  const candidates = synthesisTasks.length > 0 ? synthesisTasks : finalTasks
  const outputs = candidates
    .map((task) => ({
      title: task.title,
      output: getTaskOutput(job, task),
    }))
    .filter(({ output }) => output)

  if (outputs.length === 1) return outputs[0].output
  if (outputs.length > 1) {
    return outputs
      .map(({ title, output }) => `${title}\n\n${output}`)
      .join('\n\n')
  }
  return typeof job?.result === 'string'
    ? stripSubtaskScaffolding(job.result.trim())
    : ''
}

// The single canonical "assistant answer" getter -- chat rendering, the
// completed-run view, history/reload (ThreadView -> ChatExchange), and
// export/copy (buildJobExport, copyResult) all resolve their displayed
// text through this function, so normalizing its return value here is
// sufficient to cover every one of those paths uniformly.
export function getFinalAnswer(job, tasks = getJobTasks(job)) {
  return normalizeAssistantText(computeFinalAnswer(job, tasks))
}

export function safeFilename(topic, extension) {
  const stem = safeText(topic, 'agent-result')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64)
  return `${stem || 'agent-result'}.${extension}`
}

export function buildJobExport(job, tasks, format = 'markdown') {
  const markdown = format === 'markdown'
  const title = safeText(job?.topic, 'Agent result')
  const lines = [markdown ? `# ${title}` : title]

  const attachments = Array.isArray(job?.attachments) ? job.attachments : []
  const workspaceFiles = Array.isArray(job?.workspace?.files)
    ? job.workspace.files
    : []
  if (attachments.length > 0 || workspaceFiles.length > 0) {
    lines.push('', markdown ? '## Workspace' : 'WORKSPACE')
    attachments.forEach((attachment) => {
      lines.push(
        '',
        `Source: ${safeText(attachment.filename, 'Unknown source')}`,
        `Type: ${safeText(attachment.kind, 'file')}`,
        `Files: ${attachment.file_count ?? 1}`,
      )
    })
    if (workspaceFiles.length > 0) {
      lines.push('', markdown ? '### Files' : 'FILES')
      workspaceFiles.forEach((file) => {
        lines.push(`- ${safeText(file.path, 'Unknown path')}`)
      })
    }
  }

  lines.push('', markdown ? '## Plan' : 'PLAN')
  tasks.forEach((task, index) => {
    const runtime = getTaskRuntime(job, task)
    lines.push(
      '',
      markdown ? `### ${index + 1}. ${task.title}` : `${index + 1}. ${task.title}`,
      `Agent: ${runtime.agentAssigned ? runtime.agent : 'Not yet assigned'}`,
      `Status: ${runtime.statusLabel}`,
    )
    if (task.description) lines.push(task.description)
  })

  const finalAnswer = getFinalAnswer(job, tasks)
  lines.push(
    '',
    markdown
      ? '## Final synthesized answer'
      : 'FINAL SYNTHESIZED ANSWER',
    '',
    finalAnswer || 'No final answer was produced.',
  )

  lines.push(
    '',
    markdown ? '## Individual task outputs' : 'INDIVIDUAL TASK OUTPUTS',
  )
  tasks.forEach((task) => {
    const output = getTaskOutput(job, task)
    const runtime = getTaskRuntime(job, task)
    lines.push(
      '',
      markdown ? `### ${task.title}` : task.title.toUpperCase(),
      `Agent: ${runtime.agentAssigned ? runtime.agent : 'Not yet assigned'}`,
      `Status: ${runtime.statusLabel}`,
      '',
      output || 'No task output was reported.',
    )
  })

  const usage = job?.usage || {}
  const tokensReported = usageTokensReported(usage)
  const duration =
    usage.total_duration_seconds ?? job?.duration_seconds ?? 'Not reported'
  const cost =
    usage.estimated_cost_usd === null ||
    usage.estimated_cost_usd === undefined
      ? 'Not reported'
      : `$${Number(usage.estimated_cost_usd).toFixed(6)}`
  lines.push(
    '',
    markdown ? '## Usage' : 'USAGE',
    `Model: ${safeText(usage.model_name || job?.model_name, 'Not reported')}`,
    `Provider: ${safeText(
      usage.provider || job?.model_provider,
      'Not reported',
    )}`,
    `Model calls: ${usage.model_call_count ?? job?.model_calls ?? 0}`,
    `Input tokens: ${tokensReported ? usage.input_tokens ?? 0 : 'Not reported'}`,
    `Output tokens: ${tokensReported ? usage.output_tokens ?? 0 : 'Not reported'}`,
    `Total tokens: ${tokensReported ? usage.total_tokens ?? 0 : 'Not reported'}`,
    `Estimated cost: ${cost}`,
    `Duration seconds: ${duration}`,
  )
  return `${lines.join('\n').trim()}\n`
}

/**
 * Backward-compatible parser for hydrated jobs created by the old backend.
 */
export function parseResult(raw) {
  if (!raw || typeof raw !== 'string') return []
  const parts = raw.split(/---\s*Task\s+(\S+)\s+Result\s*---/g)
  const sections = []
  for (let i = 1; i < parts.length; i += 2) {
    const key = parts[i]
    const text = (parts[i + 1] || '').trim()
    if (!text) continue
    sections.push({ key, label: safeStatusLabel(key), text })
  }
  if (sections.length === 0 && raw.trim()) {
    sections.push({ key: 'result', label: 'Result', text: raw.trim() })
  }
  return sections
}

// Extracted from ChatExchange so the streaming-stability rules can be
// tested directly (see frontend/render.test.mjs) instead of being
// re-implemented by a test that could drift from the real component.
export function stableStreamingMarkdown(value) {
  const text = typeof value === 'string' ? value : ''
  let boundary = text.length
  const positions = new Map([['**', []], ['__', []], ['~~', []]])
  let fenced = false
  let inlineCode = false
  // Byte offset of the ``` that opened the block we are currently inside,
  // so an unterminated fence can be closed rather than left dangling.
  let openFenceStart = -1

  for (let index = 0; index < text.length;) {
    if (text.startsWith('```', index)) {
      fenced = !fenced
      openFenceStart = fenced ? index : -1
      index += 3
      continue
    }
    if (!fenced && text[index] === '`' && text[index - 1] !== '\\') {
      inlineCode = !inlineCode
      index += 1
      continue
    }
    if (!fenced && !inlineCode && text[index - 1] !== '\\') {
      const marker = [...positions.keys()].find((candidate) =>
        text.startsWith(candidate, index),
      )
      if (marker) {
        positions.get(marker).push(index)
        index += marker.length
        continue
      }
    }
    index += 1
  }

  // An unbalanced emphasis marker is buffered away, but only when it sits
  // OUTSIDE a fence -- truncating at a `**` that is really Python's
  // **kwargs would swallow the rest of the code block.
  for (const markerPositions of positions.values()) {
    if (markerPositions.length % 2 === 1) {
      boundary = Math.min(boundary, markerPositions[markerPositions.length - 1])
    }
  }

  // A fence that is still open mid-stream reaches remark as an
  // unterminated block: everything after it -- including prose the model
  // has not finished writing -- gets absorbed into one code element, and
  // the moment the closing ``` arrives the whole tail reflows. That is the
  // "prose and code merged into one malformed block" case. Close it here
  // instead, so each streamed frame is valid Markdown on its own and the
  // block simply grows as content arrives.
  if (fenced && openFenceStart >= 0) {
    if (boundary <= openFenceStart) return text.slice(0, boundary)
    const body = text.slice(0, boundary)
    return body.endsWith('\n') ? `${body}\`\`\`` : `${body}\n\`\`\``
  }
  return text.slice(0, boundary)
}
