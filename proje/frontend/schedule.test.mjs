// Preset <-> cron mapping, schedule text and draft validation for scheduled
// tasks. Imports the real module so it cannot drift from the editor.
import {
  describeSchedule,
  draftToPayload,
  localDateTimeToEpoch,
  epochToLocalParts,
  newTaskDraft,
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

if (failures > 0) {
  console.error(`${failures} schedule check(s) failed`)
  process.exit(1)
}
console.log('All schedule checks passed')
