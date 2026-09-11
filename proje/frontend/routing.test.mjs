// Route parsing/building and the conversation-resolution rule that the
// refresh/deep-link restoration depends on. Imports the real module so it
// cannot drift from what App.jsx actually runs.
import {
  conversationIdOf,
  readRoute,
  routePath,
} from './src/routing.js'

let failures = 0
const check = (name, ok, detail = '') => {
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${ok ? '' : ` -- ${detail}`}`)
  if (!ok) failures++
}
const eq = (name, actual, expected) =>
  check(
    name,
    JSON.stringify(actual) === JSON.stringify(expected),
    `got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`,
  )

// --- readRoute ---------------------------------------------------------
eq('bare / is the picker', readRoute('/'), {
  workspaceId: null,
  conversationId: null,
})
eq('workspace URL', readRoute('/w/ws-1'), {
  workspaceId: 'ws-1',
  conversationId: null,
})
eq('workspace URL with trailing slash', readRoute('/w/ws-1/'), {
  workspaceId: 'ws-1',
  conversationId: null,
})
eq('conversation URL', readRoute('/w/ws-1/c/conv-9'), {
  workspaceId: 'ws-1',
  conversationId: 'conv-9',
})
eq('conversation URL with trailing slash', readRoute('/w/ws-1/c/conv-9/'), {
  workspaceId: 'ws-1',
  conversationId: 'conv-9',
})
eq('uuids round-trip', readRoute('/w/3ec7b3d6-3d15-4d67-9992-db7711b8336e'), {
  workspaceId: '3ec7b3d6-3d15-4d67-9992-db7711b8336e',
  conversationId: null,
})
eq('unknown path falls back to picker', readRoute('/nope/deep'), {
  workspaceId: null,
  conversationId: null,
})
eq('malformed escape does not throw', readRoute('/w/%E0%A4%A'), {
  workspaceId: null,
  conversationId: null,
})

// --- routePath ---------------------------------------------------------
eq('no workspace builds /', routePath(null, null), '/')
eq('conversation without workspace still builds /', routePath(null, 'c1'), '/')
eq('workspace path', routePath('ws-1', null), '/w/ws-1')
eq('conversation path', routePath('ws-1', 'conv-9'), '/w/ws-1/c/conv-9')

// --- round-trip --------------------------------------------------------
for (const [ws, conv] of [
  ['ws-1', null],
  ['ws-1', 'conv-9'],
  ['a b/c', 'd?e#f'],
  ['3ec7b3d6-3d15-4d67-9992-db7711b8336e', '5e5d04db-9361-424b-a862-faaf0e2f4db7'],
]) {
  const parsed = readRoute(routePath(ws, conv))
  eq(`round-trip ${JSON.stringify([ws, conv])}`, parsed, {
    workspaceId: ws,
    conversationId: conv,
  })
}

// --- conversationIdOf --------------------------------------------------
eq(
  'conversation_id wins',
  conversationIdOf({ conversation_id: 'c', workspace_id: 'w', job_id: 'j' }),
  'c',
)
eq(
  'falls back to workspace_id for pre-conversation jobs',
  conversationIdOf({ workspace_id: 'w', job_id: 'j' }),
  'w',
)
eq('falls back to job_id', conversationIdOf({ job_id: 'j' }), 'j')
eq('null-safe', conversationIdOf(null), null)

// --- newest-job resolution (mirrors App.newestJobInConversation) --------
const jobs = {
  j1: { job_id: 'j1', conversation_id: 'c1', created_at: 100 },
  j2: { job_id: 'j2', conversation_id: 'c1', created_at: 300 },
  j3: { job_id: 'j3', conversation_id: 'c2', created_at: 200 },
  j4: { job_id: 'j4', workspace_id: 'w9', created_at: 50 },
}
const order = ['j2', 'j3', 'j1', 'j4']
const newestIn = (conversationId) => {
  const matches = order
    .map((id) => jobs[id])
    .filter((job) => job && conversationIdOf(job) === conversationId)
  if (matches.length === 0) return null
  return matches.reduce((newest, job) =>
    (job.created_at || 0) > (newest.created_at || 0) ? job : newest,
  )
}
eq('newest job in a multi-job conversation', newestIn('c1')?.job_id, 'j2')
eq('single-job conversation', newestIn('c2')?.job_id, 'j3')
eq('legacy job keyed by workspace_id', newestIn('w9')?.job_id, 'j4')
check(
  'unknown conversation resolves to null (caller keeps URL until hydrated)',
  newestIn('does-not-exist') === null,
)

console.log(
  failures ? `\n${failures} FAILED` : '\nAll routing/deep-link checks passed',
)
process.exit(failures ? 1 : 0)
