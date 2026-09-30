import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { AlertCircle, Check, ExternalLink, Loader2 } from 'lucide-react'

import {
  applyConfluenceAction,
  applyJiraAction,
  cancelConfluenceAction,
  cancelJiraAction,
} from '../api.js'
import { ConfluenceLogo, JiraLogo } from '../brandLogos.jsx'
import { Button } from './ui/button.jsx'

/*
 * One change a run proposed to Jira or Confluence (backend/jira_actions.py,
 * backend/confluence_actions.py). Nothing has been written when this card
 * appears: what it shows is what Approve sends, and the server refuses a
 * second approval of the same change.
 */

const SYSTEMS = {
  jira: { Logo: JiraLogo, apply: applyJiraAction, cancel: cancelJiraAction, name: 'Jira' },
  confluence: {
    Logo: ConfluenceLogo,
    apply: applyConfluenceAction,
    cancel: cancelConfluenceAction,
    name: 'Confluence',
  },
}

export function approvalTitle(action) {
  const p = action?.payload || {}
  if (action?.system === 'confluence') {
    if (action?.kind === 'create_page') return `New page in ${p.space_key || 'Confluence'}`
    if (action?.kind === 'add_comment') return `Comment on “${p.page_title || 'page'}”`
    return 'Confluence change'
  }
  switch (action?.kind) {
    case 'create_issue':
      return `New ${p.issue_type || 'issue'} in ${p.project_key || 'Jira'}`
    case 'add_comment':
      return `Comment on ${p.issue_key || 'issue'}`
    case 'transition':
      return `Move ${p.issue_key || 'issue'}: ${p.current_status || '?'} → ${p.to_status || '?'}`
    default:
      return 'Jira change'
  }
}

