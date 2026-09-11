import { Clock, LayoutGrid, Layers, PanelLeft, Plus, Trash2 } from 'lucide-react'

import { useState } from 'react'

import { ERROR_JOB_STATUSES, SUCCESS_JOB_STATUSES, isActiveJob } from '../stages.js'
import { cn } from '../lib/utils.js'
import { Badge, MarkSlot } from './ui/badge.jsx'
import { Button } from './ui/button.jsx'
import { ScrollArea } from './ui/scroll-area.jsx'

// Status is carried by a 6px dot rather than an icon: at this size an icon
// is unreadable anyway, and a column of three different glyphs made the
// list look busier than it is. Colour alone is not the signal -- every row
// also has its title and a tooltip -- so this stays legible without it.
function StatusMark({ status }) {
  const active = isActiveJob({ status })
  return (
    <MarkSlot>
      <span
        className={cn(
          'h-1.5 w-1.5 rounded-full',
          SUCCESS_JOB_STATUSES.has(status) && 'bg-success/70',
          ERROR_JOB_STATUSES.has(status) && 'bg-destructive/80',
          active && 'animate-pulse-soft bg-link',
          !active &&
            !SUCCESS_JOB_STATUSES.has(status) &&
            !ERROR_JOB_STATUSES.has(status) &&
            'bg-muted-foreground/40',
        )}
      />
    </MarkSlot>
  )
}

function ConversationSkeleton() {
  return (
    <div className="flex flex-col gap-px" aria-hidden="true">
      {[70, 55, 62].map((width, index) => (
        <div key={index} className="flex items-start gap-2 px-2.5 py-2">
          <MarkSlot>
            <span className="h-1.5 w-1.5 rounded-full bg-muted" />
          </MarkSlot>
          <span
            className="mt-1 h-3 animate-pulse-soft rounded-sm bg-muted"
            style={{ width: `${width}%` }}
          />
        </div>
      ))}
    </div>
  )
}

