import { useCallback, useEffect, useRef, useState } from 'react'
import { Library, Loader2, Plus, X } from 'lucide-react'

import {
  getWorkspaceDocuments,
  removeWorkspaceDocument,
  uploadWorkspaceDocuments,
} from '../api.js'
import { cn } from '../lib/utils.js'
import { SimpleTooltip } from './ui/tooltip.jsx'

// What the Knowledge Agent can search: the documents uploaded to this
// workspace, from any chat or straight into this list. Formats are the ones
// the index reads (knowledge_index.py); zip and csv are left out because
// they are stored but never searched, and offering them here would promise
// the opposite.
export const LIBRARY_EXTENSIONS = [
  '.pdf',
  '.docx',
  '.pptx',
  '.xlsx',
  '.txt',
  '.md',
  '.png',
  '.jpg',
  '.jpeg',
  '.tif',
  '.tiff',
  '.webp',
]

const POLL_MS = 2000
const MAX_ROWS = 6

// One line under the file name: what the index made of it.
export function documentStatusLine(doc) {
  switch (doc?.status) {
    case 'indexed':
      return doc.passage_count === 1 ? '1 passage' : `${doc.passage_count} passages`
    case 'pending':
      return 'Indexing…'
    case 'missing':
      return 'Waiting to be indexed'
    case 'skipped':
      return doc.reason ? `Not searchable: ${doc.reason}` : 'Not searchable'
    case 'failed':
      return 'Could not be indexed'
    default:
      return ''
  }
}

function isBusy(doc) {
  return doc?.status === 'pending' || doc?.status === 'missing'
}

export default function LibraryPanel({ workspaceId }) {
  const [documents, setDocuments] = useState([])
  const [loaded, setLoaded] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const [showAll, setShowAll] = useState(false)
  const inputRef = useRef(null)

  const refresh = useCallback(
    async (signal) => {
      if (!workspaceId) return
      try {
        const body = await getWorkspaceDocuments(workspaceId, { signal })
        setDocuments(body.documents || [])
        setLoaded(true)
      } catch (err) {
        if (err?.name !== 'AbortError') setError(err.message || 'Could not load documents')
      }
    },
    [workspaceId],
  )

  useEffect(() => {
    setDocuments([])
    setLoaded(false)
    setError('')
    setShowAll(false)
    const controller = new AbortController()
    refresh(controller.signal)
    return () => controller.abort()
  }, [refresh])

  // Poll only while something is still being indexed.
  const busy = documents.some(isBusy)
  useEffect(() => {
    if (!busy) return undefined
    const timer = setInterval(() => refresh(), POLL_MS)
    return () => clearInterval(timer)
  }, [busy, refresh])

  async function handleFiles(fileList) {
    const files = Array.from(fileList || []).filter((file) =>
      LIBRARY_EXTENSIONS.some((ext) => file.name.toLowerCase().endsWith(ext)),
    )
    if (!files.length) return
    setUploading(true)
    setError('')
    try {
      const body = await uploadWorkspaceDocuments(workspaceId, files)
      setDocuments(body.documents || [])
    } catch (err) {
      setError(err.message || 'Upload failed')
    } finally {
      setUploading(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  async function handleRemove(doc) {
    setError('')
    setDocuments((current) => current.filter((item) => item.file_id !== doc.file_id))
    try {
      await removeWorkspaceDocument(workspaceId, doc.file_id)
    } catch (err) {
      setError(err.message || 'Could not remove the document')
      refresh()
    }
  }

  if (!workspaceId || !loaded) return null

  const newestFirst = [...documents].reverse()
  const rows = showAll ? newestFirst : newestFirst.slice(0, MAX_ROWS)
  const hidden = newestFirst.length - rows.length

  return (
    <section className="w-full" aria-labelledby="library-heading">
      <div className="mb-3 flex items-center justify-center gap-2">
        <Library className="h-3.5 w-3.5 text-muted-foreground/50" aria-hidden="true" />
        <h3
          id="library-heading"
          className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground/70"
        >
          Documents{documents.length ? ` · ${documents.length}` : ''}
        </h3>
        <SimpleTooltip label="Add documents the agents can search">
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            disabled={uploading}
            aria-label="Add documents"
            className={cn(
              'ml-1 flex h-5 w-5 items-center justify-center rounded-full text-muted-foreground/70',
              'transition-colors duration-150 hover:bg-muted hover:text-foreground',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
              'disabled:cursor-not-allowed disabled:opacity-50',
            )}
          >
            {uploading ? (
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
            ) : (
              <Plus className="h-3 w-3" aria-hidden="true" />
            )}
          </button>
        </SimpleTooltip>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={LIBRARY_EXTENSIONS.join(',')}
          className="hidden"
          onChange={(event) => handleFiles(event.target.files)}
        />
      </div>

      {documents.length === 0 ? (
        <p className="text-center text-[12px] leading-5 text-muted-foreground">
          Add contracts, reports or notes here, then ask about them in any chat.
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {rows.map((doc) => {
            const line = documentStatusLine(doc)
            const unusable = doc.status === 'skipped' || doc.status === 'failed'
            return (
              <li
                key={doc.file_id}
                className={cn(
                  'group flex items-center gap-3 rounded-full px-3.5 py-2',
                  'transition-colors duration-150 hover:bg-muted/50',
                )}
              >
                <span
                  className={cn(
                    'flex h-5 w-5 shrink-0 items-center justify-center rounded-full',
                    doc.status === 'indexed' && 'bg-success/15 text-success',
                    isBusy(doc) && 'bg-link/15 text-link',
                    unusable && 'bg-muted text-muted-foreground/60',
                  )}
                  aria-hidden="true"
                >
                  {isBusy(doc) ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <span className="h-1.5 w-1.5 rounded-full bg-current" />
                  )}
                </span>
                <span className="flex min-w-0 flex-1 items-baseline gap-2">
                  <span
                    className={cn(
                      'min-w-0 truncate text-[13px]',
                      unusable ? 'text-muted-foreground' : 'text-foreground/90',
                    )}
                    title={doc.filename}
                  >
                    {doc.filename}
                  </span>
                  <span
                    className="shrink-0 truncate text-[11px] text-muted-foreground/80"
                    title={line}
                  >
                    {line}
                  </span>
                </span>
                <SimpleTooltip label="Remove from this workspace's documents">
                  <button
                    type="button"
                    onClick={() => handleRemove(doc)}
                    aria-label={`Remove ${doc.filename}`}
                    className={cn(
                      'flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-muted-foreground/70',
                      'opacity-0 transition-opacity duration-150 group-hover:opacity-100 focus-visible:opacity-100',
                      'hover:bg-muted hover:text-foreground',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
                    )}
                  >
                    <X className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                </SimpleTooltip>
              </li>
            )
          })}
        </ul>
      )}

      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setShowAll(true)}
          className="mx-auto mt-1 block text-[12px] text-muted-foreground transition-colors duration-150 hover:text-foreground"
        >
          Show {hidden} more
        </button>
      )}

      {error && (
        <p className="mt-2 text-center text-[12px] text-destructive" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
