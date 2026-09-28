import { useEffect, useState } from 'react'
import { AlertCircle, Check, ExternalLink, Loader2 } from 'lucide-react'

import { applyJiraAction, cancelJiraAction } from '../api.js'
import { JiraLogo } from '../brandLogos.jsx'
import { Button } from './ui/button.jsx'

/*
 * One Jira change a run proposed (backend/jira_actions.py). Nothing has been
 * written when this card appears: the text shown is exactly what Approve
 * sends, and the server refuses a second approval of the same change.
 */

export function jiraActionTitle(action) {
  const p = action?.payload || {}
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
  applying: 'Writing to Jira…',
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

function Body({ text }) {
  if (!text) return <span className="text-muted-foreground">(empty)</span>
  return (
    <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-md bg-secondary/60 px-2.5 py-2 font-sans text-[13px] leading-relaxed">
      {text}
    </pre>
  )
}

export default function JiraActionCard({ action: initial }) {
  const [action, setAction] = useState(initial)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  // A fresher copy from the job snapshot (a reload, another tab) wins.
  useEffect(() => {
    if ((initial?.updated_at || 0) > (action?.updated_at || 0)) setAction(initial)
  }, [initial]) // eslint-disable-line react-hooks/exhaustive-deps

  const p = action?.payload || {}
  const status = action?.status || 'proposed'

  const run = async (which) => {
    if (busy) return
    setBusy(which)
    setError(null)
    try {
      const call = which === 'apply' ? applyJiraAction : cancelJiraAction
      setAction(await call(action.action_id))
    } catch (err) {
      setError(err?.message || 'Could not reach the server.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div
      className="mt-3 overflow-hidden rounded-lg border border-border/60 bg-card/60"
      data-testid="jira-action-card"
    >
      <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 border-b border-border/50 px-3 py-2.5">
        <JiraLogo width={16} height={16} />
        <span className="min-w-0 flex-1 text-[13px] font-medium text-foreground">
          {jiraActionTitle(action)}
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
        {action?.kind === 'create_issue' && (
          <>
            <Field label="Project">
              {p.project_name ? `${p.project_name} (${p.project_key})` : p.project_key}
            </Field>
            <Field label="Summary">{p.summary}</Field>
            <Field label="Description">
              <Body text={p.description} />
            </Field>
            {p.note && <p className="text-xs text-amber-600 dark:text-amber-400">{p.note}</p>}
          </>
        )}
        {action?.kind !== 'create_issue' && (
          <Field label="Issue">
            {p.issue_key}
            {p.issue_summary ? ` — ${p.issue_summary}` : ''}
          </Field>
        )}
        {action?.kind === 'add_comment' && (
          <Field label="Comment">
            <Body text={p.body} />
          </Field>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-border/50 px-3 py-2">
        {status === 'applied' && action?.result?.url && (
          <a
            href={action.result.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 text-[13px] font-medium text-link hover:underline"
          >
            <Check className="h-3.5 w-3.5" aria-hidden="true" />
            Open {action.result.key || 'in Jira'}
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
                ? `Last attempt did not reach Jira: ${action.error}`
                : 'Nothing is written to Jira until you approve.'}
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
            Writing to Jira…
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