// The Recent list is scoped to the active Workspace. "Workspaces" returns to
// workspace selection; "New chat" creates a sibling conversation in place.
export default function Sidebar({
  conversations,
  activeWorkspaceName,
  activeConversationId,
  loading = false,
  open = false,
  onClose,
  onSelectConversation,
  onDeleteConversation,
  onNew,
  onGoHome,
  workspacesSelected = false,
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(null)
  return (
    <aside
      className={cn(
        'flex h-full min-h-0 w-[248px] shrink-0 flex-col border-r border-border/70 bg-panel',
        // Below md: drawer over the conversation (execution panel's mirror).
        // At md+: docked column that can fully collapse via TopBar — same
        // open/close contract as the right panel, not mobile-only anymore.
        'max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-40 max-md:shadow-2xl max-md:transition-transform max-md:duration-200',
        open
          ? 'max-md:translate-x-0'
          : 'max-md:invisible max-md:-translate-x-full md:hidden',
      )}
    >
      {/* Same h-14 + border-b as TopBar / Execution so the three header
          rails share one horizontal line. Desktop collapse is on TopBar
          (paired with PanelRight); this close is drawer-only. */}
      <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-border/70 pl-4 pr-2">
        <span
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-gradient-to-br from-primary to-accent-secondary text-primary-foreground"
          aria-hidden="true"
        >
          <Layers className="h-4 w-4" />
        </span>
        <span className="min-w-0 flex-1 truncate text-[13px] font-semibold tracking-tight">
          Agent Workspace
        </span>
        {onClose && (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            onClick={onClose}
            aria-label="Hide chat list"
            className="shrink-0 text-muted-foreground hover:text-foreground md:hidden"
          >
            <PanelLeft className="h-4 w-4" aria-hidden="true" />
          </Button>
        )}
      </div>

      {/* Same slot on every screen so Workspaces never jumps into the header. */}
      <div className="px-3 pb-3 pt-3">
        <Button
          type="button"
          variant="outline"
          onClick={onNew}
          className="w-full justify-start gap-2 text-foreground"
        >
          <Plus className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
          New chat
        </Button>
      </div>

      <nav className="flex flex-col gap-px px-3" aria-label="Primary">
        <button
          type="button"
          onClick={onGoHome}
          className={cn(
            'flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-[13px] font-medium transition-colors duration-150',
            workspacesSelected
              ? 'bg-accent text-foreground'
              : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground',
          )}
        >
          <LayoutGrid className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span>Workspaces</span>
        </button>
        {/* Navigation placeholder: scheduling has no backend in this
            release, so the item announces itself as unavailable rather
            than leading to a dead screen. */}
        <button
          type="button"
          disabled
          title="Scheduled runs are not available yet"
          className="flex w-full cursor-not-allowed items-center gap-2.5 rounded-md px-2.5 py-2 text-[13px] font-medium text-muted-foreground/50"
        >
          <Clock className="h-4 w-4 shrink-0" aria-hidden="true" />
          <span>Scheduled</span>
          <Badge variant="outline" className="ml-auto border-border/60">
            Soon
          </Badge>
        </button>
      </nav>

      {activeWorkspaceName && (
        <div className="mt-6 flex min-h-0 flex-1 flex-col">
          {/* The workspace name labels the list it scopes, instead of
              floating on its own above an unrelated button. It repeats the
              list's own padding structure (px-3 outer + px-2.5 inner)
              rather than one hand-summed value, so the label stays flush
              with the rows' left edge if either padding ever changes. */}
          <div className="px-3 pb-2">
            <div className="flex items-center gap-2 px-2.5">
              <span
                className="eyebrow min-w-0 flex-1 truncate"
                title={activeWorkspaceName}
              >
                {activeWorkspaceName}
              </span>
              {conversations.length > 0 && (
                <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground/70">
                  {conversations.length}
                </span>
              )}
            </div>
          </div>

          <ScrollArea className="min-h-0 flex-1">
            <div className="flex flex-col gap-px px-3 pb-4">
              {loading && conversations.length === 0 && <ConversationSkeleton />}
              {!loading && conversations.length === 0 && (
                <p className="px-2.5 py-2 text-[13px] leading-relaxed text-muted-foreground">
                  No conversations yet. Start one with New chat.
                </p>
              )}
              {conversations.map((conversation) => {
                const isCurrentThread = conversation.conversationId
                  ? conversation.conversationId === activeConversationId
                  : conversation.latestJobId === activeConversationId
                const key = conversation.conversationId || conversation.latestJobId
                const isConfirming = confirmingDelete === key
                if (isConfirming) {
                  return (
                    <div
                      key={key}
                      className="flex flex-col gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-2"
                    >
                      <span className="text-[12px] leading-4 text-destructive">
                        Delete this chat? This cannot be undone.
                      </span>
                      <div className="flex items-center gap-1.5">
                        <button
                          type="button"
                          onClick={() => {
                            setConfirmingDelete(null)
                            onDeleteConversation?.(conversation)
                          }}
                          className="rounded px-2 py-1 text-[11px] font-medium text-destructive-foreground bg-destructive hover:bg-destructive/90"
                        >
                          Delete
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirmingDelete(null)}
                          className="rounded px-2 py-1 text-[11px] font-medium text-muted-foreground hover:bg-accent/50"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )
                }
                return (
                  <div
                    key={key}
                    className={cn(
                      'group flex w-full items-start gap-1 rounded-md transition-colors duration-150',
                      isCurrentThread
                        ? 'bg-accent text-foreground'
                        : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground',
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => onSelectConversation(conversation)}
                      title={conversation.title}
                      aria-current={isCurrentThread ? 'true' : undefined}
                      className="flex min-w-0 flex-1 items-start gap-2 px-2.5 py-2 text-left"
                    >
                      <StatusMark status={conversation.status} />
                      <span
                        className={cn(
                          'min-w-0 flex-1 truncate text-[13px] leading-5',
                          isCurrentThread && 'font-medium',
                        )}
                      >
                        {conversation.title}
                      </span>
                    </button>
                    {onDeleteConversation && (
                      <button
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation()
                          setConfirmingDelete(key)
                        }}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter') {
                            event.stopPropagation()
                            setConfirmingDelete(key)
                          }
                        }}
                        className="mt-1.5 mr-1.5 shrink-0 rounded p-1 text-muted-foreground/0 transition-colors group-hover:text-muted-foreground hover:!text-destructive"
                        aria-label="Delete conversation"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                )
              })}
            </div>
          </ScrollArea>
        </div>
      )}
    </aside>
  )
}
