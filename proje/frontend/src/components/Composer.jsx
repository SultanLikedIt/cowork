import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { Microscope, Paperclip, X } from 'lucide-react'

import { ArrowUpIcon, StopIcon } from '../icons.jsx'
import { safeText } from '../stages.js'
import { cn } from '../lib/utils.js'
import SkillPicker from './SkillPicker.jsx'
import { Button } from './ui/button.jsx'
import { SimpleTooltip } from './ui/tooltip.jsx'

// "openai/gpt-4o" / "deepseek/deepseek-v4-flash" -> the part after the
// vendor prefix. The full identifier is still shown in the Usage section.
function shortModelName(modelName) {
  const full = safeText(modelName, 'Configured model')
  const tail = full.split('/').pop()
  return tail || full
}

// The description is what the segmented control's tooltip says, so the
// difference between the two modes is explained on demand instead of in a
// permanent line of copy under every composer in the app.
const MODE_OPTIONS = [
  {
    value: 'single',
    label: 'Single Agent',
    hint: 'One assistant answers directly. Fastest for questions and drafting.',
  },
  {
    value: 'multi',
    label: 'Workforce',
    hint: 'Splits the task across specialist agents. Better for research and multi-step work.',
  },
]

// Auto-grow bounds. Below MIN the box looks like a single-line input and
// invites one-liners; above MAX a pasted document would push the send
// controls off-screen, so the textarea starts scrolling internally
// instead of growing further.
const MIN_TEXTAREA_HEIGHT = 52
const MAX_TEXTAREA_HEIGHT = 320

// One list for every way a file arrives. The file input's accept= is a
// string and a drop has no accept at all, so keeping the extensions here
// is what stops those paths from drifting apart. Exported because App.jsx
// gates uploads a second time before calling the API -- that gate is
// derived from this list rather than restating it, which is exactly the
// drift that once let an image reach a "Choose PDF, DOCX, ..." error.
export const ACCEPTED_UPLOAD_EXTENSIONS = [
  '.pdf',
  '.docx',
  '.xlsx',
  '.csv',
  '.txt',
  '.zip',
  // Images are read with OCR on the backend (document_ingestion.py).
  '.png',
  '.jpg',
  '.jpeg',
  '.tif',
  '.tiff',
  '.bmp',
  '.webp',
]
const ACCEPTED_UPLOAD_ATTR = ACCEPTED_UPLOAD_EXTENSIONS.join(',')

/**
 * Grow-to-fit textarea, capped, then internal scroll.
 *
 * Height has to be reset to 'auto' before reading scrollHeight, otherwise
 * the previous (taller) inline height floors the measurement and the box
 * can only ever grow -- deleting text would leave it stuck tall. Runs in
 * useLayoutEffect so the resize is committed in the same frame as the
 * value change; in an effect the user would see a one-frame jump on paste.
 */
function useAutoGrow(ref, value) {
  const resize = useCallback(() => {
    const el = ref.current
    if (!el) return
    el.style.height = 'auto'
    const next = Math.min(
      Math.max(el.scrollHeight, MIN_TEXTAREA_HEIGHT),
      MAX_TEXTAREA_HEIGHT,
    )
    el.style.height = `${next}px`
    // Only scroll once capped -- below the cap the element is exactly as
    // tall as its content, so a scrollbar would be dead chrome.
    el.style.overflowY = el.scrollHeight > MAX_TEXTAREA_HEIGHT ? 'auto' : 'hidden'
  }, [ref])

  useLayoutEffect(resize, [resize, value])
  return resize
}

