// All requests go through Vite's /api proxy -> http://127.0.0.1:8000
const BASE = '/api'

export function formatErrorDetail(detail, fallback) {
  if (detail == null || detail === '') return fallback
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object' && item.msg) return String(item.msg)
        return ''
      })
      .filter(Boolean)
    return parts.join('; ') || fallback
  }
  if (typeof detail === 'object') {
    if (detail.message) {
      const message = String(detail.message)
      const reason = detail.reason ? String(detail.reason) : ''
      return reason ? `${message} (${reason})` : message
    }
    if (detail.msg) return String(detail.msg)
  }
  return fallback
}

async function request(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, options)
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (body.detail != null) {
        detail = formatErrorDetail(body.detail, detail)
      }
    } catch {
      // non-JSON error body; keep the status line
    }
    const error = new Error(
      response.status >= 500
        ? 'The server could not complete the request. Please try again.'
        : detail,
    )
    error.status = response.status
    throw error
  }
  return response.json()
}

export function startPipeline(
  {
    topic,
    fileId,
    fileIds = [],
    contextText,
    mode = 'single',
    workspaceId,
    conversationId,
    skillIds = [],
    deepResearch = false,
  },
  options = {},
) {
  return request('/run-pipeline', {
    ...options,
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      topic,
      file_id: fileId ?? null,
      file_ids: fileIds,
      context_text: contextText ?? null,
      mode,
      // Absent/unknown is fine -- the backend silently creates one rather
      // than rejecting the request. See App.jsx's workspace_id handling.
      workspace_id: workspaceId ?? null,
      conversation_id: conversationId ?? null,
      skill_ids: skillIds,
      // Opt-in only, never auto-detected -- see RunPipelineRequest.deep_research.
      deep_research: deepResearch,
    }),
  })
}

export function getWorkspaces(options = {}) {
  return request('/workspaces', options)
}

export function createWorkspace({ name } = {}, options = {}) {
  return request('/workspaces', {
    ...options,
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name ?? null }),
  })
}

export function deleteWorkspace(workspaceId, options = {}) {
  return request(`/workspaces/${workspaceId}`, {
    ...options,
    method: 'DELETE',
  })
}

export function updateWorkspace(workspaceId, { name }, options = {}) {
  return request(`/workspaces/${workspaceId}`, {
    ...options,
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
}

export function getJobs(options = {}) {
  return request('/jobs', options)
}

export function getJob(jobId, options = {}) {
  return request(`/jobs/${jobId}`, options)
}

export function cancelJob(jobId, options = {}) {
  return request(`/jobs/${jobId}/cancel`, {
    ...options,
    method: 'POST',
  })
}

// Exports return a binary document, not JSON, so this can't go through
// request() -- but it reuses the same error-detail handling.
export async function exportJobDocument(
  jobId,
  { format, title, content },
  options = {},
) {
  const response = await fetch(`${BASE}/jobs/${jobId}/export/${format}`, {
    ...options,
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, content }),
  })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (body.detail != null) {
        detail = formatErrorDetail(body.detail, detail)
      }
    } catch {
      // non-JSON error body; keep the status line
    }
    const error = new Error(
      response.status >= 500
        ? 'The server could not generate the file. Please try again.'
        : detail,
    )
    error.status = response.status
    throw error
  }
  return response.blob()
}

// A direct URL, not a fetch: artifacts are real files the backend serves with
// their own Content-Type and Content-Disposition, so the browser should
// handle them natively (download a spreadsheet, open a generated page). Going
// through request() would pull the bytes into JS for no benefit and lose the
// filename the server already set.
//
// Each segment is encoded separately so a bundle path like
// "kitap-arama/index.html" keeps its separator while a filename with spaces or
// non-ASCII characters still survives -- encodeURIComponent on the whole path
// would escape the slash and 404.
export function artifactUrl(jobId, downloadPath) {
  const segments = String(downloadPath || '')
    .split('/')
    .filter(Boolean)
    .map(encodeURIComponent)
    .join('/')
  return `${BASE}/jobs/${encodeURIComponent(jobId)}/artifacts/${segments}`
}

