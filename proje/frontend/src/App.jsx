import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import {
  addWorker,
  cancelJob,
  createWorkspace,
  deleteConversation,
  deleteWorkspace,
  updateWorkspace,
  getJob,
  getJobs,
  getWorkers,
  getSkills,
  getWorkspaces,
  getScheduledTasks,
  startPipeline,
  uploadFiles,
} from './api.js'
import { AlertCircle } from 'lucide-react'
import AddAgentModal from './components/AddAgentModal.jsx'
import { ACCEPTED_UPLOAD_EXTENSIONS } from './components/Composer.jsx'
import HomeView from './components/HomeView.jsx'
import RightPanel from './components/RightPanel.jsx'
import ScheduledView from './components/ScheduledView.jsx'
import Sidebar from './components/Sidebar.jsx'
import ThreadView from './components/ThreadView.jsx'
import TopBar from './components/TopBar.jsx'
import WorkspacePicker from './components/WorkspacePicker.jsx'
import { TooltipProvider } from './components/ui/tooltip.jsx'
import { conversationIdOf, readRoute, routePath } from './routing.js'
import { newTaskDraft } from './schedule.js'
import { executionLabelOf, isActiveJob, tabForJob } from './stages.js'

// Internal view ids stay 'Home'/'Task' (matches tabForJob() in stages.js
// and the tab === 'Task' checks throughout this file). 'Task' renders the
// selected conversation (ThreadView), not a single job -- see threadJobs
// below. These are no longer user-facing: the visible Home/Current Task
// tab strip was removed because it duplicated the sidebar, which already
// owns both "open this conversation" and "start a new one".
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024
const MAX_BATCH_UPLOAD_BYTES = 50 * 1024 * 1024
const MAX_UPLOAD_SOURCES = 10
// Derived, never restated: Composer owns the one upload allowlist (its
// accept= attribute and drop filter read the same array). Listing the
// extensions again here is what let an image pass Composer's picker and
// then fail this gate with a message naming only the old formats.
const SUPPORTED_EXTENSIONS = new Set(
  ACCEPTED_UPLOAD_EXTENSIONS.map((extension) => extension.replace(/^\./, '')),
)
const SUPPORTED_EXTENSIONS_LABEL = [...SUPPORTED_EXTENSIONS]
  .map((extension) => extension.toUpperCase())
  .join(', ')

function userSafeMessage(error, fallback) {
  if (error?.name === 'AbortError') return null
  const message = error?.message
  if (
    !message ||
    message === 'Failed to fetch' ||
    message === '[object Object]'
  ) {
    return fallback
  }
  return message
}

function withoutKey(object, key) {
  const copy = { ...object }
  delete copy[key]
  return copy
}

function attachmentSnapshot(attachments) {
  return attachments.map((attachment) => ({
    file_id: attachment.file_id,
    filename: attachment.filename,
    kind: attachment.kind || 'file',
    file_count: attachment.workspace_files?.length || 1,
    total_size_bytes: attachment.size_bytes || 0,
    ignored_file_count: attachment.ignored_file_count || 0,
  }))
}

function createJobStub({
  job_id,
  workspace_id,
  conversation_id,
  topic,
  mode,
  deepResearch = false,
  attachments,
  optimistic = false,
}) {
  return {
    job_id,
    workspace_id,
    conversation_id,
    topic,
    status: 'queued',
    mode,
    deep_research: deepResearch,
    stages: {},
    task_plan: null,
    planning_fallback: false,
    planning_errors: [],
    task_states: {},
    task_outputs: {},
    current_activity: null,
    attachments: attachmentSnapshot(attachments),
    workspace: null,
    usage: {
      usage_available: false,
      model_call_count: 0,
      input_tokens: 0,
      output_tokens: 0,
      total_tokens: 0,
      estimated_cost_usd: null,
      total_duration_seconds: null,
    },
    cancellation_requested: false,
    result: null,
    error: null,
    created_at: Date.now() / 1000,
    ...(optimistic ? { _optimistic: true } : {}),
  }
}

