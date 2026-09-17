// Pure helpers for scheduled tasks: the preset <-> cron mapping the editor
// uses, human-readable schedule text, and run status labels. Kept out of the
// components so the rules can be tested directly -- see schedule.test.mjs.

export const DEFAULT_TIMEZONE = 'Europe/Istanbul'

export const WEEKDAYS = [
  { value: 1, label: 'Monday' },
  { value: 2, label: 'Tuesday' },
  { value: 3, label: 'Wednesday' },
  { value: 4, label: 'Thursday' },
  { value: 5, label: 'Friday' },
  { value: 6, label: 'Saturday' },
  { value: 0, label: 'Sunday' },
]

export const SCHEDULE_KINDS = [
  { value: 'daily', label: 'Every day' },
  { value: 'weekdays', label: 'Weekdays' },
  { value: 'weekly', label: 'Every week' },
  { value: 'monthly', label: 'Every month' },
  { value: 'once', label: 'Once' },
  { value: 'custom', label: 'Custom (cron)' },
]

// Variables the backend resolves at run time (scheduling/prompts.py).
export const PROMPT_VARIABLES = [
  { token: '{tarih}', hint: "the run's date" },
  { token: '{hafta}', hint: 'the 7 days up to the run' },
  { token: '{ay}', hint: 'month and year' },
  { token: '{son_calisma}', hint: 'date of the previous run' },
]

export const TASK_TEMPLATES = [
  {
    name: 'Weekly sector brief',
    prompt:
      '{hafta} döneminde sektörümüzdeki önemli gelişmeleri, rakip duyurularını ve mevzuat değişikliklerini araştır; kaynaklı 1-2 sayfalık PDF rapor hazırla.',
    schedule: { kind: 'weekly', weekday: 1, time: '09:00' },
  },
  {
    name: 'Daily market snapshot',
    prompt: '{tarih} itibarıyla dolar, euro, altın ve BIST 100 değerlerini tablo halinde özetle.',
    schedule: { kind: 'weekdays', time: '08:30' },
  },
  {
    name: 'Regulation watch',
    prompt:
      "Resmi Gazete'de {son_calisma} tarihinden bu yana yayımlanan ve inşaat sektörünü etkileyen düzenlemeleri araştır.",
    schedule: { kind: 'daily', time: '10:00' },
    watch: { strategies: ['research_diff'], urls: [], criteria: 'İnşaat sektörünü etkileyen yeni yönetmelik, tebliğ veya kanun' },
  },
]

const pad = (value) => String(value).padStart(2, '0')

function parseTime(time) {
  const match = /^(\d{1,2}):(\d{2})$/.exec(String(time || '').trim())
  if (!match) return null
  const hour = Number(match[1])
  const minute = Number(match[2])
  if (hour > 23 || minute > 59) return null
  return { hour, minute }
}

// Editor state -> the trigger fields the API takes. Returns { error } when
// the state cannot be turned into a schedule yet.
export function scheduleToTrigger(schedule, timezone = DEFAULT_TIMEZONE) {
  const kind = schedule?.kind || 'daily'
  if (kind === 'custom') {
    const expression = String(schedule.cron || '').trim().split(/\s+/).join(' ')
    if (expression.split(' ').length !== 5) {
      return { error: 'A cron expression has 5 fields: minute hour day month weekday.' }
    }
    return { trigger_kind: 'cron', cron_expr: expression, run_at: null, timezone }
  }
  if (kind === 'once') {
    const runAt = localDateTimeToEpoch(schedule.date, schedule.time, timezone)
    if (runAt == null) return { error: 'Pick a date and time.' }
    return { trigger_kind: 'once', cron_expr: null, run_at: runAt, timezone }
  }
  const time = parseTime(schedule.time)
  if (!time) return { error: 'Pick a time.' }
  const { hour, minute } = time
  const cronByKind = {
    daily: `${minute} ${hour} * * *`,
    weekdays: `${minute} ${hour} * * 1-5`,
    weekly: `${minute} ${hour} * * ${Number(schedule.weekday ?? 1)}`,
    monthly: `${minute} ${hour} ${Math.min(28, Math.max(1, Number(schedule.dayOfMonth ?? 1)))} * *`,
  }
  return { trigger_kind: 'cron', cron_expr: cronByKind[kind], run_at: null, timezone }
}

