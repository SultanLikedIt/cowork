import { useState } from 'react'
import {
  ArrowRight,
  Folder,
  Pencil,
  Plus,
  Trash2,
} from 'lucide-react'

import { cn } from '../lib/utils.js'
import { Button } from './ui/button.jsx'
import { Input } from './ui/input.jsx'
import { ScrollArea } from './ui/scroll-area.jsx'

function formatWorkspaceActivity(timestamp) {
  if (!timestamp) return null
  const then = new Date(timestamp * 1000)
  const days = Math.floor((Date.now() - then.getTime()) / 86400000)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 7) return `${days} days ago`
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
  }).format(then)
}

function WorkspaceRow({ workspace, count, onSelect, onRename, onDelete }) {
  const [mode, setMode] = useState('idle') // idle | rename | confirm-delete
  const [editName, setEditName] = useState(workspace.name || '')
  const [busy, setBusy] = useState(false)
  const activity = formatWorkspaceActivity(workspace.last_active_at)
  const label = workspace.name || 'Untitled workspace'

  const submitRename = async (event) => {
    event.preventDefault()
    if (busy || !onRename) return
    setBusy(true)
    try {
      await onRename(workspace.workspace_id, editName.trim())
      setMode('idle')
    } catch {
      // Keep the rename form open so the user can retry.
    } finally {
      setBusy(false)
    }
  }

  const confirmDelete = async () => {
    if (busy || !onDelete) return
    setBusy(true)
    try {
      await onDelete(workspace.workspace_id)
    } catch {
      setMode('idle')
    } finally {
      setBusy(false)
    }
  }

  if (mode === 'confirm-delete') {
    return (
      <div className="flex items-center gap-3 rounded-full border border-destructive/35 bg-destructive/10 px-5 py-3">
        <span className="min-w-0 flex-1 text-[13px] leading-5 text-destructive">
          <span className="font-medium">Delete “{label}”?</span>
        </span>
        <Button
          type="button"
          size="sm"
          variant="destructive"
          disabled={busy}
          onClick={confirmDelete}
          className="shrink-0 rounded-full"
        >
          {busy ? 'Deleting…' : 'Delete'}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={busy}
          onClick={() => setMode('idle')}
          className="shrink-0 rounded-full"
        >
          Cancel
        </Button>
      </div>
    )
  }

  if (mode === 'rename') {
    return (
      <form
        onSubmit={submitRename}
        className="flex items-center gap-2 rounded-full border border-border/70 bg-card px-3 py-2"
      >
        <Folder className="ml-1 h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        <Input
          value={editName}
          maxLength={80}
          autoFocus
          disabled={busy}
          onChange={(event) => setEditName(event.target.value)}
          aria-label="Workspace name"
          className="h-8 min-w-0 flex-1 rounded-full border-0 bg-transparent px-2 text-[13px] shadow-none focus-visible:ring-0"
        />
        <Button type="submit" size="sm" disabled={busy} className="shrink-0 rounded-full">
          {busy ? 'Saving…' : 'Save'}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={busy}
          onClick={() => {
            setEditName(workspace.name || '')
            setMode('idle')
          }}
          className="shrink-0 rounded-full"
        >
          Cancel
        </Button>
      </form>
    )
  }

  return (
    <div
      className={cn(
        'group relative flex items-center gap-1 rounded-full border border-border/60 bg-muted/40 py-1.5 pl-1.5 pr-2',
        'transition-all duration-200',
        'hover:border-border hover:bg-muted/70',
      )}
    >
      <button
        type="button"
        onClick={() => onSelect(workspace.workspace_id)}
        className={cn(
          'flex min-w-0 flex-1 items-center gap-2.5 rounded-full px-3 py-2 text-left',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
        )}
      >
        <Folder className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-foreground">
          {label}
        </span>
        <span className="hidden shrink-0 text-[11px] text-muted-foreground/60 sm:inline">
          {count === 0
            ? 'No chats'
            : `${count} ${count === 1 ? 'chat' : 'chats'}`}
          {activity ? ` · ${activity}` : ''}
        </span>
      </button>

      <div className="flex shrink-0 items-center gap-0.5 pr-1">
        {onRename && (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              setEditName(workspace.name || '')
              setMode('rename')
            }}
            className="rounded-full p-2 text-muted-foreground opacity-100 transition-colors hover:bg-accent hover:text-foreground sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
            aria-label={`Rename ${label}`}
            title="Rename"
          >
            <Pencil className="h-3.5 w-3.5" />
          </button>
        )}
        {onDelete && (
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation()
              setMode('confirm-delete')
            }}
            className="rounded-full p-2 text-muted-foreground opacity-100 transition-colors hover:bg-destructive/10 hover:text-destructive sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
            aria-label={`Delete ${label}`}
            title="Delete"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
        <span
          className="flex h-7 w-7 items-center justify-center text-muted-foreground/40 transition-all duration-200 group-hover:translate-x-0.5 group-hover:text-muted-foreground"
          aria-hidden="true"
        >
          <ArrowRight className="h-3.5 w-3.5" />
        </span>
      </div>
    </div>
  )
}