export default function App() {
  // Read once, synchronously, so the very first paint already reflects the
  // requested route instead of rendering the picker and correcting later.
  const initialRouteRef = useRef(readRoute())
  // A third view, 'Scheduled', is the workspace's scheduled tasks screen
  // (/w/:id/scheduled); it has no job of its own.
  const [tab, setTab] = useState(
    initialRouteRef.current.view === 'scheduled'
      ? 'Scheduled'
      : initialRouteRef.current.conversationId
        ? 'Task'
        : 'Home',
  )
  const [input, setInput] = useState('')
  // Manual choice only, no auto-detection. Defaults to Single Agent.
  const [mode, setMode] = useState('single')
  const [attachments, setAttachments] = useState([])
  const [uploadState, setUploadState] = useState({
    status: 'idle',
    filenames: [],
  })
  const [cancellingJobs, setCancellingJobs] = useState(new Set())
  const [hydrationState, setHydrationState] = useState('loading')
  const [errors, setErrors] = useState({
    hydration: null,
    submission: null,
    upload: null,
    cancellation: null,
    polling: {},
  })

  const [jobs, setJobs] = useState({})
  const [order, setOrder] = useState([])
  const [activeJobId, setActiveJobId] = useState(null)
  const [activeConversationId, setActiveConversationId] = useState(
    initialRouteRef.current.conversationId,
  )
  const [workers, setWorkers] = useState([])
  // Whether the agent roster is trustworthy yet. RightPanel needs this to
  // avoid captioning a fixed worker "Dynamically created agent" purely
  // because /workers had not loaded (or had failed) -- absence from an
  // unloaded roster proves nothing. Same pattern as workspacesState above.
  const [workersState, setWorkersState] = useState('loading')
  // Built-in Skill catalog + the current selection. The selection is
  // per-composer state, cleared on submit like the input itself: a skill
  // applies to the message you are sending, not to the conversation.
  const [skillCatalog, setSkillCatalog] = useState([])
  const [maxActiveSkills, setMaxActiveSkills] = useState(3)
  const [selectedSkillIds, setSelectedSkillIds] = useState([])
  // Opt-in only, never auto-detected (backend: RunPipelineRequest.deep_research).
  // Unlike the skill picks above it survives the send: someone who wants
  // the wide search usually wants it for the follow-ups too. It resets on a
  // new conversation (handleNewConversation), so the cost is never
  // inherited from whatever was open before.
  // Only runs on the Workforce path. The toggle is still offered in Single
  // Agent, where switching it on moves the message to Workforce
  // (handleDeepResearchChange); switching the mode back clears it again
  // (handleModeChange), so it never claims a search that will not run.
  const [deepResearch, setDeepResearch] = useState(false)
  const [agentModalOpen, setAgentModalOpen] = useState(false)
  // One concept -- "is the execution panel open" -- presented two ways: a
  // docked column at lg and up, a drawer below it. It starts open on a
  // desktop-width viewport (there is room for it) and closed on narrow
  // ones (where it would cover the conversation).
  const [rightPanelOpen, setRightPanelOpen] = useState(
    () => typeof window !== 'undefined' && window.innerWidth >= 1024,
  )
  // Left chat/workspace rail: open by default on md+ (room for it), closed
  // on narrow viewports where it is a drawer. Collapse sits on the sidebar
  // header; TopBar only shows a labeled reopen when the rail is hidden.
  const [sidebarOpen, setSidebarOpen] = useState(
    () => typeof window !== 'undefined' && window.innerWidth >= 768,
  )
  const [savingAgent, setSavingAgent] = useState(false)
  const [agentError, setAgentError] = useState(null)

  // Navigation context now comes from the URL (see readRoute): a load with
  // a /w/... path restores that workspace/conversation, and a bare "/"
  // still begins at Workspace Selection.
  const [workspaceId, setWorkspaceIdState] = useState(
    initialRouteRef.current.workspaceId,
  )
  const [workspaces, setWorkspaces] = useState([])
  // Tracked so route restoration can wait for the workspace list to settle.
  // Without this the reconcile step could run against an empty array and
  // "correct" a perfectly valid URL back to the picker -- the exact false
  // redirect this feature has to avoid.
  const [workspacesState, setWorkspacesState] = useState('loading')
  // WorkspacePicker chooses the persistent container. `creatingWorkspace`/
  // `workspaceCreateError` are its own small piece of state because the
  // picker's "New workspace" form needs its own pending/error UI,
  // independent of task submission's `errors.submission`.
  const [pickerOpen, setPickerOpen] = useState(
    !initialRouteRef.current.workspaceId,
  )
  const [creatingWorkspace, setCreatingWorkspace] = useState(false)
  // New scheduled results not yet opened, for the sidebar badge; and a draft
  // handed from a finished chat ("Repeat on a schedule") to the editor.
  const [scheduledUnseen, setScheduledUnseen] = useState(0)
  const [scheduleSeed, setScheduleSeed] = useState(null)
  const [workspaceCreateError, setWorkspaceCreateError] = useState(null)

  const uploadSequenceRef = useRef(0)
  const uploadControllerRef = useRef(null)

  const activeJob = activeJobId ? jobs[activeJobId] : null
  // Every job that shares the selected conversation_id, oldest first.
  const threadJobs = useMemo(() => {
    const inThisConversation = order
      .map((id) => jobs[id])
      .filter(
        (job) =>
          job &&
          activeConversationId &&
          (job.conversation_id || job.workspace_id || job.job_id) ===
            activeConversationId,
      )
    if (inThisConversation.length > 0) {
      return inThisConversation.sort(
        (left, right) => (left.created_at || 0) - (right.created_at || 0),
      )
    }
    return activeJob ? [activeJob] : []
  }, [jobs, order, activeConversationId, activeJob])
  const latestThreadJob =
    threadJobs.length > 0 ? threadJobs[threadJobs.length - 1] : null

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false

    async function hydrate() {
      setHydrationState('loading')
      setErrors((previous) => ({ ...previous, hydration: null }))
      try {
        const response = await getJobs({ signal: controller.signal })
        if (disposed) return
        if (!Array.isArray(response)) {
          throw new Error('The server returned an invalid job list.')
        }

        const sorted = response
          .filter((job) => job?.job_id)
          .sort((left, right) => (right.created_at || 0) - (left.created_at || 0))
        const hydratedJobs = Object.fromEntries(
          sorted.map((job) => [job.job_id, job]),
        )
        const hydratedOrder = sorted.map((job) => job.job_id)

        setJobs((previous) => ({ ...hydratedJobs, ...previous }))
        setOrder((previous) => [
          ...previous,
          ...hydratedOrder.filter((id) => !previous.includes(id)),
        ])

        // Hydration deliberately does not open the newest conversation.
        // A stored Workspace lands on its home state; otherwise the picker
        // lets the user choose the Workspace context explicitly.
        setHydrationState('ready')
      } catch (error) {
        if (disposed || error?.name === 'AbortError') return
        setHydrationState('error')
        setErrors((previous) => ({
          ...previous,
          hydration: userSafeMessage(
            error,
            'Unable to load recent jobs. You can still create a new task.',
          ),
        }))
      }
    }

    hydrate()
    return () => {
      disposed = true
      controller.abort()
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    getWorkers({ signal: controller.signal })
      .then((data) => {
        if (Array.isArray(data?.workers)) {
          setWorkers(data.workers)
          setWorkersState('ready')
        } else {
          setWorkersState('error')
        }
      })
      .catch((error) => {
        // Non-fatal: the Agent Pool just stays empty until the next add.
        // Recorded as 'error' rather than left at 'loading' so the panel
        // knows the roster is unverified and declines to infer anything
        // from a name's absence.
        if (error?.name !== 'AbortError') setWorkersState('error')
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    getSkills({ signal: controller.signal })
      .then((data) => {
        if (Array.isArray(data?.skills)) {
          setSkillCatalog(data.skills)
          if (Number.isFinite(data.max_active)) setMaxActiveSkills(data.max_active)
        }
      })
      // Non-fatal by design: SkillPicker renders nothing on an empty
      // catalog, so a failed fetch costs the Skills control and nothing
      // else -- submitting, routing and execution are unaffected.
      .catch(() => {})
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    getWorkspaces({ signal: controller.signal })
      .then((data) => {
        if (Array.isArray(data?.workspaces)) {
          setWorkspaces(data.workspaces)
          setWorkspacesState('ready')
        } else {
          setWorkspacesState('error')
        }
      })
      .catch((error) => {
        // Non-fatal: the workspace picker remains empty until a retry.
        // Recorded as 'error' rather than 'ready' so route restoration
        // knows the list is unverified and leaves the URL alone instead of
        // deciding the workspace does not exist.
        if (error?.name !== 'AbortError') setWorkspacesState('error')
      })
    return () => controller.abort()
    // Validation of the current workspace against this list moved into the
    // route-reconciliation effect below, which knows whether the list is
    // trustworthy yet.
  }, [])

  // Latest jobs, readable from listeners registered once. popstate has to
  // resolve a conversation id against whatever has loaded by the time the
  // user presses Back, not whatever was loaded when the listener attached.
  const jobsRef = useRef({ jobs, order })
  jobsRef.current = { jobs, order }

  const newestJobInConversation = useCallback((conversationId, source) => {
    const { jobs: currentJobs, order: currentOrder } =
      source || jobsRef.current
    const matches = currentOrder
      .map((id) => currentJobs[id])
      .filter((job) => job && conversationIdOf(job) === conversationId)
    if (matches.length === 0) return null
    return matches.reduce((newest, job) =>
      (job.created_at || 0) > (newest.created_at || 0) ? job : newest,
    )
  }, [])

  // Restore the URL's workspace/conversation exactly once, and only after
  // both async sources have settled. Running earlier would compare the
  // route against an empty list and bounce a valid deep link back to the
  // picker; the gate is that data arriving, never a timer.
  // State, not a ref: the URL-sync effect below has to re-run at the moment
  // this flips. If it were a ref, a user who clicked a workspace while the
  // job list was still loading would change navigation state in a commit
  // where sync was still gated off, and sync would never fire again --
  // leaving the address bar stuck on "/" while the app showed a workspace.
  const [routeApplied, setRouteApplied] = useState(false)
  useEffect(() => {
    if (routeApplied) return
    if (hydrationState === 'loading' || workspacesState === 'loading') return

    const route = initialRouteRef.current
    setRouteApplied(true)
    if (!route.workspaceId) {
      // Bare "/" used to land on the workspace index. Open the most recent
      // space's cowork home instead; the index stays available from the
      // sidebar / space pill.
      if (workspacesState === 'ready' && workspaces.length > 0) {
        const preferred = [...workspaces].sort(
          (left, right) =>
            (right.last_active_at || 0) - (left.last_active_at || 0),
        )[0]
        setWorkspaceIdState(preferred.workspace_id)
        setPickerOpen(false)
        setTab('Home')
      }
      return
    }

    // "Not in the list" is only meaningful when the list actually loaded.
    // If the workspaces request failed we cannot prove the workspace is
    // gone, so the URL stands rather than being silently discarded.
    if (
      workspacesState === 'ready' &&
      !workspaces.some(
        (workspace) => workspace.workspace_id === route.workspaceId,
      )
    ) {
      setWorkspaceIdState(null)
      setActiveConversationId(null)
      setPickerOpen(true)
      setTab('Home')
      window.history.replaceState({}, '', '/')
      return
    }

    if (!route.conversationId) return
    const newest = newestJobInConversation(route.conversationId)
    if (newest) {
      setActiveJobId(newest.job_id)
      setTab('Task')
      return
    }
    // Same rule for conversations: only drop it from the URL once the job
    // list is known-good and genuinely does not contain it.
    if (hydrationState === 'ready') {
      setActiveConversationId(null)
      setTab('Home')
      window.history.replaceState(
        {},
        '',
        routePath(route.workspaceId, null),
      )
    }
  }, [
    routeApplied,
    hydrationState,
    workspacesState,
    workspaces,
    jobs,
    order,
    newestJobInConversation,
  ])

  // Mirror navigation state into the address bar. Guarded on the current
  // pathname so a popstate-driven state update does not push a duplicate
  // entry for the URL the user just navigated to.
  useEffect(() => {
    if (!routeApplied) return
    const next = pickerOpen
      ? '/'
      : routePath(
          workspaceId,
          activeConversationId,
          tab === 'Scheduled' ? 'scheduled' : null,
        )
    if (window.location.pathname !== next) {
      window.history.pushState({}, '', next)
    }
  }, [routeApplied, pickerOpen, workspaceId, activeConversationId, tab])

  useEffect(() => {
    const onPopState = () => {
      const route = readRoute()
      setPickerOpen(!route.workspaceId)
      setWorkspaceIdState(route.workspaceId)
      setActiveConversationId(route.conversationId)
      const newest = route.conversationId
        ? newestJobInConversation(route.conversationId)
        : null
      setActiveJobId(newest?.job_id ?? null)
      setTab(
        route.view === 'scheduled' && route.workspaceId
          ? 'Scheduled'
          : newest
            ? 'Task'
            : 'Home',
      )
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [newestJobInConversation])

  const setWorkspaceId = useCallback((id) => {
    setWorkspaceIdState(id || null)
  }, [])

  // Creating a Workspace lands on its empty Workspace home. A Conversation
  // is created only when the user submits from the existing composer.
  const handleCreateWorkspace = useCallback(async (name) => {
    setCreatingWorkspace(true)
    setWorkspaceCreateError(null)
    try {
      const created = await createWorkspace({ name: name?.trim() || null })
      setWorkspaces((previous) => [created, ...previous])
      setWorkspaceId(created.workspace_id)
      setActiveJobId(null)
      setActiveConversationId(null)
      setPickerOpen(false)
      setTab('Home')
    } catch (error) {
      setWorkspaceCreateError(
        userSafeMessage(
          error,
          'Unable to create a new workspace. Please try again.',
        ),
      )
    } finally {
      setCreatingWorkspace(false)
    }
  }, [setWorkspaceId])

  const handleDeleteWorkspace = useCallback(
    async (workspaceIdToDelete) => {
      await deleteWorkspace(workspaceIdToDelete)
      setWorkspaces((previous) =>
        previous.filter(
          (workspace) => workspace.workspace_id !== workspaceIdToDelete,
        ),
      )
      setJobs((previous) => {
        const next = { ...previous }
        for (const jobId of Object.keys(next)) {
          if (next[jobId]?.workspace_id === workspaceIdToDelete) {
            delete next[jobId]
          }
        }
        return next
      })
      if (workspaceId === workspaceIdToDelete) {
        setWorkspaceId(null)
        setActiveJobId(null)
        setActiveConversationId(null)
        setPickerOpen(true)
        setTab('Home')
      }
    },
    [setWorkspaceId, workspaceId],
  )

  const handleRenameWorkspace = useCallback(async (workspaceIdToRename, name) => {
    const updated = await updateWorkspace(workspaceIdToRename, { name })
    setWorkspaces((previous) =>
      previous.map((workspace) =>
        workspace.workspace_id === workspaceIdToRename ? updated : workspace,
      ),
    )
  }, [])


  const handleSaveAgent = useCallback(async ({ name, description }) => {
    setSavingAgent(true)
    setAgentError(null)
    try {
      const data = await addWorker({ name, description })
      // The add response is the full, authoritative roster -- so a session
      // whose initial /workers fetch failed becomes trustworthy again here.
      if (Array.isArray(data?.workers)) {
        setWorkers(data.workers)
        setWorkersState('ready')
      }
      setAgentModalOpen(false)
    } catch (error) {
      setAgentError(
        userSafeMessage(error, 'Unable to add this agent. Please try again.'),
      )
    } finally {
      setSavingAgent(false)
    }
  }, [])

  const handleCloseAgentModal = useCallback(() => {
    setAgentModalOpen(false)
    setAgentError(null)
  }, [])

  const activePollingIds = useMemo(
    () =>
      order.filter((jobId) => {
        const job = jobs[jobId]
        return job && isActiveJob(job) && !job._optimistic
      }),
    [jobs, order],
  )
  const activePollingKey = activePollingIds.join('|')

  useEffect(() => {
    if (!activePollingKey) return

    const sources = activePollingIds.map((jobId) => {
      const source = new EventSource(`/api/jobs/${jobId}/events`)

      // The backend streams one SSE message per ~4 characters of the
      // answer (see main.py's typing-effect replay), so committing every
      // message straight to React state re-rendered the whole chat tree
      // and re-parsed the accumulated markdown from scratch dozens of
      // times a second -- work that grows with the answer's length, so a
      // long answer visibly froze the UI toward the end of streaming.
      // Coalescing to one commit per animation frame keeps the typing
      // effect looking identical (well above perceptible smoothness) while
      // cutting render frequency by an order of magnitude or more.
      let latestFresh = null
      let flushHandle = null
      const flush = () => {
        flushHandle = null
        if (latestFresh) setJobs((previous) => ({ ...previous, [latestFresh.job_id]: latestFresh }))
      }

      source.onmessage = (event) => {
        let fresh
        try {
          fresh = JSON.parse(event.data)
        } catch {
          return
        }
        latestFresh = fresh
        if (flushHandle == null) flushHandle = requestAnimationFrame(flush)
        setErrors((previous) => ({
          ...previous,
          polling: withoutKey(previous.polling, jobId),
        }))
        if (!isActiveJob(fresh)) source.close()
      }

      source.onerror = () => {
        // Backend restart closes the stream. The job is still "running" in
        // local state, so Stop stays visible; fetch the persisted snapshot
        // so an interrupted/cancelled row replaces the live one.
        getJob(jobId)
          .then((fresh) => {
            setJobs((previous) => ({ ...previous, [fresh.job_id]: fresh }))
            setErrors((previous) => ({
              ...previous,
              polling: withoutKey(previous.polling, jobId),
            }))
            if (!isActiveJob(fresh)) source.close()
          })
          .catch(() => {
            setErrors((previous) => ({
              ...previous,
              polling: {
                ...previous.polling,
                [jobId]:
                  'Live updates are temporarily unavailable for this job. Refresh the page to retry.',
              },
            }))
          })
      }

      return { source, cancelFlush: () => flushHandle != null && cancelAnimationFrame(flushHandle) }
    })

    return () => {
      sources.forEach(({ source, cancelFlush }) => {
        cancelFlush()
        source.close()
      })
    }
  }, [activePollingKey])

  // `options` is optional and only ever supplied by handleSwitchToWorkforce.
  // Composer passes the click event here (onClick={onSubmit}), so the two
  // fields are read defensively rather than spread -- an event has neither.
  const handleSubmit = useCallback(async (options) => {
    const overrideTopic =
      typeof options?.topicOverride === 'string' ? options.topicOverride : ''
    const overrideMode =
      options?.modeOverride === 'multi' || options?.modeOverride === 'single'
        ? options.modeOverride
        : ''
    const topic = (overrideTopic || input).trim()
    const startsConversation = !activeConversationId
    if (!topic || uploadState.status === 'uploading') {
      return
    }
    if (latestThreadJob?._optimistic) {
      return
    }

    const pendingJobId = `pending-${crypto.randomUUID()}`
    const priorConversationId = activeConversationId
    const conversationId =
      priorConversationId || `pending-conv-${crypto.randomUUID()}`
    const capturedAttachments = [...attachments]
    const capturedMode = overrideMode || mode
    const capturedSkills = [...selectedSkillIds]
    const capturedDeepResearch = capturedMode === 'multi' && deepResearch

    const optimisticStub = createJobStub({
      job_id: pendingJobId,
      workspace_id: workspaceId,
      conversation_id: conversationId,
      topic,
      mode: capturedMode,
      deepResearch: capturedDeepResearch,
      attachments: capturedAttachments,
      optimistic: true,
    })

    // Commit the optimistic exchange synchronously so the user's message
    // leaves the composer and appears in the thread before the POST returns.
    flushSync(() => {
      setJobs((previous) => ({ ...previous, [pendingJobId]: optimisticStub }))
      setOrder((previous) => [
        pendingJobId,
        ...previous.filter((id) => id !== pendingJobId),
      ])
      setActiveJobId(pendingJobId)
      setActiveConversationId(conversationId)
      setTab('Task')
      setInput('')
      setAttachments([])
      setSelectedSkillIds([])
      // Deep research deliberately survives the send, unlike the input and
      // the skill picks above. Someone who wants the wide search usually
      // wants it for the follow-ups too, and re-arming it every message was
      // the complaint. It was cleared to stop an accidental ten-minute run;
      // the lit "Deep" pill and the per-job badge now make it visible
      // instead. Mode changes still clear it (handleModeChange).
      setUploadState({ status: 'idle', filenames: [] })
      setErrors((previous) => ({ ...previous, submission: null }))
    })

    try {
      const { job_id, workspace_id, conversation_id } = await startPipeline({
        topic,
        fileIds: capturedAttachments.map((attachment) => attachment.file_id),
        mode: capturedMode,
        workspaceId,
        conversationId: priorConversationId,
        skillIds: capturedSkills,
        deepResearch: capturedDeepResearch,
      })
      if (workspace_id && workspace_id !== workspaceId) {
        setWorkspaceId(workspace_id)
        setWorkspaces((previous) =>
          previous.some((item) => item.workspace_id === workspace_id)
            ? previous
            : [
                {
                  workspace_id,
                  name: null,
                  created_at: Date.now() / 1000,
                  last_active_at: Date.now() / 1000,
                },
                ...previous,
              ],
        )
      }
      const stub = createJobStub({
        job_id,
        workspace_id,
        conversation_id,
        topic,
        mode: capturedMode,
        attachments: capturedAttachments,
      })
      setJobs((previous) => {
        const { [pendingJobId]: _removed, ...rest } = previous
        return { ...rest, [job_id]: stub }
      })
      setOrder((previous) =>
        previous.map((id) => (id === pendingJobId ? job_id : id)),
      )
      setActiveJobId(job_id)
      setActiveConversationId(conversation_id)
    } catch (error) {
      setJobs((previous) => {
        const { [pendingJobId]: _removed, ...rest } = previous
        return rest
      })
      setOrder((previous) => previous.filter((id) => id !== pendingJobId))
      setActiveJobId((current) => (current === pendingJobId ? null : current))
      if (startsConversation) {
        setActiveConversationId(null)
        setTab('Home')
      }
      setInput(topic)
      setAttachments(capturedAttachments)
      setSelectedSkillIds(capturedSkills)
      // No setDeepResearch here: the send no longer clears it, so there is
      // nothing to put back, and restoring capturedDeepResearch would turn
      // the toggle off after a failed send from Single Agent.
      setErrors((previous) => ({
        ...previous,
        submission: userSafeMessage(
          error,
          'Unable to start the task. Please try again.',
        ),
      }))
    }
  }, [
    attachments,
    input,
    mode,
    uploadState.status,
    workspaceId,
    activeConversationId,
    selectedSkillIds,
    deepResearch,
    latestThreadJob,
    setWorkspaceId,
  ])

  const handleAttach = useCallback(async (selectedFiles) => {
    const files = Array.from(selectedFiles || [])
    if (files.length === 0) return
    if (attachments.length + files.length > MAX_UPLOAD_SOURCES) {
      setErrors((previous) => ({
        ...previous,
        upload:
          `A job can include at most ${MAX_UPLOAD_SOURCES} uploaded sources.`,
      }))
      return
    }

    const unsupported = files.find((file) => {
      const extension = file.name.split('.').pop()?.toLowerCase()
      return !extension || !SUPPORTED_EXTENSIONS.has(extension)
    })
    if (unsupported) {
      setErrors((previous) => ({
        ...previous,
        upload:
          `${unsupported.name} is unsupported. Choose one of: ` +
          `${SUPPORTED_EXTENSIONS_LABEL}.`,
      }))
      return
    }
    const oversized = files.find((file) => file.size > MAX_UPLOAD_BYTES)
    if (oversized) {
      setErrors((previous) => ({
        ...previous,
        upload: `${oversized.name} is larger than the 20 MB upload limit.`,
      }))
      return
    }
    const existingBytes = attachments.reduce(
      (total, attachment) => total + (attachment.size_bytes || 0),
      0,
    )
    const selectedBytes = files.reduce((total, file) => total + file.size, 0)
    if (existingBytes + selectedBytes > MAX_BATCH_UPLOAD_BYTES) {
      setErrors((previous) => ({
        ...previous,
        upload: 'Combined attachments exceed the 50 MB job limit.',
      }))
      return
    }

    const sequence = uploadSequenceRef.current + 1
    uploadSequenceRef.current = sequence
    uploadControllerRef.current?.abort()
    const controller = new AbortController()
    uploadControllerRef.current = controller

    setUploadState({
      status: 'uploading',
      filenames: files.map((file) => file.name),
    })
    setErrors((previous) => ({ ...previous, upload: null }))

    try {
      const data = await uploadFiles(files, { signal: controller.signal })
      if (
        controller.signal.aborted ||
        uploadSequenceRef.current !== sequence
      ) {
        return
      }
      if (!Array.isArray(data)) {
        throw new Error('The server returned an invalid upload response.')
      }
      setAttachments((previous) => [...previous, ...data])
      setUploadState({ status: 'idle', filenames: [] })
    } catch (error) {
      if (
        error?.name === 'AbortError' ||
        uploadSequenceRef.current !== sequence
      ) {
        return
      }
      setUploadState({ status: 'idle', filenames: [] })
      setErrors((previous) => ({
        ...previous,
        upload: userSafeMessage(
          error,
          'Unable to upload this file. Please try again.',
        ),
      }))
    }
  }, [attachments])

  const handleClearAttachment = useCallback((fileId) => {
    if (fileId) {
      setAttachments((previous) =>
        previous.filter((attachment) => attachment.file_id !== fileId),
      )
      return
    }
    uploadSequenceRef.current += 1
    uploadControllerRef.current?.abort()
    uploadControllerRef.current = null
    setAttachments([])
    setUploadState({ status: 'idle', filenames: [] })
    setErrors((previous) => ({ ...previous, upload: null }))
  }, [])

  const handleCancelUpload = useCallback(() => {
    uploadSequenceRef.current += 1
    uploadControllerRef.current?.abort()
    uploadControllerRef.current = null
    setUploadState({ status: 'idle', filenames: [] })
    setErrors((previous) => ({ ...previous, upload: null }))
  }, [])

  useEffect(
    () => () => {
      uploadControllerRef.current?.abort()
    },
    [],
  )

  const handleSelectJob = useCallback(
    (jobId) => {
      const job = jobs[jobId]
      if (!job) return
      setActiveJobId(jobId)
      if (job.workspace_id) setWorkspaceId(job.workspace_id)
      setActiveConversationId(
        job.conversation_id || job.workspace_id || job.job_id,
      )
      setPickerOpen(false)
      setTab(tabForJob(job))
    },
    [jobs, setWorkspaceId],
  )

  const handleSelectConversation = useCallback(
    (conversation) => handleSelectJob(conversation.latestJobId),
    [handleSelectJob],
  )

  const handlePickWorkspace = useCallback(
    (id) => {
      setPickerOpen(false)
      setWorkspaceId(id)
      setActiveJobId(null)
      setActiveConversationId(null)
      setTab('Home')
    },
    [setWorkspaceId],
  )

  const handleNewConversation = useCallback(() => {
    setActiveJobId(null)
    setActiveConversationId(null)
    setInput('')
    // Deep research sticks across follow-ups inside a conversation, but a
    // new one starts clean: an expensive wide search should be a choice
    // made here, not inherited from whatever was open before.
    setDeepResearch(false)
    handleClearAttachment()
    setErrors((previous) => ({
      ...previous,
      submission: null,
      cancellation: null,
    }))
    setWorkspaceCreateError(null)
    if (workspaceId) {
      setPickerOpen(false)
      setTab('Home')
      return
    }
    const preferred = [...workspaces].sort(
      (left, right) =>
        (right.last_active_at || 0) - (left.last_active_at || 0),
    )[0]
    if (preferred) {
      setWorkspaceId(preferred.workspace_id)
      setPickerOpen(false)
      setTab('Home')
      return
    }
    setPickerOpen(true)
    setTab('Home')
  }, [handleClearAttachment, setWorkspaceId, workspaceId, workspaces])

  // Scheduled runs create jobs on the server with nobody watching, so the
  // job list loaded at startup never contains them. Merge the server's list
  // in (live local entries win, as in hydrate) whenever that can have changed.
  const refreshJobs = useCallback(async () => {
    try {
      const response = await getJobs()
      if (!Array.isArray(response)) return
      const fresh = response.filter((job) => job?.job_id)
      setJobs((previous) => ({
        ...Object.fromEntries(fresh.map((job) => [job.job_id, job])),
        ...previous,
      }))
      setOrder((previous) => [
        ...previous,
        ...fresh
          .sort((left, right) => (right.created_at || 0) - (left.created_at || 0))
          .map((job) => job.job_id)
          .filter((id) => !previous.includes(id)),
      ])
    } catch {
      // Non-fatal: the next poll tries again.
    }
  }, [])

  // Badge count for the sidebar, and the signal that a scheduled run has
  // started or finished somewhere (its task's last_job_id/last_status moved).
  const scheduledSignatureRef = useRef(null)
  useEffect(() => {
    if (!workspaceId) {
      setScheduledUnseen(0)
      return undefined
    }
    let disposed = false
    const poll = async () => {
      try {
        const data = await getScheduledTasks(workspaceId)
        if (disposed) return
        setScheduledUnseen(data.unseen_runs || 0)
        const signature = (data.tasks || [])
          .map((task) => `${task.task_id}:${task.last_job_id}:${task.last_status}`)
          .join('|')
        if (scheduledSignatureRef.current !== null && scheduledSignatureRef.current !== signature) {
          refreshJobs()
        }
        scheduledSignatureRef.current = signature
      } catch {
        // Non-fatal: the badge just keeps its last value.
      }
    }
    scheduledSignatureRef.current = null
    poll()
    const timer = window.setInterval(poll, 30000)
    return () => {
      disposed = true
      window.clearInterval(timer)
    }
  }, [workspaceId, refreshJobs])

  const handleOpenScheduled = useCallback(() => {
    if (!workspaceId) {
      setPickerOpen(true)
      setTab('Home')
      return
    }
    setPickerOpen(false)
    setActiveJobId(null)
    setActiveConversationId(null)
    setTab('Scheduled')
  }, [workspaceId])

  const handleOpenScheduledRun = useCallback(
    async (run) => {
      if (!run?.job_id) return
      try {
        const job = await getJob(run.job_id)
        setJobs((previous) => ({ ...previous, [job.job_id]: previous[job.job_id] || job }))
        setOrder((previous) =>
          previous.includes(job.job_id) ? previous : [job.job_id, ...previous],
        )
        setActiveJobId(job.job_id)
        if (job.workspace_id) setWorkspaceId(job.workspace_id)
        setActiveConversationId(conversationIdOf(job))
        setPickerOpen(false)
        setTab('Task')
        refreshJobs()
      } catch (error) {
        setErrors((previous) => ({
          ...previous,
          submission: userSafeMessage(error, 'Unable to open that run.'),
        }))
      }
    },
    [refreshJobs, setWorkspaceId],
  )

  const handleRepeatOnSchedule = useCallback(
    (job) => {
      const request = String(job?.usage?.resolved_request || job?.topic || '').trim()
      const name = String(job?.topic || '').trim()
      setScheduleSeed(
        newTaskDraft({
          name: name.length > 60 ? `${name.slice(0, 57)}…` : name,
          prompt: request,
          mode: job?.mode || 'multi',
          deepResearch: Boolean(job?.deep_research),
          skillIds: (job?.skills || []).map((skill) => skill.skill_id).filter(Boolean),
        }),
      )
      handleOpenScheduled()
    },
    [handleOpenScheduled],
  )

  const handleOpenWorkspacePicker = useCallback(() => {
    setActiveJobId(null)
    setActiveConversationId(null)
    setRightPanelOpen(false)
    setInput('')
    handleClearAttachment()
    setTab('Home')
    setPickerOpen(true)
    setWorkspaceCreateError(null)
  }, [handleClearAttachment])

  const handleCancelJob = useCallback(async (jobId) => {
    if (!jobId) return
    const existing = jobs[jobId]
    if (existing?._optimistic) {
      setJobs((previous) => {
        const { [jobId]: _removed, ...rest } = previous
        return rest
      })
      setOrder((previous) => previous.filter((id) => id !== jobId))
      setActiveJobId((current) => (current === jobId ? null : current))
      return
    }
    setCancellingJobs((previous) => new Set(previous).add(jobId))
    setErrors((previous) => ({ ...previous, cancellation: null }))
    try {
      const cancelled = await cancelJob(jobId)
      setJobs((previous) => ({
        ...previous,
        [cancelled.job_id]: cancelled,
      }))
      if (activeJobId === jobId) setTab('Task')
    } catch (error) {
      setErrors((previous) => ({
        ...previous,
        cancellation: userSafeMessage(
          error,
          'Unable to cancel this job. Please try again.',
        ),
      }))
    } finally {
      setCancellingJobs((previous) => {
        const next = new Set(previous)
        next.delete(jobId)
        return next
      })
    }
  }, [activeJobId, jobs])

  const recent = order.map((id) => jobs[id]).filter(Boolean)
  // One row per persisted conversation. Jobs are messages/runs within that
  // conversation; Workspace is only the container used for filtering.
  const conversationList = useMemo(() => {
    const groups = new Map()
    for (const job of recent) {
      const conversationId =
        job.conversation_id || job.workspace_id || job.job_id
      const timestamp = job.created_at || 0
      const existing = groups.get(conversationId)
      if (!existing) {
        groups.set(conversationId, {
          conversationId,
          workspaceId: job.workspace_id || null,
          title: job.topic,
          preview: null,
          status: job.status,
          mode: job.mode,
          modeLabel: executionLabelOf(job),
          firstActiveAt: timestamp,
          lastActiveAt: timestamp,
          latestJobId: job.job_id,
          latestTopic: job.topic,
        })
        continue
      }
      if (timestamp < existing.firstActiveAt) {
        existing.firstActiveAt = timestamp
        existing.title = job.topic
        existing.mode = job.mode
        existing.modeLabel = executionLabelOf(job)
      }
      if (timestamp > existing.lastActiveAt) {
        existing.lastActiveAt = timestamp
        existing.latestJobId = job.job_id
        existing.latestTopic = job.topic
        existing.status = job.status
      }
    }
    for (const entry of groups.values()) {
      entry.preview =
        entry.latestTopic && entry.latestTopic !== entry.title
          ? entry.latestTopic
          : null
    }
    return [...groups.values()].sort(
      (left, right) => right.lastActiveAt - left.lastActiveAt,
    )
  }, [recent])
  const workspaceConversations = useMemo(
    () =>
      conversationList.filter(
        (conversation) => conversation.workspaceId === workspaceId,
      ),
    [conversationList, workspaceId],
  )
  const conversationCounts = useMemo(() => {
    const counts = new Map()
    for (const conversation of conversationList) {
      if (!conversation.workspaceId) continue
      counts.set(
        conversation.workspaceId,
        (counts.get(conversation.workspaceId) || 0) + 1,
      )
    }
    return counts
  }, [conversationList])
  const activeWorkspace = useMemo(
    () =>
      workspaces.find((workspace) => workspace.workspace_id === workspaceId) ||
      null,
    [workspaces, workspaceId],
  )
  // The backend picks the model from MODEL_NAME; the most recent job that
  // reported one is the only place the frontend can learn it.
  const modelName = useMemo(
    () =>
      recent.find((job) => job?.usage?.model_name || job?.model_name)?.usage
        ?.model_name ||
      recent.find((job) => job?.model_name)?.model_name ||
      null,
    [recent],
  )
  // Lock mode while a real job is streaming (not during the brief optimistic
  // POST gap -- that stub has no server-side run yet).
  const isOptimisticPending = Boolean(latestThreadJob?._optimistic)
  const isStreamingActiveJob =
    isActiveJob(latestThreadJob) && !latestThreadJob?._optimistic
  const modeLocked = isStreamingActiveJob
  // The *permanent* lock: once a thread has sent its first message, that
  // message's mode is the thread's mode for good -- Workforce is never
  // offered again in a thread that started Single Agent (and vice versa),
  // matching RightPanel's existing "job's own mode always wins" rule
  // instead of just disabling the option temporarily. Sync App's `mode`
  // state to it so the composer always reflects the thread's real mode,
  // even if the user had a different selection queued up on Home before
  // switching threads.
  // One-way escape hatch from the permanent lock below. A Single thread
  // stays Single unless the user takes the offer the system makes when the
  // ask needed Workforce (job.suggests_workforce) -- the lock's purpose is
  // to stop arbitrary mid-thread switching, not to trap a conversation in
  // a mode that cannot answer it. Nothing in execution assumes one mode per
  // thread: every job records and runs its own (main._run_job's
  // execution_strategy), and RightPanel already renders per job.
  const [threadModeUnlocked, setThreadModeUnlocked] = useState(false)
  useEffect(() => {
    setThreadModeUnlocked(false)
  }, [activeConversationId])

  const isThreadModeLocked = threadJobs.length > 0 && !threadModeUnlocked
  const threadLockedMode = isThreadModeLocked ? threadJobs[0].mode : null
  useEffect(() => {
    if (threadLockedMode && threadLockedMode !== mode) {
      setMode(threadLockedMode)
      // Opening a thread locked to Single Agent must clear a toggle queued
      // on Home, for the same reason handleModeChange does.
      if (threadLockedMode !== 'multi') setDeepResearch(false)
    }
  }, [threadLockedMode])

  // Deep research only runs on the workforce path (handleSubmit drops it
  // unless mode is 'multi'), so in Single Agent the toggle lit up and then
  // did nothing. Turning it on switches the mode with it. A thread that
  // already started in Single keeps its permanent lock — there the toggle
  // is hidden rather than silently ignored (see Composer).
  // Taking the offer unlocks the thread and arms Workforce for the next
  // message. It does not resend anything: the user still edits and sends,
  // so an expensive run is never started on their behalf.
  const handleSwitchToWorkforce = useCallback(
    (topic) => {
      setThreadModeUnlocked(true)
      setMode('multi')
      const ask = String(topic || '').trim()
      if (!ask) return
      // Sent with explicit overrides rather than relying on the state set
      // above: React has not re-rendered yet, so `mode` still reads
      // 'single' inside this tick and the run would go to the wrong path.
      void handleSubmit({ topicOverride: ask, modeOverride: 'multi' })
    },
    [handleSubmit],
  )

  const deepResearchAvailable = !isThreadModeLocked || threadLockedMode === 'multi'
  const handleDeepResearchChange = useCallback(
    (next) => {
      setDeepResearch(next)
      if (next && !isThreadModeLocked) setMode('multi')
    },
    [isThreadModeLocked],
  )
  // The other half of that rule. Without it, switching back to Single Agent
  // left the toggle lit while handleSubmit dropped the flag (it only sends
  // deep research on the workforce path) -- the button then claimed a wide
  // search that was never going to run.
  const handleModeChange = useCallback((next) => {
    setMode(next)
    if (next !== 'multi') setDeepResearch(false)
  }, [])

  const visibleError =
    errors.upload ||
    errors.submission ||
    errors.cancellation ||
    (activeJobId ? errors.polling[activeJobId] : null) ||
    errors.hydration

  return (
    <TooltipProvider delayDuration={300} skipDelayDuration={200}>
      <div className="flex h-full overflow-hidden">
        {sidebarOpen && (
          <div
            className="fixed inset-0 z-30 bg-black/60 backdrop-blur-[2px] md:hidden"
            onClick={() => setSidebarOpen(false)}
          />
        )}
        <Sidebar
          conversations={workspaceConversations}
          activeWorkspaceName={
            workspaceId ? activeWorkspace?.name || 'Untitled workspace' : null
          }
          activeConversationId={activeConversationId}
          loading={hydrationState === 'loading'}
          open={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
          onSelectConversation={(conversation) => {
            // Drawer only: on md+ the rail stays open until the TopBar toggle.
            if (typeof window !== 'undefined' && window.innerWidth < 768) {
              setSidebarOpen(false)
            }
            handleSelectConversation(conversation)
          }}
          onNew={() => {
            if (typeof window !== 'undefined' && window.innerWidth < 768) {
              setSidebarOpen(false)
            }
            handleNewConversation()
          }}
          workspacesSelected={pickerOpen}
          scheduledSelected={!pickerOpen && tab === 'Scheduled'}
          scheduledUnseen={scheduledUnseen}
          onOpenScheduled={() => {
            if (typeof window !== 'undefined' && window.innerWidth < 768) {
              setSidebarOpen(false)
            }
            handleOpenScheduled()
          }}
          onGoHome={() => {
            if (typeof window !== 'undefined' && window.innerWidth < 768) {
              setSidebarOpen(false)
            }
            handleOpenWorkspacePicker()
          }}
          onDeleteConversation={async (conversation) => {
            const id = conversation.conversationId
            if (!id) return
            try {
              await deleteConversation(id)
            } catch (error) {
              // Fallback rows may be keyed by workspace/job id with no
              // conversations row; still clear them from the sidebar.
              if (error?.status !== 404) throw error
            }
            setJobs((previous) => {
              const next = { ...previous }
              for (const jid of Object.keys(next)) {
                if (conversationIdOf(next[jid]) === id) delete next[jid]
              }
              return next
            })
            if (activeConversationId === id) {
              setActiveJobId(null)
              setActiveConversationId(null)
              setTab('Home')
            }
          }}
        />

        <div className="app-ground flex min-h-0 min-w-0 flex-1 flex-col">
          {!pickerOpen && tab === 'Task' && (
            <TopBar
              workspaceName={activeWorkspace?.name || 'Untitled workspace'}
              title={threadJobs.length > 0 ? threadJobs[0].topic : null}
              status={latestThreadJob?.status}
              onToggleNav={() => setSidebarOpen((open) => !open)}
              navOpen={sidebarOpen}
              onNewConversation={handleNewConversation}
              onTogglePanel={() => setRightPanelOpen((open) => !open)}
              panelOpen={rightPanelOpen}
              showPanelToggle={tab === 'Task'}
            />
          )}

          <div className="flex min-h-0 flex-1">
            {pickerOpen ? (
              <main className="flex min-h-0 min-w-0 flex-1 flex-col" id="workspace-panel">
                <WorkspacePicker
                  workspaces={workspaces}
                  conversationCounts={conversationCounts}
                  loading={
                    workspacesState === 'loading' || hydrationState === 'loading'
                  }
                  onSelect={handlePickWorkspace}
                  onCreate={handleCreateWorkspace}
                  onRename={handleRenameWorkspace}
                  onDelete={handleDeleteWorkspace}
                  creating={creatingWorkspace}
                  createError={workspaceCreateError}
                />
              </main>
            ) : (
              <main
                className="flex min-h-0 min-w-0 flex-1 flex-col"
                id="workspace-panel"
              >
                {/* Errors are the one thing allowed to interrupt the view,
                    so they sit above it on the same column axis as the
                    conversation rather than inside the scrolling log. */}
                {visibleError && (
                  <div
                    className="mx-auto mt-4 flex w-full max-w-[46rem] items-start gap-2.5 rounded-lg border border-destructive/30 bg-destructive/10 px-3.5 py-2.5 text-[13px] leading-relaxed text-destructive"
                    role="alert"
                  >
                    <AlertCircle
                      className="mt-px h-4 w-4 shrink-0"
                      aria-hidden="true"
                    />
                    {visibleError}
                  </div>
                )}

                {tab === 'Home' && workspaceId && (
                  <HomeView
                    value={input}
                    onChange={setInput}
                    onSubmit={handleSubmit}
                    sendLocked={isOptimisticPending}
                    attachments={attachments}
                    uploadState={uploadState}
                    onAttach={handleAttach}
                    onClearAttachment={handleClearAttachment}
                    onCancelUpload={handleCancelUpload}
                    mode={mode}
                    onModeChange={handleModeChange}
                    modeFixed={isThreadModeLocked}
                    modelName={modelName}
                    skillCatalog={skillCatalog}
                    maxActiveSkills={maxActiveSkills}
                    selectedSkillIds={selectedSkillIds}
                    onSkillsChange={setSelectedSkillIds}
                    deepResearch={deepResearch}
                    onDeepResearchChange={handleDeepResearchChange}
                    deepResearchAvailable={deepResearchAvailable}
                    onAddAgent={() => setAgentModalOpen(true)}
                    workers={workers}
                    workspaceName={
                      activeWorkspace?.name || 'Untitled workspace'
                    }
                    workspaceId={workspaceId}
                    workspaces={workspaces}
                    onSelectWorkspace={handlePickWorkspace}
                    onManageWorkspaces={handleOpenWorkspacePicker}
                    onCreateWorkspace={() => {
                      void handleCreateWorkspace('')
                    }}
                    onToggleNav={() => setSidebarOpen((open) => !open)}
                    navOpen={sidebarOpen}
                    recentConversations={workspaceConversations}
                    onSelectConversation={handleSelectConversation}
                  />
                )}
                {tab === 'Scheduled' && workspaceId && (
                  <ScheduledView
                    workspaceId={workspaceId}
                    workspaceName={activeWorkspace?.name || 'Untitled workspace'}
                    skillCatalog={skillCatalog}
                    maxActiveSkills={maxActiveSkills}
                    editorSeed={scheduleSeed}
                    onEditorSeedConsumed={() => setScheduleSeed(null)}
                    onOpenRun={handleOpenScheduledRun}
                    onUnseenChange={setScheduledUnseen}
                    onToggleNav={() => setSidebarOpen((open) => !open)}
                    navOpen={sidebarOpen}
                  />
                )}
                {tab === 'Task' && (
                  <ThreadView
                    jobs={threadJobs}
                    onRepeat={handleRepeatOnSchedule}
                    onSwitchToWorkforce={
                      isThreadModeLocked ? handleSwitchToWorkforce : null
                    }
                    composerProps={{
                      value: input,
                      onChange: setInput,
                      onSubmit: handleSubmit,
                      sendLocked: isOptimisticPending,
                      attachments,
                      uploadState,
                      onAttach: handleAttach,
                      onClearAttachment: handleClearAttachment,
                      onCancelUpload: handleCancelUpload,
                      mode,
                      onModeChange: handleModeChange,
                      modelName,
                      modeLocked,
                      modeFixed: isThreadModeLocked,
                      skillCatalog,
                      maxActiveSkills,
                      selectedSkillIds,
                      onSkillsChange: setSelectedSkillIds,
                      deepResearch,
                      onDeepResearchChange: handleDeepResearchChange,
                      deepResearchAvailable,
                      jobActive: isStreamingActiveJob,
                      onStop: () =>
                        latestThreadJob && handleCancelJob(latestThreadJob.job_id),
                      cancelling: Boolean(
                        latestThreadJob && cancellingJobs.has(latestThreadJob.job_id),
                      ),
                    }}
                  />
                )}
              </main>
            )}

            {!pickerOpen && tab === 'Task' && (
              <>
                {rightPanelOpen && (
                  <div
                    className="fixed inset-0 z-30 bg-black/60 backdrop-blur-[2px] lg:hidden"
                    onClick={() => setRightPanelOpen(false)}
                  />
                )}
                <RightPanel
                  job={latestThreadJob}
                  mode={mode}
                  workers={workers}
                  workersState={workersState}
                  onAddAgent={() => setAgentModalOpen(true)}
                  open={rightPanelOpen}
                  onClose={() => setRightPanelOpen(false)}
                />
              </>
            )}
          </div>
        </div>

        <AddAgentModal
          open={agentModalOpen}
          onClose={handleCloseAgentModal}
          onSave={handleSaveAgent}
          saving={savingAgent}
          error={agentError}
        />
      </div>
    </TooltipProvider>
  )
}