// A saved task -> editor state. Anything the presets cannot express opens
// as a custom cron rather than being rounded to the nearest preset.
export function triggerToSchedule(task, timezone = DEFAULT_TIMEZONE) {
  if (task?.trigger_kind === 'once' && task.run_at) {
    const { date, time } = epochToLocalParts(task.run_at, task.timezone || timezone)
    return { kind: 'once', date, time }
  }
  const expression = String(task?.cron_expr || '').trim()
  const fields = expression.split(/\s+/)
  if (fields.length === 5 && /^\d+$/.test(fields[0]) && /^\d+$/.test(fields[1])) {
    const time = `${pad(fields[1])}:${pad(fields[0])}`
    const [, , dom, month, dow] = fields
    if (month === '*') {
      if (dom === '*' && dow === '*') return { kind: 'daily', time }
      if (dom === '*' && dow === '1-5') return { kind: 'weekdays', time }
      if (dom === '*' && /^[0-7]$/.test(dow)) return { kind: 'weekly', weekday: Number(dow) % 7, time }
      if (/^\d+$/.test(dom) && Number(dom) <= 28 && dow === '*') {
        return { kind: 'monthly', dayOfMonth: Number(dom), time }
      }
    }
  }
  return { kind: 'custom', cron: expression }
}

const ordinal = (day) => {
  const mod100 = day % 100
  if (mod100 >= 11 && mod100 <= 13) return `${day}th`
  return `${day}${{ 1: 'st', 2: 'nd', 3: 'rd' }[day % 10] || 'th'}`
}

export function describeSchedule(task) {
  if (!task) return ''
  if (task.trigger_kind === 'once') {
    return task.run_at ? `Once, ${formatDateTime(task.run_at, task.timezone)}` : 'Once'
  }
  const schedule = triggerToSchedule(task)
  switch (schedule.kind) {
    case 'daily':
      return `Every day at ${schedule.time}`
    case 'weekdays':
      return `Weekdays at ${schedule.time}`
    case 'weekly': {
      const day = WEEKDAYS.find((item) => item.value === schedule.weekday)?.label
      return `Every ${day} at ${schedule.time}`
    }
    case 'monthly':
      return `Monthly on the ${ordinal(schedule.dayOfMonth)} at ${schedule.time}`
    default:
      return `Cron: ${task.cron_expr}`
  }
}

