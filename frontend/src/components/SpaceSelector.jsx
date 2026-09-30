import { useMemo, useState } from 'react'
import {
  Check,
  ChevronDown,
  ChevronsUpDown,
  Folder,
  LayoutGrid,
  Plus,
} from 'lucide-react'

import { cn } from '../lib/utils.js'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu.jsx'

// Listing every space inline pushed "New space" / "All spaces" past the
// bottom of the screen once a few dozen existed — the two items people
// actually need most. Show the recently used ones, keep the actions
// reachable, and let the rest open on request.
const VISIBLE_SPACES = 5

/**
 * Compact workspace switcher — the "Untitled Space" pill from the home hero.
 * Lists recently used workspaces inline; "All spaces" opens the full picker
 * for create / rename / delete.
 */
export default function SpaceSelector({
  workspaceName = 'Untitled workspace',
  workspaceId = null,
  workspaces = [],
  onSelectWorkspace,
  onManageWorkspaces,
  onCreateWorkspace,
  className,
}) {
  const label = workspaceName?.trim() || 'Untitled workspace'
  const [expanded, setExpanded] = useState(false)

  // Most recently active first, but never hide the space you are in.
  const ordered = useMemo(() => {
    const list = [...workspaces].sort(
      (a, b) => (b.last_active_at || 0) - (a.last_active_at || 0),
    )
    const currentIndex = list.findIndex((w) => w.workspace_id === workspaceId)
    if (currentIndex > 0) {
      const [current] = list.splice(currentIndex, 1)
      list.unshift(current)
    }
    return list
  }, [workspaces, workspaceId])

  const hidden = Math.max(0, ordered.length - VISIBLE_SPACES)
  const shown = expanded ? ordered : ordered.slice(0, VISIBLE_SPACES)

  return (
    <DropdownMenu onOpenChange={(open) => !open && setExpanded(false)}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={cn(
            'inline-flex max-w-[min(100%,20rem)] items-center gap-2 rounded-full',
            'border border-border/70 bg-muted/50 px-3.5 py-1.5',
            'text-[13px] font-medium text-foreground/90',
            'transition-colors duration-150',
            'hover:border-border hover:bg-muted/80 hover:text-foreground',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
            className,
          )}
          aria-label={`Workspace: ${label}. Change workspace`}
        >
          <Folder className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="min-w-0 truncate">{label}</span>
          <ChevronsUpDown
            className="h-3.5 w-3.5 shrink-0 text-muted-foreground/70"
            aria-hidden="true"
          />
        </button>
      </DropdownMenuTrigger>

      <DropdownMenuContent align="center" className="min-w-[14rem] max-w-[20rem]">
        <DropdownMenuLabel>Spaces</DropdownMenuLabel>
        {ordered.length === 0 ? (
          <DropdownMenuItem disabled>No workspaces yet</DropdownMenuItem>
        ) : (
          <div className="max-h-[45vh] overflow-y-auto">
            {shown.map((workspace) => {
              const id = workspace.workspace_id
              const name = workspace.name?.trim() || 'Untitled workspace'
              const active = id === workspaceId
              return (
                <DropdownMenuItem
                  key={id}
                  onSelect={() => {
                    if (!active) onSelectWorkspace?.(id)
                  }}
                  className="gap-2"
                >
                  <Folder className="h-3.5 w-3.5" aria-hidden="true" />
                  <span className="min-w-0 flex-1 truncate">{name}</span>
                  {active && (
                    <Check className="h-3.5 w-3.5 text-foreground" aria-hidden="true" />
                  )}
                </DropdownMenuItem>
              )
            })}
          </div>
        )}
        {hidden > 0 && !expanded && (
          <DropdownMenuItem
            onSelect={(event) => {
              // Keep the menu open — this reveals rows, it does not pick one.
              event.preventDefault()
              setExpanded(true)
            }}
            className="gap-2 text-muted-foreground"
          >
            <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
            Show {hidden} more
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
        {onCreateWorkspace && (
          <DropdownMenuItem onSelect={() => onCreateWorkspace()}>
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            New space
          </DropdownMenuItem>
        )}
        {onManageWorkspaces && (
          <DropdownMenuItem onSelect={() => onManageWorkspaces()}>
            <LayoutGrid className="h-3.5 w-3.5" aria-hidden="true" />
            All spaces
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