// A file the user attached, served back by file_id. Same reasoning as
// artifactUrl above: a plain href lets the browser use the server's own
// Content-Disposition instead of pulling the bytes through JS.
export function uploadUrl(fileId) {
  return `${BASE}/uploads/${encodeURIComponent(String(fileId || ''))}`
}

// A bundle (create_web_bundle / create_code_bundle) is a folder of files;
// this hits the ZIP endpoint instead of the single-file one, using the
// `zip_download_path` the job snapshot already reports for a bundle entry.
export function artifactBundleZipUrl(jobId, zipDownloadPath) {
  const segments = String(zipDownloadPath || '')
    .split('/')
    .filter(Boolean)
    .map(encodeURIComponent)
    .join('/')
  return `${BASE}/jobs/${encodeURIComponent(jobId)}/artifact-bundles/${segments}`
}

export function deleteConversation(conversationId, options = {}) {
  return request(`/conversations/${conversationId}`, {
    ...options,
    method: 'DELETE',
  })
}

// Read-only in v1: there is no addSkill counterpart, unlike workers.
export function getSkills(options = {}) {
  return request('/skills', options)
}

export function getWorkers(options = {}) {
  return request('/workers', options)
}

export function addWorker({ name, description }, options = {}) {
  return request('/workers', {
    ...options,
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description: description || null }),
  })
}

export function executeCode({ code, language = 'python', workspaceId }, options = {}) {
  return request('/execute', {
    ...options,
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      code,
      language,
      workspace_id: workspaceId ?? null,
    }),
  })
}

export function uploadFile(file, options = {}) {
  const form = new FormData()
  form.append('file', file)
  return request('/upload', { ...options, method: 'POST', body: form })
}

export function uploadFiles(files, options = {}) {
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  return request('/uploads', { ...options, method: 'POST', body: form })
}

// --- Scheduled tasks (backend/scheduling/api.py) ---------------------------

function jsonRequest(path, method, body, options = {}) {
  return request(path, {
    ...options,
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export function getScheduledTasks(workspaceId, options = {}) {
  const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ''
  return request(`/scheduled-tasks${query}`, options)
}

export function createScheduledTask(task, options = {}) {
  return jsonRequest('/scheduled-tasks', 'POST', task, options)
}

export function updateScheduledTask(taskId, changes, options = {}) {
  return jsonRequest(`/scheduled-tasks/${encodeURIComponent(taskId)}`, 'PATCH', changes, options)
}

export function deleteScheduledTask(taskId, options = {}) {
  return request(`/scheduled-tasks/${encodeURIComponent(taskId)}`, { ...options, method: 'DELETE' })
}

export function pauseScheduledTask(taskId, options = {}) {
  return jsonRequest(`/scheduled-tasks/${encodeURIComponent(taskId)}/pause`, 'POST', undefined, options)
}

export function resumeScheduledTask(taskId, options = {}) {
  return jsonRequest(`/scheduled-tasks/${encodeURIComponent(taskId)}/resume`, 'POST', undefined, options)
}

export function runScheduledTaskNow(taskId, options = {}) {
  return jsonRequest(`/scheduled-tasks/${encodeURIComponent(taskId)}/run-now`, 'POST', undefined, options)
}

export function getScheduledRuns(taskId, options = {}) {
  return request(`/scheduled-tasks/${encodeURIComponent(taskId)}/runs`, options)
}

export function markScheduledRunsSeen(taskId, options = {}) {
  return jsonRequest(`/scheduled-tasks/${encodeURIComponent(taskId)}/runs/seen`, 'POST', undefined, options)
}

export function previewSchedule(schedule, options = {}) {
  return jsonRequest('/scheduled-tasks/preview', 'POST', schedule, options)
}

export function parseSchedule({ text, timezone }, options = {}) {
  return jsonRequest('/scheduled-tasks/parse-schedule', 'POST', { text, timezone: timezone ?? null }, options)
}

// --- Outlook (backend/outlook.py) --------------------------------------------

export function getOutlookStatus(options = {}) {
  return request('/integrations/outlook/status', options)
}

export function sendOutlookTest(to, options = {}) {
  return jsonRequest('/integrations/outlook/test', 'POST', { to }, options)
}
