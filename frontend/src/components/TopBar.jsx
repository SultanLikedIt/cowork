import { PanelLeft, PanelRight, Plus } from 'lucide-react'

import { ERROR_JOB_STATUSES, SUCCESS_JOB_STATUSES, safeText } from '../stages.js'
import { cn } from '../lib/utils.js'
import { Badge, StatusDot } from './ui/badge.jsx'
import { Button } from './ui/button.jsx'
import { SimpleTooltip } from './ui/tooltip.jsx'

// The run's outcome, stated once, in the one place that is always on
// screen. Before this existed the only report of "is this still running"
// lived inside the conversation itself, which meant scrolling to find it.
function RunStatus({ status }) {
  if (!status) return null
  if (SUCCESS_JOB_STATUSES.has(status)) {
    return (
      <Badge variant="success" className="shrink-0">
        Completed
      </Badge>
    )
  }
  if (ERROR_JOB_STATUSES.has(status)) {
    return (
      <Badge variant="destructive" className="shrink-0">
        {status === 'cancelled' ? 'Stopped' : 'Failed'}
      </Badge>
    )
  }
  return (
    <Badge variant="running" className="shrink-0">
      <StatusDot pulse />
      Running
    </Badge>
  )
}

/**
 * Working-surface header. Left and right panel toggles live in this same
 * row (same height, same icon-sm button, mirrored) so they share one
 * baseline — the left control no longer sits in the sidebar chrome where
 * padding/logo made it drift off the right toggle's line.
 */
export default function TopBar({
  workspaceName,
  title,
  status,
  onToggleNav,
  navOpen,
  onNewConversation,
  onTogglePanel,
  panelOpen,
  showPanelToggle,
}) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-2 border-b border-border/70 px-3 sm:px-4">
      <SimpleTooltip label={navOpen ? 'Hide chat list' : 'Show chat list'}>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          onClick={onToggleNav}
          aria-label={navOpen ? 'Hide chat list' : 'Show chat list'}
          aria-pressed={navOpen}
          className={cn(
            'shrink-0 text-muted-foreground hover:text-foreground',
            navOpen && 'bg-accent text-foreground',
          )}
        >
          <PanelLeft className="h-4 w-4" aria-hidden="true" />
        </Button>
      </SimpleTooltip>

      <div className="flex min-w-0 flex-1 items-center gap-1.5">
        {workspaceName && (
          <>
            <span className="hidden max-w-[10rem] shrink-0 truncate text-[13px] leading-5 text-muted-foreground sm:inline">
              {workspaceName}
            </span>
            {title ? (
              <span
                className="hidden shrink-0 text-[13px] leading-5 text-muted-foreground/50 sm:inline"
                aria-hidden="true"
              >
                /
              </span>
            ) : null}
          </>
        )}
        {title ? (
          <h1 className="min-w-0 truncate text-[13px] font-medium leading-5 text-foreground">
            {safeText(title, 'Untitled conversation')}
          </h1>
        ) : null}
      </div>

      <div className="flex shrink-0 items-center gap-1.5">
        <RunStatus status={status} />
        {title && (
          <SimpleTooltip label="New conversation">
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              onClick={onNewConversation}
              aria-label="New conversation"
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
            </Button>
          </SimpleTooltip>
        )}
        {showPanelToggle && (
          <SimpleTooltip
            label={panelOpen ? 'Hide execution panel' : 'Show execution panel'}
          >
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              onClick={onTogglePanel}
              aria-label={
                panelOpen ? 'Hide execution panel' : 'Show execution panel'
              }
              aria-pressed={panelOpen}
              className={cn(
                'shrink-0 text-muted-foreground hover:text-foreground',
                panelOpen && 'bg-accent text-foreground',
              )}
            >
              <PanelRight className="h-4 w-4" aria-hidden="true" />
            </Button>
          </SimpleTooltip>
        )}
      </div>
    </header>
  )
}