const STATUS_LABELS = {
  proposed: 'Waiting for your approval',
  applying: 'Writing…',
  applied: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

function Field({ label, children }) {
  return (
    <div className="min-w-0">
      <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
      <div className="mt-0.5 text-[13px] leading-relaxed text-foreground">{children}</div>
    </div>
  )
}

function PlainBody({ text }) {
  if (!text) return <span className="text-muted-foreground">(empty)</span>
  return (
    <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-md bg-secondary/60 px-2.5 py-2 font-sans text-[13px] leading-relaxed">
      {text}
    </pre>
  )
}

// The page as Confluence will show it: the same Markdown the server turns
// into storage format.
function MarkdownBody({ text }) {
  if (!text) return <span className="text-muted-foreground">(empty)</span>
  return (
    <div className="max-h-80 overflow-auto rounded-md bg-secondary/60 px-3 py-2 text-[13px] leading-relaxed [&_h1]:mb-1 [&_h1]:mt-2 [&_h1]:text-base [&_h1]:font-semibold [&_h2]:mb-1 [&_h2]:mt-2 [&_h2]:text-[15px] [&_h2]:font-semibold [&_h3]:mt-2 [&_h3]:font-semibold [&_li]:ml-4 [&_ol]:list-decimal [&_p]:my-1.5 [&_table]:my-2 [&_td]:border [&_td]:border-border/60 [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-border/60 [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_ul]:list-disc">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  )
}

function JiraFields({ action }) {
  const p = action?.payload || {}
  if (action?.kind === 'create_issue') {
    return (
      <>
        <Field label="Project">
          {p.project_name ? `${p.project_name} (${p.project_key})` : p.project_key}
        </Field>
        <Field label="Summary">{p.summary}</Field>
        <Field label="Description">
          <PlainBody text={p.description} />
        </Field>
        {p.note && <p className="text-xs text-amber-600 dark:text-amber-400">{p.note}</p>}
      </>
    )
  }
  return (
    <>
      <Field label="Issue">
        {p.issue_key}
        {p.issue_summary ? ` — ${p.issue_summary}` : ''}
      </Field>
      {action?.kind === 'add_comment' && (
        <Field label="Comment">
          <PlainBody text={p.body} />
        </Field>
      )}
    </>
  )
}

function ConfluenceFields({ action }) {
  const p = action?.payload || {}
  if (action?.kind === 'create_page') {
    return (
      <>
        <Field label="Space">
          {p.space_name && p.space_name !== p.space_key ? `${p.space_name} (${p.space_key})` : p.space_key}
          {p.parent_title ? ` › under “${p.parent_title}”` : ''}
        </Field>
        <Field label="Title">{p.title}</Field>
        <Field label="Content">
          <MarkdownBody text={p.body_markdown} />
        </Field>
      </>
    )
  }
  return (
    <>
      <Field label="Page">
        {p.page_url ? (
          <a href={p.page_url} target="_blank" rel="noreferrer" className="text-link hover:underline">
            {p.page_title}
          </a>
        ) : (
          p.page_title
        )}
        {p.space_key ? ` (${p.space_key})` : ''}
      </Field>
      <Field label="Comment">
        <MarkdownBody text={p.body_markdown} />
      </Field>
    </>
  )
}

export default function ApprovalCard({ action: initial }) {
  const [action, setAction] = useState(initial)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  // A fresher copy from the job snapshot (a reload, another tab) wins.
  useEffect(() => {
    if ((initial?.updated_at || 0) > (action?.updated_at || 0)) setAction(initial)
  }, [initial]) // eslint-disable-line react-hooks/exhaustive-deps

  const system = SYSTEMS[action?.system] || SYSTEMS.jira
  const { Logo } = system
  const status = action?.status || 'proposed'
  const result = action?.result || {}

  const run = async (which) => {
    if (busy) return
    setBusy(which)
    setError(null)
    try {
      setAction(await system[which](action.action_id))
    } catch (err) {
      setError(err?.message || 'Could not reach the server.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div
      className="mt-3 overflow-hidden rounded-lg border border-border/60 bg-card/60"
      data-testid="approval-card"
      data-system={action?.system}
    >
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 border-b border-border/50 px-3 py-2.5">
        <Logo width={16} height={16} />
        <span className="min-w-0 flex-1 text-[13px] font-medium text-foreground">
          {approvalTitle(action)}
        </span>
        <span
          className={
            status === 'applied'
              ? 'text-xs text-emerald-600 dark:text-emerald-400'
              : status === 'failed'
                ? 'text-xs text-destructive'
                : 'text-xs text-muted-foreground'
          }
        >
          {STATUS_LABELS[status] || status}
        </span>
      </div>

      <div className="flex flex-col gap-2.5 px-3 py-2.5">
        {action?.system === 'confluence' ? (
          <ConfluenceFields action={action} />
        ) : (
          <JiraFields action={action} />
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-border/50 px-3 py-2">
        {status === 'applied' && result.url && (
          <a
            href={result.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-[13px] font-medium text-link hover:underline"
          >
            <Check className="h-3.5 w-3.5" aria-hidden="true" />
            Open {result.key || (action?.kind === 'create_page' ? 'the page' : `in ${system.name}`)}
            <ExternalLink className="h-3 w-3" aria-hidden="true" />
          </a>
        )}
        {status === 'failed' && action?.error && (
          <p className="flex min-w-0 flex-1 items-start gap-1.5 text-xs text-destructive" role="alert">
            <AlertCircle className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span>{action.error}</span>
          </p>
        )}
        {status === 'proposed' && (
          <>
            <span className="min-w-0 flex-1 text-xs text-muted-foreground">
              {action?.error
                ? `Last attempt did not reach ${system.name}: ${action.error}`
                : `Nothing is written to ${system.name} until you approve.`}
            </span>
            <Button
              type="button"
              size="xs"
              variant="ghost"
              disabled={Boolean(busy)}
              onClick={() => run('cancel')}
            >
              {busy === 'cancel' && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
              Cancel
            </Button>
            <Button type="button" size="xs" disabled={Boolean(busy)} onClick={() => run('apply')}>
              {busy === 'apply' && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
              Approve
            </Button>
          </>
        )}
        {status === 'applying' && (
          <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            Writing to {system.name}…
          </span>
        )}
        {error && (
          <p className="w-full text-xs text-destructive" role="alert">
            {error}
          </p>
        )}
      </div>
    </div>
  )
}