// Wall-clock parts of an instant in a named timezone.
export function epochToLocalParts(epochSeconds, timezone = DEFAULT_TIMEZONE) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone: timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    })
      .formatToParts(new Date(epochSeconds * 1000))
      .map((part) => [part.type, part.value]),
  )
  return { date: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}` }
}

// "2026-09-18" + "08:30" in `timezone` -> epoch seconds. Intl has no direct
// inverse, so start from the UTC reading and correct by the zone's offset
// at that moment (twice, which settles across a DST boundary).
export function localDateTimeToEpoch(date, time, timezone = DEFAULT_TIMEZONE) {
  const dateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(date || ''))
  const parsedTime = parseTime(time)
  if (!dateMatch || !parsedTime) return null
  const wanted = Date.UTC(
    Number(dateMatch[1]),
    Number(dateMatch[2]) - 1,
    Number(dateMatch[3]),
    parsedTime.hour,
    parsedTime.minute,
  )
  let guess = wanted
  for (let step = 0; step < 2; step += 1) {
    const local = epochToLocalParts(guess / 1000, timezone)
    const [y, m, d] = local.date.split('-').map(Number)
    const [hh, mm] = local.time.split(':').map(Number)
    guess += wanted - Date.UTC(y, m - 1, d, hh, mm)
  }
  return Math.round(guess / 1000)
}

export function formatDateTime(epochSeconds, timezone = DEFAULT_TIMEZONE) {
  if (!epochSeconds) return ''
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: timezone,
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(new Date(epochSeconds * 1000))
}

export function formatRelative(epochSeconds, nowMs = Date.now()) {
  if (!epochSeconds) return ''
  const diff = Math.round(epochSeconds - nowMs / 1000)
  const abs = Math.abs(diff)
  const units = [
    [86400, 'day'],
    [3600, 'hour'],
    [60, 'minute'],
  ]
  for (const [seconds, unit] of units) {
    if (abs >= seconds) {
      const value = Math.round(abs / seconds)
      const label = `${value} ${unit}${value === 1 ? '' : 's'}`
      return diff >= 0 ? `in ${label}` : `${label} ago`
    }
  }
  return diff >= 0 ? 'in under a minute' : 'just now'
}

const RUN_STATUS = {
  running: { label: 'Running', variant: 'running' },
  done: { label: 'Done', variant: 'success' },
  partial: { label: 'Partial', variant: 'warning' },
  failed: { label: 'Failed', variant: 'destructive' },
  error: { label: 'Error', variant: 'destructive' },
  cancelled: { label: 'Cancelled', variant: 'secondary' },
  interrupted: { label: 'Interrupted', variant: 'warning' },
  skipped_overlap: { label: 'Skipped', variant: 'secondary' },
  missed: { label: 'Missed', variant: 'secondary' },
  no_change: { label: 'No change', variant: 'secondary' },
}

export function runStatusMeta(status) {
  return RUN_STATUS[status] || { label: status || 'Unknown', variant: 'outline' }
}

export const RUN_TRIGGER_LABELS = {
  schedule: 'Scheduled',
  catch_up: 'Catch-up',
  manual: 'Run now',
  retry: 'Retry',
}

// Editor state for a new task, optionally prefilled from a finished chat
// ("Repeat on a schedule") or a template.
export function newTaskDraft(seed = {}) {
  return {
    name: seed.name || '',
    prompt: seed.prompt || '',
    mode: seed.mode || 'multi',
    deepResearch: Boolean(seed.deepResearch),
    skillIds: seed.skillIds || [],
    schedule: seed.schedule || { kind: 'weekly', weekday: 1, time: '09:00' },
    watchEnabled: Boolean(seed.watch),
    watch: {
      strategies: seed.watch?.strategies || ['research_diff'],
      urls: (seed.watch?.urls || []).join('\n'),
      criteria: seed.watch?.criteria || '',
    },
    includePreviousSummary: seed.includePreviousSummary ?? true,
  }
}

export function draftFromTask(task) {
  return newTaskDraft({
    name: task.name,
    prompt: task.prompt,
    mode: task.mode,
    deepResearch: task.deep_research,
    skillIds: task.skill_ids,
    schedule: triggerToSchedule(task),
    watch: task.watch,
    includePreviousSummary: task.include_previous_summary,
  })
}

// Editor state -> request body. Returns { error } for anything the form
// can catch before the server does.
export function draftToPayload(draft, timezone = DEFAULT_TIMEZONE) {
  if (!draft.name.trim()) return { error: 'Give the task a name.' }
  if (!draft.prompt.trim()) return { error: 'Write what the agent should do.' }
  const trigger = scheduleToTrigger(draft.schedule, timezone)
  if (trigger.error) return trigger
  let watch = null
  if (draft.watchEnabled) {
    const urls = draft.watch.urls
      .split(/\s+/)
      .map((url) => url.trim())
      .filter(Boolean)
    const strategies = draft.watch.strategies.filter((strategy) =>
      ['url_hash', 'research_diff'].includes(strategy),
    )
    if (strategies.length === 0) return { error: 'Choose how changes are detected.' }
    if (strategies.includes('url_hash') && urls.length === 0) {
      return { error: 'Add at least one page to watch.' }
    }
    watch = { strategies, urls, criteria: draft.watch.criteria.trim() }
  }
  return {
    payload: {
      name: draft.name.trim(),
      prompt: draft.prompt.trim(),
      mode: draft.mode,
      deep_research: draft.mode === 'multi' && draft.deepResearch,
      skill_ids: draft.skillIds,
      ...trigger,
      watch,
      include_previous_summary: draft.includePreviousSummary,
    },
  }
}
