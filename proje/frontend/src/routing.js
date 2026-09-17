// URL <-> navigation state.
//
// Navigation used to be session-only: every load started at the Workspace
// picker, so a refresh mid-conversation silently threw the user back to the
// entry screen. The History API is enough to fix that without adding a
// router dependency -- nginx (try_files $uri /index.html) and Vite's dev
// server already serve the SPA for these deep paths, so the only missing
// piece was the app agreeing with its own address bar.
//
//   /                                 workspace picker
//   /w/:workspaceId                   workspace home
//   /w/:workspaceId/c/:conversationId one conversation
//   /w/:workspaceId/scheduled         the workspace's scheduled tasks
//
// Kept in its own module (rather than inline in App.jsx) so the parsing and
// building rules can be tested directly -- see frontend/routing.test.mjs.
const ROUTE_PATTERN = /^\/w\/([^/]+)(?:\/c\/([^/]+)|\/(scheduled))?\/?$/

export function readRoute(pathname) {
  const target =
    pathname ??
    (typeof window !== 'undefined' ? window.location.pathname : '/')
  const match = ROUTE_PATTERN.exec(target)
  if (!match) return { workspaceId: null, conversationId: null }
  let workspaceId
  let conversationId
  try {
    workspaceId = decodeURIComponent(match[1])
    conversationId = match[2] ? decodeURIComponent(match[2]) : null
  } catch {
    // A malformed percent-escape must not throw during the first render.
    return { workspaceId: null, conversationId: null }
  }
  // `view` is only present for non-default views, so every existing route
  // keeps its exact two-key shape.
  return match[3]
    ? { workspaceId, conversationId, view: 'scheduled' }
    : { workspaceId, conversationId }
}

export function routePath(workspaceId, conversationId, view = null) {
  if (!workspaceId) return '/'
  const base = `/w/${encodeURIComponent(workspaceId)}`
  if (view === 'scheduled') return `${base}/scheduled`
  return conversationId
    ? `${base}/c/${encodeURIComponent(conversationId)}`
    : base
}

// A job's conversation key. Older jobs predate conversation_id, so they
// fall back the same way the rest of the app does.
export function conversationIdOf(job) {
  return job?.conversation_id || job?.workspace_id || job?.job_id || null
}
