import { useEffect, useRef, useState } from 'react'

import { Button } from './ui/button.jsx'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './ui/dialog.jsx'
import { Input, Label, Textarea } from './ui/input.jsx'

/*
 * Name is required, description optional -- a blank description is filled
 * in server-side with a synthesized sentence, because the coordinator reads
 * a worker's description when deciding assignment and would otherwise have
 * nothing to route on (see camel_service._synthesized_description). Which
 * makes the description the single highest-value field in this dialog, so
 * it is the only other thing asked for.
 *
 * Removed from this form: a searchable list of five MCP integrations that
 * were all disabled and captioned "Coming soon", and a model-configuration
 * switch that was permanently off with the explanation that models are
 * managed server-side. Neither could be acted on, and together they made a
 * two-field form look like a settings screen where most settings were
 * broken. The one fact worth keeping from them is stated as a sentence.
 */
export default function AddAgentModal({ open, onClose, onSave, saving, error }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const nameRef = useRef(null)

  useEffect(() => {
    if (open) {
      setName('')
      setDescription('')
    }
  }, [open])

  const trimmedName = name.trim()
  const canSave = Boolean(trimmedName) && !saving

  const submit = (event) => {
    event.preventDefault()
    if (!canSave) return
    onSave({ name: trimmedName, description: description.trim() })
  }

  return (
    // Radix owns focus trapping, Escape, and outside-click dismissal, so the
    // component no longer hand-rolls a keydown listener or a backdrop
    // mousedown check. onOpenChange(false) covers every dismissal route.
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        className="scrollbar-slim max-h-[85vh] max-w-md overflow-y-auto"
        onOpenAutoFocus={(event) => {
          event.preventDefault()
          nameRef.current?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle>Add an agent</DialogTitle>
          <DialogDescription>
            Available to Workforce runs for the rest of this session. The
            coordinator decides what to assign it from the description below.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="agent-name">Name</Label>
            <Input
              id="agent-name"
              ref={nameRef}
              value={name}
              maxLength={60}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. Data Analyst"
              required
              aria-required="true"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="agent-description">
              What it is responsible for{' '}
              <span className="font-normal text-muted-foreground">optional</span>
            </Label>
            <Textarea
              id="agent-description"
              value={description}
              maxLength={500}
              rows={3}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="e.g. Reads spreadsheets and produces the numeric analysis other agents cite."
              className="resize-none"
            />
            <p className="text-xs leading-relaxed text-muted-foreground">
              New agents use the same server-configured model and tools as the
              built-in ones.
            </p>
          </div>

          {error && (
            <div
              role="alert"
              className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-[13px] leading-relaxed text-destructive"
            >
              {error}
            </div>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose} disabled={saving}>
              Cancel
            </Button>
            <Button type="submit" disabled={!canSave} aria-busy={saving}>
              {saving ? 'Adding…' : 'Add agent'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