function NewSpaceRow({ name, setName, onSubmit, creating }) {
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit(name.trim())
      }}
      className={cn(
        'group flex items-center gap-2 rounded-full border border-dashed border-border/70 bg-transparent px-3 py-2',
        'transition-colors duration-150',
        'hover:border-border hover:bg-muted/30',
        'focus-within:border-border focus-within:bg-muted/40',
      )}
    >
      <Plus className="ml-1 h-3.5 w-3.5 shrink-0 text-muted-foreground/70" aria-hidden="true" />
      <Input
        value={name}
        maxLength={80}
        onChange={(e) => setName(e.target.value)}
        placeholder="New space name (optional)"
        aria-label="New workspace name"
        disabled={creating}
        className="h-8 min-w-0 flex-1 rounded-full border-0 bg-transparent px-2 text-[13px] shadow-none focus-visible:ring-0"
      />
      <Button
        type="submit"
        size="sm"
        disabled={creating}
        className="h-8 shrink-0 rounded-full px-3"
      >
        {creating ? 'Creating…' : 'Create'}
      </Button>
    </form>
  )
}

export default function WorkspacePicker({
  workspaces = [],
  conversationCounts = new Map(),
  loading = false,
  onSelect,
  onCreate,
  onRename,
  onDelete,
  creating,
  createError,
}) {
  const [name, setName] = useState('')
  const hasWorkspaces = workspaces.length > 0

  return (
    <ScrollArea className="min-h-0 flex-1">
      <div className="surface-grid relative min-h-full">
        <div className="relative mx-auto flex w-full max-w-[42rem] flex-col items-center px-6 pb-16 pt-12 animate-fade-in-up sm:px-8 sm:pt-16">
          <div className="mb-8 flex flex-col items-center text-center">
            <span
              className={cn(
                'mb-6 inline-flex items-center gap-2 rounded-full',
                'border border-border/70 bg-muted/50 px-3.5 py-1.5',
                'text-[13px] font-medium text-foreground/90',
              )}
            >
              <Folder className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
              Choose a space
            </span>
            <h1 className="text-[28px] font-semibold leading-tight tracking-[-0.02em] text-foreground sm:text-[32px]">
              Your spaces
            </h1>
            <p className="mt-3 max-w-md text-[14px] leading-relaxed text-muted-foreground">
              Pick a workspace to cowork with a single agent or a full workforce.
            </p>
          </div>

          <div className="w-full">
            {createError && (
              <p className="mb-3 text-center text-[13px] text-destructive" role="alert">
                {createError}
              </p>
            )}

            <div className="flex flex-col gap-2">
              <NewSpaceRow
                name={name}
                setName={setName}
                onSubmit={onCreate}
                creating={creating}
              />

              {loading && !hasWorkspaces ? (
                <>
                  {[0, 1].map((i) => (
                    <div
                      key={i}
                      className="flex items-center gap-3 rounded-full border border-border/40 bg-muted/30 px-4 py-3"
                      aria-hidden="true"
                    >
                      <span className="h-3.5 w-3.5 shrink-0 animate-pulse-soft rounded-sm bg-muted" />
                      <span className="h-3 w-1/2 animate-pulse-soft rounded-sm bg-muted" />
                    </div>
                  ))}
                </>
              ) : hasWorkspaces ? (
                workspaces.map((workspace) => (
                  <WorkspaceRow
                    key={workspace.workspace_id}
                    workspace={workspace}
                    count={conversationCounts.get(workspace.workspace_id) || 0}
                    onSelect={onSelect}
                    onRename={onRename}
                    onDelete={onDelete}
                  />
                ))
              ) : (
                <div className="rounded-2xl border border-dashed border-border/60 px-6 py-10 text-center">
                  <p className="text-[14px] font-medium text-foreground/80">
                    No spaces yet
                  </p>
                  <p className="mx-auto mt-2 max-w-xs text-[13px] leading-relaxed text-muted-foreground">
                    Create your first space above to start coworking.
                  </p>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </ScrollArea>
  )
}