// Shared by HomeView (the very first message) and ThreadView (every
// follow-up in an existing conversation) -- same input, same mode
// selector, same attach/model affordances, so a follow-up feels like a
// continuation of the same composer rather than a different control.
export default function Composer({
  value,
  onChange,
  onSubmit,
  sendLocked = false,
  attachments,
  uploadState,
  onAttach,
  onClearAttachment,
  onCancelUpload,
  mode,
  onModeChange,
  modelName,
  // Whole-request lock while a real job is streaming -- distinct from
  // `sendLocked`, which covers the brief optimistic POST gap.
  modeLocked,
  // Permanent, not temporary: once a thread has sent its first message,
  // its mode is fixed for the rest of that thread's life (see App.jsx's
  // isThreadModeLocked). When true, the segmented control is replaced by
  // a plain, non-interactive label -- the other mode isn't dimmed, it's
  // gone, which is a different (and stronger) signal than the transient
  // "locked while streaming" state.
  modeFixed = false,
  // Job-active state for the send/stop toggle: while true the send button
  // becomes a stop button that cancels the run in progress, mirroring
  // ChatGPT/Claude's composer.
  jobActive = false,
  onStop,
  cancelling = false,
  // Built-in Skill catalog + current selection. Skills are execution
  // guidance only -- they never influence which agents run (see the
  // backend's skills.py); the composer treats them exactly like the mode
  // selector: part of composing the message, not a separate mode of use.
  skillCatalog = [],
  maxActiveSkills = 3,
  selectedSkillIds = [],
  onSkillsChange,
  // Opt-in budget raise for Research (backend:
  // RunPipelineRequest.deep_research) -- never auto-detected. It runs on
  // the Workforce path but is offered in both modes: switching it on moves
  // the message to Workforce. It stays on across follow-ups and is cleared
  // by a mode change or a new conversation, not by sending.
  deepResearch = false,
  onDeepResearchChange,
  deepResearchAvailable = true,
  placeholder = 'Describe what you want to accomplish…',
  autoFocus = false,
}) {
  const fileRef = useRef(null)
  const textareaRef = useRef(null)
  const uploading = uploadState.status === 'uploading'
  const locked = modeLocked ?? false
  const activeMode = MODE_OPTIONS.find((option) => option.value === mode)
  const activeModeLabel = activeMode?.label || mode

  useAutoGrow(textareaRef, value)

  const handleFile = (event) => {
    const files = event.target.files
    if (files?.length) onAttach(files)
    event.target.value = ''
  }

  // Drag-and-drop onto the composer. The browser's default for a dropped
  // file is to navigate away to it, which loses whatever was typed, so
  // both dragover and drop must preventDefault whether or not the file is
  // one we accept.
  //
  // dragCounter rather than a boolean: dragenter/dragleave also fire when
  // the pointer crosses onto a child element, so a plain flag flickers off
  // the moment the cursor moves over the textarea inside the drop zone.
  const dragCounter = useRef(0)
  const [dragActive, setDragActive] = useState(false)

  const endDrag = () => {
    dragCounter.current = 0
    setDragActive(false)
  }

  const handleDragEnter = (event) => {
    if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return
    event.preventDefault()
    dragCounter.current += 1
    setDragActive(true)
  }

  const handleDragOver = (event) => {
    if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return
    event.preventDefault()
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
  }

  const handleDragLeave = (event) => {
    if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return
    event.preventDefault()
    dragCounter.current -= 1
    if (dragCounter.current <= 0) endDrag()
  }

  const handleDrop = (event) => {
    event.preventDefault()
    endDrag()
    if (sendLocked || uploading || jobActive) return
    const dropped = Array.from(event.dataTransfer?.files || [])
    if (!dropped.length) return
    // Filtered here as well as by the input's accept=, which a drop
    // bypasses entirely -- otherwise dragging in a .exe reaches the
    // upload endpoint and comes back a 400 the user cannot act on.
    const accepted = dropped.filter((file) =>
      ACCEPTED_UPLOAD_EXTENSIONS.some((ext) =>
        file.name.toLowerCase().endsWith(ext),
      ),
    )
    if (accepted.length) onAttach(accepted)
  }

  const handleKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      if (sendLocked || uploading || !value.trim() || jobActive) return
      onSubmit()
    }
  }

  const handleModeKeyDown = (event, index) => {
    if (locked) return
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return
    event.preventDefault()
    const offset = event.key === 'ArrowRight' ? 1 : -1
    const nextIndex = (index + offset + MODE_OPTIONS.length) % MODE_OPTIONS.length
    onModeChange(MODE_OPTIONS[nextIndex].value)
    event.currentTarget.parentElement
      ?.querySelectorAll('[role="radio"]')
      [nextIndex]?.focus()
  }

  const selectedSkills = selectedSkillIds
    .map((id) => skillCatalog.find((skill) => skill.skill_id === id))
    .filter(Boolean)
  const hasAttachmentRow =
    attachments.length > 0 || uploading || selectedSkills.length > 0

  return (
    // One raised surface holds textarea + attachments + controls, so
    // attachments read as part of the input rather than as a separate
    // strip floating above it. focus-within lifts the whole shell.
    <div
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      className={cn(
        'relative rounded-xl border border-border bg-card shadow-[0_1px_2px_rgba(0,0,0,0.28),0_8px_24px_-12px_rgba(0,0,0,0.5)] transition-[border-color,box-shadow] duration-150',
        // Two-hue focus glow (same primary/accent-secondary pair as the
        // brand mark) instead of a flat ring -- the one moment the
        // composer is the center of attention gets the brand's signature,
        // everywhere else stays quiet.
        'focus-within:border-ring/70 focus-within:shadow-[0_1px_2px_rgba(0,0,0,0.28),0_8px_24px_-12px_rgba(0,0,0,0.5),0_0_0_1px_hsl(var(--primary)/0.25),0_0_28px_-6px_hsl(var(--accent-secondary)/0.35)]',
      )}
    >
      {dragActive && (
        /* Covers the shell while a file is over it. pointer-events-none is
           what keeps it from stealing the drop from the div underneath. */
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-xl border-2 border-dashed border-primary/60 bg-card/85">
          <span className="flex items-center gap-2 text-[13px] font-medium text-foreground">
            <Paperclip className="h-4 w-4" aria-hidden="true" />
            Drop to attach
          </span>
        </div>
      )}

      <textarea
        ref={textareaRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        aria-label="Task description"
        autoFocus={autoFocus}
        rows={1}
        style={{ height: MIN_TEXTAREA_HEIGHT }}
        className="scrollbar-slim block w-full resize-none bg-transparent px-4 pt-3.5 text-[15px] leading-relaxed text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-0 focus-visible:ring-offset-0"
      />

      {hasAttachmentRow && (
        <div className="flex flex-wrap gap-1.5 px-3 pb-1">
          {/* Skill chips share the attachment row rather than getting a
              band of their own: both answer "what is going with this
              message", and two stacked strips would make a two-line
              composer out of an ordinary send. Tinted with the accent
              colour so a skill reads as active rather than attached. */}
          {selectedSkills.map((skill) => (
            <span
              key={skill.skill_id}
              title={skill.description}
              className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-link/25 bg-link/10 py-1 pl-2.5 pr-1 text-xs text-link"
            >
              <span className="truncate">{skill.name}</span>
              <button
                type="button"
                onClick={() =>
                  onSkillsChange?.(
                    selectedSkillIds.filter((id) => id !== skill.skill_id),
                  )
                }
                title={`Remove ${skill.name}`}
                aria-label={`Remove skill ${skill.name}`}
                className="rounded-sm p-0.5 transition-colors duration-150 hover:bg-link/20"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
          {attachments.map((attachment) => (
            <span
              key={attachment.file_id}
              className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-border bg-muted py-1 pl-2.5 pr-1 text-xs text-foreground"
            >
              <span className="truncate">
                {attachment.filename}
                {attachment.kind === 'zip'
                  ? ` · ${attachment.workspace_files?.length || 0} files`
                  : ''}
              </span>
              <button
                type="button"
                onClick={() => onClearAttachment(attachment.file_id)}
                title={`Remove ${attachment.filename}`}
                aria-label={`Remove attachment ${attachment.filename}`}
                className="rounded-sm p-0.5 text-muted-foreground transition-colors duration-150 hover:bg-accent hover:text-foreground"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
          {uploading && (
            <span
              role="status"
              className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-dashed border-border bg-muted/50 py-1 pl-2.5 pr-1 text-xs text-muted-foreground"
            >
              <span
                className="h-3 w-3 shrink-0 animate-spin rounded-full border-[1.5px] border-muted-foreground/30 border-t-muted-foreground"
                aria-hidden="true"
              />
              <span className="truncate">
                Uploading {uploadState.filenames?.length || 0}{' '}
                {uploadState.filenames?.length === 1 ? 'file' : 'files'}…
              </span>
              <button
                type="button"
                onClick={onCancelUpload}
                title="Cancel upload"
                aria-label="Cancel upload"
                className="rounded-sm p-0.5 transition-colors duration-150 hover:bg-accent hover:text-foreground"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          )}
        </div>
      )}

      <div className="flex items-center justify-between gap-2 px-2.5 pb-2.5 pt-1.5">
        <div className="flex min-w-0 items-center gap-1.5">
          <input
            ref={fileRef}
            type="file"
            hidden
            multiple
            accept={ACCEPTED_UPLOAD_ATTR}
            onChange={handleFile}
          />
          {/* The two disabled placeholder buttons that used to sit here
              (Toolkits, Enhance prompt) were removed as dead controls; the
              Skills picker now occupies that slot with the same intent --
              change how the request is handled -- actually implemented. */}
          <SimpleTooltip label="Attach files or a ZIP workspace">
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              onClick={() => fileRef.current?.click()}
              aria-label={
                uploading ? 'Choose more attachments' : 'Attach files or a ZIP workspace'
              }
              aria-busy={uploading}
            >
              <Paperclip className="h-4 w-4" aria-hidden="true" />
            </Button>
          </SimpleTooltip>

          <SkillPicker
            catalog={skillCatalog}
            maxActive={maxActiveSkills}
            selected={selectedSkillIds}
            onChange={(ids) => onSkillsChange?.(ids)}
            disabled={uploading}
          />

          {modeFixed ? (
            <SimpleTooltip
              label={`This conversation runs in ${activeModeLabel} mode. Start a new chat to switch.`}
            >
              <span className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium text-muted-foreground">
                <span
                  className={cn(
                    'h-1.5 w-1.5 rounded-full',
                    mode === 'multi' ? 'bg-link' : 'bg-muted-foreground/60',
                  )}
                  aria-hidden="true"
                />
                {activeModeLabel}
              </span>
            </SimpleTooltip>
          ) : (
            /* Real segmented control, not a disguised native <select> --
               clicking it must never open the OS dropdown chrome. Locked
               for the whole life of a request (see `locked` above), not
               just while the initial POST is in flight, so mode can't be
               changed mid-stream. */
            <div
              role="radiogroup"
              aria-label="Agent mode"
              className={cn(
                'inline-flex items-center rounded-md bg-muted p-0.5',
                locked && 'opacity-60',
              )}
            >
              {MODE_OPTIONS.map((option, index) => (
                <SimpleTooltip key={option.value} label={option.hint}>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={mode === option.value}
                    tabIndex={mode === option.value ? 0 : -1}
                    disabled={locked}
                    onClick={() => !locked && onModeChange(option.value)}
                    onKeyDown={(event) => handleModeKeyDown(event, index)}
                    className={cn(
                      'rounded-sm px-2.5 py-1 text-xs font-medium transition-[background-color,color] duration-150 disabled:cursor-not-allowed',
                      mode === option.value
                        ? 'bg-card text-foreground shadow-sm'
                        : 'text-muted-foreground hover:text-foreground',
                    )}
                  >
                    {option.label}
                  </button>
                </SimpleTooltip>
              ))}
            </div>
          )}

          {/* Deep research: opt-in only, never auto-detected. It needs
              Research in the loop, so it runs on the Workforce path -- but
              hiding it in Single Agent meant switching modes first and
              discovering the option only afterwards. It is offered in both
              now, and turning it on moves the message to Workforce (see
              handleDeepResearchChange in App). Still hidden in a thread
              permanently locked to Single Agent, where that switch is not
              available: deepResearchAvailable carries that. Unlike the skill
              selection above it is NOT one-shot: it survives the send on
              purpose and is cleared by a mode change or a new conversation
              (see handleSubmit in App).

              Locked while a job streams, for the same reason and by the same
              flag as the mode control above. Without that it was a backdoor
              around the mode lock: handleDeepResearchChange also calls
              setMode('multi'), so clicking Deep mid-run flipped the very
              mode state the disabled segmented control was refusing to
              change -- and because the flag is sticky, the lit pill then
              carried into the NEXT message rather than the running one. */}
          {deepResearchAvailable !== false && (
            <SimpleTooltip
              label={
                locked
                  ? 'Deep research can’t be changed while a job is running. It applies to the next message.'
                  : deepResearch
                    ? 'Deep research is on: many more angles and sources, slower. Stays on for follow-ups. Click to turn off.'
                    : mode === 'multi'
                      ? 'Deep research: plan many more angles and open far more sources. Slower, more thorough.'
                      : 'Deep research: many more angles and sources. Switches to Workforce, which is what runs the research. Slower, more thorough.'
              }
            >
              {/* Labelled in both states. On/off has to be readable at a
                  glance -- a 15%-opacity tint on one of several ghost icon
                  buttons was not -- and an unlabelled microscope does not
                  say what it would turn on. The word stays put; only the
                  fill changes, so the control never moves under the cursor
                  and never has to be hovered to be understood. */}
              <Button
                type="button"
                variant="ghost"
                // 'xs' (h-7, text-xs) matches the Workforce segmented
                // control beside it; 'sm' stood a head taller with larger
                // text and read as a different class of control.
                size="xs"
                aria-pressed={deepResearch}
                aria-label={
                  deepResearch
                    ? 'Deep research is on'
                    : 'Turn on deep research'
                }
                disabled={locked}
                onClick={() => !locked && onDeepResearchChange?.(!deepResearch)}
                className={cn(
                  'gap-1.5 font-medium disabled:cursor-not-allowed',
                  // --primary is the one surface blue picked to keep white
                  // legible in both themes (see index.css); --link is far
                  // lighter in dark mode and would fail behind white text.
                  deepResearch &&
                    'bg-primary text-primary-foreground hover:bg-primary hover:text-primary-foreground hover:opacity-90',
                  // Same dimming as the mode segmented control above, so the
                  // two locked controls read as locked for the same reason.
                  locked && 'opacity-60',
                )}
              >
                <Microscope className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                <span>Deep</span>
              </Button>
            </SimpleTooltip>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {/* Read-only: the model is set by MODEL_NAME on the backend, so
              this reports the configured model instead of pretending to
              offer a choice the API cannot honour. Vendor-prefixed ids are
              inspector detail -- the full id stays in the execution
              panel's Usage section and in this tooltip.

              Rendered only once a job has actually reported a model. The
              placeholder it used to show ("Configured model") named
              nothing, so it was a label with no content. */}
          {modelName && (
            <SimpleTooltip label={`Model is configured server-side: ${modelName}`}>
              <span className="hidden max-w-[11rem] truncate px-1 text-xs text-muted-foreground/80 sm:block">
                {shortModelName(modelName)}
              </span>
            </SimpleTooltip>
          )}
          {/* Send/stop toggle, ChatGPT/Claude-style: while a job is
              running this becomes a stop button wired to the existing
              cancel flow, instead of the only way to cancel being a
              separate control buried under the thinking indicator. */}
          {jobActive ? (
            <Button
              type="button"
              size="icon-sm"
              onClick={onStop}
              disabled={cancelling}
              title="Stop generating"
              aria-label={cancelling ? 'Stopping…' : 'Stop generating'}
              aria-busy={cancelling}
              className="rounded-lg"
            >
              {cancelling ? (
                <span
                  className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-primary-foreground/30 border-t-primary-foreground"
                  aria-hidden="true"
                />
              ) : (
                <StopIcon width={13} height={13} aria-hidden="true" />
              )}
            </Button>
          ) : (
            <Button
              type="button"
              size="icon-sm"
              onClick={onSubmit}
              disabled={sendLocked || uploading || !value.trim()}
              title="Run task"
              aria-label="Run task"
              className="rounded-lg transition-transform duration-150 hover:scale-105 active:scale-95"
            >
              <ArrowUpIcon width={16} height={16} aria-hidden="true" />
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
