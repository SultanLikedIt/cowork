// Preset <-> cron mapping, schedule text and draft validation for scheduled
// tasks. Imports the real module so it cannot drift from the editor.
import {
  buildTaskTree,
  chainDescendantIds,
  deliveryStatusMeta,
  describeSchedule,
  draftFromTask,
  draftToPayload,
  localDateTimeToEpoch,
  epochToLocalParts,
  newTaskDraft,
  outlookProblem,
  parseAddresses,
  scheduleToTrigger,
  triggerToSchedule,
  formatRelative,
} from './src/schedule.js'

let failures = 0
const check = (name, ok, detail = '') => {
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${ok ? '' : ` -- ${detail}`}`)
  if (!ok) failures++
}
const eq = (name, actual, expected) =>
  check(name, JSON.stringify(actual) === JSON.stringify(expected), `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`)

const IST = 'Europe/Istanbul'

eq('daily preset', scheduleToTrigger({ kind: 'daily', time: '09:00' }).cron_expr, '0 9 * * *')
eq('weekdays preset', scheduleToTrigger({ kind: 'weekdays', time: '08:30' }).cron_expr, '30 8 * * 1-5')
eq('weekly preset', scheduleToTrigger({ kind: 'weekly', weekday: 0, time: '17:05' }).cron_expr, '5 17 * * 0')
eq('monthly preset clamps to 28', scheduleToTrigger({ kind: 'monthly', dayOfMonth: 31, time: '09:00' }).cron_expr, '0 9 28 * *')
eq('custom cron normalizes spaces', scheduleToTrigger({ kind: 'custom', cron: ' 0  9 * * 1 ' }).cron_expr, '0 9 * * 1')
check('custom cron needs five fields', Boolean(scheduleToTrigger({ kind: 'custom', cron: '0 9 * *' }).error))
check('missing time is an error', Boolean(scheduleToTrigger({ kind: 'daily', time: '' }).error))

for (const schedule of [
  { kind: 'daily', time: '09:00' },
  { kind: 'weekdays', time: '08:30' },
  { kind: 'weekly', weekday: 3, time: '14:15' },
  { kind: 'monthly', dayOfMonth: 15, time: '07:00' },
]) {
  eq(`round trip ${schedule.kind}`, triggerToSchedule(scheduleToTrigger(schedule)), schedule)
}
eq('unrecognised cron opens as custom', triggerToSchedule({ cron_expr: '*/30 9-17 * * 1-5' }), { kind: 'custom', cron: '*/30 9-17 * * 1-5' })
eq('weekday 7 reads as Sunday', triggerToSchedule({ cron_expr: '0 9 * * 7' }), { kind: 'weekly', weekday: 0, time: '09:00' })

eq('describe weekly', describeSchedule({ trigger_kind: 'cron', cron_expr: '0 9 * * 1' }), 'Every Monday at 09:00')
eq('describe weekdays', describeSchedule({ trigger_kind: 'cron', cron_expr: '30 8 * * 1-5' }), 'Weekdays at 08:30')
eq('describe monthly', describeSchedule({ trigger_kind: 'cron', cron_expr: '0 9 1 * *' }), 'Monthly on the 1st at 09:00')
eq('describe custom', describeSchedule({ trigger_kind: 'cron', cron_expr: '*/30 * * * *' }), 'Cron: */30 * * * *')

const epoch = localDateTimeToEpoch('2026-09-18', '08:30', IST)
eq('local time to epoch (UTC+3)', epoch, Date.UTC(2026, 8, 18, 5, 30) / 1000)
eq('epoch back to local parts', epochToLocalParts(epoch, IST), { date: '2026-09-18', time: '08:30' })
eq('once round trip', triggerToSchedule(scheduleToTrigger({ kind: 'once', date: '2026-09-18', time: '08:30' }, IST)), { kind: 'once', date: '2026-09-18', time: '08:30' })

check('draft needs a name', Boolean(draftToPayload(newTaskDraft({ prompt: 'x' })).error))
const withWatch = newTaskDraft({ name: 'N', prompt: 'P' })
withWatch.watchEnabled = true
withWatch.watch.strategies = ['url_hash']
check('url watch needs a URL', Boolean(draftToPayload(withWatch).error))
withWatch.watch.urls = ' https://a.example/\nhttps://b.example/ '
const { payload } = draftToPayload(withWatch)
eq('watch payload', payload.watch, { strategies: ['url_hash'], urls: ['https://a.example/', 'https://b.example/'], criteria: '' })
const single = newTaskDraft({ name: 'N', prompt: 'P', mode: 'single', deepResearch: true })
eq('deep research only on workforce', draftToPayload(single).payload.deep_research, false)

const now = Date.UTC(2026, 8, 17, 12, 0)
eq('relative future', formatRelative(now / 1000 + 3 * 3600, now), 'in 3 hours')
eq('relative past', formatRelative(now / 1000 - 86400, now), '1 day ago')

// --- chains ------------------------------------------------------------
const afterTrigger = scheduleToTrigger({ kind: 'after_task', upstreamTaskId: 't-1', condition: 'change' })
eq('after_task trigger', afterTrigger, {
  trigger_kind: 'after_task', cron_expr: null, run_at: null, timezone: IST, upstream_task_id: 't-1', condition: 'change',
})
check('after_task needs an upstream', Boolean(scheduleToTrigger({ kind: 'after_task', upstreamTaskId: '' }).error))
eq('after_task round trip', triggerToSchedule({ trigger_kind: 'after_task', upstream_task_id: 't-1', condition: 'change' }), {
  kind: 'after_task', upstreamTaskId: 't-1', condition: 'change',
})
eq('describe follower', describeSchedule({ trigger_kind: 'after_task', upstream_name: 'Watch', condition: 'change' }), 'After Watch · on change')
eq('describe orphan', describeSchedule({ trigger_kind: 'after_task', upstream_name: null, condition: 'success' }), 'After a deleted task')

const followerDraft = newTaskDraft({
  name: 'F', prompt: 'P',
  schedule: { kind: 'after_task', upstreamTaskId: 't-1', condition: 'always' },
  passUpstreamFiles: false,
  attachedFiles: [{ file_id: 'f-1', filename: 'a.docx' }],
})
const followerPayload = draftToPayload(followerDraft).payload
eq('follower payload chain fields', [followerPayload.upstream_task_id, followerPayload.condition, followerPayload.pass_upstream_result, followerPayload.pass_upstream_files], ['t-1', 'always', true, false])
eq('attached file ids', followerPayload.attached_file_ids, ['f-1'])
eq('cron payload clears upstream', draftToPayload(newTaskDraft({ name: 'N', prompt: 'P' })).payload.upstream_task_id, null)

const tasks = [
  { task_id: 'c', upstream_task_id: 'b', created_at: 3 },
  { task_id: 'x', upstream_task_id: null, created_at: 5 },
  { task_id: 'b', upstream_task_id: 'a', created_at: 2 },
  { task_id: 'a', upstream_task_id: null, created_at: 1 },
  { task_id: 'b2', upstream_task_id: 'a', created_at: 4 },
  { task_id: 'o', upstream_task_id: 'gone', created_at: 6 },
]
eq('task tree order and depth', buildTaskTree(tasks).map(({ task, depth }) => `${task.task_id}:${depth}`), ['x:0', 'a:0', 'b:1', 'c:2', 'b2:1', 'o:0'])
eq('descendants', [...chainDescendantIds(tasks, 'a')].sort(), ['b', 'b2', 'c'])

// --- email delivery ---
eq('addresses split and dedupe', parseAddresses('a@x.com, A@x.com; b@x.com\nc@x.com'), { addresses: ['a@x.com', 'b@x.com', 'c@x.com'] })
eq('bad address', parseAddresses('a@x.com, ali@'), { error: 'Not an email address: ali@' })
const emailBase = { ...newTaskDraft({ name: 'Bülten', prompt: 'Araştır' }), emailEnabled: true }
eq('email payload', draftToPayload({ ...emailBase, email: { to: 'a@x.com', cc: '', on: 'success', attachFiles: false } }).payload.delivery, { to: ['a@x.com'], cc: [], on: 'success', attach_files: false })
eq('email needs a recipient', draftToPayload({ ...emailBase, email: { to: ' ', cc: '', on: 'success', attachFiles: true } }).error, 'Add at least one email recipient.')
eq('email off sends null', draftToPayload(newTaskDraft({ name: 'x', prompt: 'y' })).payload.delivery, null)
const savedWithEmail = { name: 'n', prompt: 'p', mode: 'multi', skill_ids: [], trigger_kind: 'cron', cron_expr: '0 9 * * 1', delivery: { to: ['a@x.com', 'b@x.com'], cc: ['c@x.com'], on: 'change', attach_files: true }, watch: { strategies: ['research_diff'], urls: [], criteria: '' } }
eq('draft from saved delivery', draftFromTask(savedWithEmail).email, { to: 'a@x.com, b@x.com', cc: 'c@x.com', on: 'change', attachFiles: true })
check('draft from saved delivery enables email', draftFromTask(savedWithEmail).emailEnabled === true)
eq('watch task defaults to change', newTaskDraft({ watch: { strategies: ['research_diff'] } }).email.on, 'change')
eq('delivery status label', deliveryStatusMeta('failed').label, 'Email failed')
eq('no delivery status', deliveryStatusMeta(null), null)
eq('outlook ready', outlookProblem({ enabled: true, token_configured: true, bridge_reachable: true, bridge_token_configured: true, outlook_available: true }), null)
check('outlook bridge down explained', /bridge is not running/.test(outlookProblem({ enabled: true, token_configured: true, bridge_reachable: false })))

if (failures > 0) {
  console.error(`${failures} schedule check(s) failed`)
  process.exit(1)
}
console.log('All schedule checks passed')
