import { useEffect, useMemo, useRef, useState } from 'react'
import { Paperclip, Sparkles, X } from 'lucide-react'

import {
  createScheduledTask,
  parseSchedule,
  previewSchedule,
  updateScheduledTask,
  uploadFiles,
} from '../api.js'
import { cn } from '../lib/utils.js'
import {
  CONDITIONS,
  DEFAULT_TIMEZONE,
  PROMPT_VARIABLES,
  SCHEDULE_KINDS,
  WEEKDAYS,
  chainDescendantIds,
  draftToPayload,
  formatDateTime,
  newTaskDraft,
  scheduleToTrigger,
  triggerToSchedule,
} from '../schedule.js'
import { ACCEPTED_UPLOAD_EXTENSIONS } from './Composer.jsx'
import SkillPicker from './SkillPicker.jsx'
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

const MAX_ATTACHED_FILES = 10

const selectClass =
  'flex h-9 w-full rounded-md border border-input bg-background px-3 text-sm transition-colors duration-150 hover:border-border focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40'

function Section({ title, description, children }) {
  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <h3 className="eyebrow">{title}</h3>
        {description && (
          <p className="text-xs leading-relaxed text-muted-foreground">{description}</p>
        )}
      </div>
      {children}
    </section>
  )
}

function Segmented({ value, options, onChange, disabled = false }) {
  return (
    <div className="inline-flex rounded-md border border-border bg-muted/40 p-0.5" role="radiogroup">
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          disabled={disabled}
          onClick={() => onChange(option.value)}
          className={cn(
            'rounded px-3 py-1.5 text-[13px] font-medium transition-colors duration-150',
            value === option.value
              ? 'bg-card text-foreground shadow-sm'
              : 'text-muted-foreground hover:text-foreground',
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

function CheckRow({ id, checked, onChange, disabled = false, title, description }) {
  return (
    <label
      htmlFor={id}
      className={cn('flex items-start gap-3', disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer')}
    >
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-0.5 h-4 w-4 shrink-0 accent-[hsl(var(--primary))]"
      />
      <span className="flex flex-col gap-0.5">
        <span className="text-[13px] font-medium leading-5">{title}</span>
        {description && (
          <span className="text-xs leading-relaxed text-muted-foreground">{description}</span>
        )}
      </span>
    </label>
  )
}

export default function ScheduleEditorDialog({
  open,
  task = null,
  initialDraft = null,
  workspaceId,
  skillCatalog = [],
  maxActiveSkills = 3,
  tasks = [],
  onClose,
  onSaved,
}) {
  const [draft, setDraft] = useState(() => newTaskDraft())
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [preview, setPreview] = useState({ runs: [], error: null })
  const [scheduleText, setScheduleText] = useState('')
  const [parsing, setParsing] = useState(false)
  const [parseNote, setParseNote] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState(null)
  const fileInputRef = useRef(null)
  const promptRef = useRef(null)
  const nameRef = useRef(null)
  const timezone = task?.timezone || DEFAULT_TIMEZONE

  useEffect(() => {
    if (!open) return
    setDraft(initialDraft || newTaskDraft())
    setError(null)
    setScheduleText('')
    setParseNote(null)
    setUploadError(null)
  }, [open, initialDraft])

  // A task may follow any task in the workspace except itself and the tasks
  // that already follow it (that link would loop).
  const upstreamOptions = useMemo(() => {
    const excluded = chainDescendantIds(tasks, task?.task_id)
    if (task?.task_id) excluded.add(task.task_id)
    return tasks.filter((candidate) => !excluded.has(candidate.task_id))
  }, [tasks, task])

  const update = (changes) => setDraft((previous) => ({ ...previous, ...changes }))
  const updateSchedule = (changes) =>
    setDraft((previous) => ({ ...previous, schedule: { ...previous.schedule, ...changes } }))
  const updateWatch = (changes) =>
    setDraft((previous) => ({ ...previous, watch: { ...previous.watch, ...changes } }))

  // Upcoming runs, straight from the server's own cron evaluation so the
  // preview can never disagree with when the task actually fires.
  const trigger = scheduleToTrigger(draft.schedule, timezone)
  const triggerKey = JSON.stringify(trigger)
  useEffect(() => {
    if (!open) return undefined
    if (trigger.error) {
      setPreview({ runs: [], error: trigger.error })
      return undefined
    }
    if (trigger.trigger_kind === 'after_task') {
      setPreview({ runs: [], error: null })
      return undefined
    }
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      previewSchedule(trigger, { signal: controller.signal })
        .then((data) => setPreview({ runs: data.upcoming_runs || [], error: null }))
        .catch((err) => {
          if (err?.name !== 'AbortError') setPreview({ runs: [], error: err.message })
        })
    }, 300)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
    // triggerKey stands in for the trigger object, which is rebuilt each render.
  }, [open, triggerKey])

  const insertVariable = (token) => {
    const element = promptRef.current
    const text = draft.prompt
    const start = element ? element.selectionStart : text.length
    const end = element ? element.selectionEnd : text.length
    update({ prompt: `${text.slice(0, start)}${token}${text.slice(end)}` })
    window.requestAnimationFrame(() => {
      if (!element) return
      element.focus()
      element.setSelectionRange(start + token.length, start + token.length)
    })
  }

  const applyScheduleText = async () => {
    const text = scheduleText.trim()
    if (!text || parsing) return
    setParsing(true)
    setParseNote(null)
    try {
      const parsed = await parseSchedule({ text, timezone })
      update({ schedule: triggerToSchedule(parsed, timezone) })
      setParseNote({ ok: true, text: parsed.description || 'Schedule set.' })
    } catch (err) {
      setParseNote({ ok: false, text: err.message })
    } finally {
      setParsing(false)
    }
  }

  const attachFiles = async (fileList) => {
    const picked = Array.from(fileList || [])
    if (picked.length === 0 || uploading) return
    const room = MAX_ATTACHED_FILES - draft.attachedFiles.length
    if (room <= 0) {
      setUploadError(`A task can keep at most ${MAX_ATTACHED_FILES} files.`)
      return
    }
    setUploading(true)
    setUploadError(null)
    try {
      const uploaded = await uploadFiles(picked.slice(0, room))
      setDraft((previous) => ({
        ...previous,
        attachedFiles: [
          ...previous.attachedFiles,
          ...uploaded.map((file) => ({ file_id: file.file_id, filename: file.filename })),
        ],
      }))
      if (picked.length > room) {
        setUploadError(`Only the first ${room} file${room === 1 ? '' : 's'} were added.`)
      }
    } catch (err) {
      setUploadError(err.message || 'Upload failed.')
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const submit = async (event) => {
    event.preventDefault()
    if (saving) return
    const result = draftToPayload(draft, timezone)
    if (result.error) {
      setError(result.error)
      return
    }
    setSaving(true)
    setError(null)
    try {
      const saved = task
        ? await updateScheduledTask(task.task_id, result.payload)
        : await createScheduledTask({ ...result.payload, workspace_id: workspaceId })
      onSaved?.(saved)
    } catch (err) {
      setError(err.message || 'Unable to save this task.')
    } finally {
      setSaving(false)
    }
  }

  const { schedule } = draft
  const isFollower = schedule.kind === 'after_task'
  const upstreamTask = tasks.find((item) => item.task_id === schedule.upstreamTaskId) || null
  const upstreamIsWatch = Boolean(upstreamTask?.watch)
  const watchUrlsOn = draft.watch.strategies.includes('url_hash')
  const toggleStrategy = (strategy, on) =>
    updateWatch({
      strategies: on
        ? [...new Set([...draft.watch.strategies, strategy])]
        : draft.watch.strategies.filter((item) => item !== strategy),
    })

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        className="scrollbar-slim max-h-[90vh] max-w-2xl overflow-y-auto"
        onOpenAutoFocus={(event) => {
          event.preventDefault()
          nameRef.current?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle>{task ? 'Edit scheduled task' : 'New scheduled task'}</DialogTitle>
          <DialogDescription>
            The agent runs this on its own and posts each result as a new chat in this
            workspace.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={submit} className="flex flex-col gap-8">
          <Section title="Task">
            <div className="flex flex-col gap-2">
              <Label htmlFor="schedule-name">Name</Label>
              <Input
                id="schedule-name"
                ref={nameRef}
                value={draft.name}
                maxLength={120}
                onChange={(event) => update({ name: event.target.value })}
                placeholder="e.g. Weekly sector brief"
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="schedule-prompt">What should the agent do?</Label>
              <Textarea
                id="schedule-prompt"
                ref={promptRef}
                value={draft.prompt}
                maxLength={4000}
                rows={5}
                onChange={(event) => update({ prompt: event.target.value })}
                placeholder="e.g. {hafta} döneminde sektördeki önemli gelişmeleri araştır ve PDF rapor hazırla."
                className="resize-y"
              />
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-muted-foreground">Insert:</span>
                {PROMPT_VARIABLES.map((variable) => (
                  <button
                    key={variable.token}
                    type="button"
                    title={`Replaced at run time with ${variable.hint}`}
                    onClick={() => insertVariable(variable.token)}
                    className="rounded-sm border border-border/70 px-1.5 py-px font-mono text-[11px] text-muted-foreground transition-colors duration-150 hover:border-border hover:text-foreground"
                  >
                    {variable.token}
                  </button>
                ))}
              </div>
            </div>
          </Section>

          <Section title="Agent">
            <div className="flex flex-wrap items-center gap-4">
              <Segmented
                value={draft.mode}
                onChange={(mode) => update({ mode, deepResearch: mode === 'multi' && draft.deepResearch })}
                options={[
                  { value: 'multi', label: 'Workforce' },
                  { value: 'single', label: 'Single Agent' },
                ]}
              />
              <SkillPicker
                catalog={skillCatalog}
                maxActive={maxActiveSkills}
                selected={draft.skillIds}
                onChange={(skillIds) => update({ skillIds })}
              />
              {draft.skillIds.length > 0 && (
                <span className="text-xs text-muted-foreground">
                  {draft.skillIds.length} skill{draft.skillIds.length === 1 ? '' : 's'}
                </span>
              )}
            </div>
            <CheckRow
              id="schedule-deep"
              checked={draft.mode === 'multi' && draft.deepResearch}
              disabled={draft.mode !== 'multi'}
              onChange={(deepResearch) => update({ deepResearch })}
              title="Deep research"
              description="Many more searches and pages per run. Limited to once a day."
            />
            {draft.mode === 'single' && (
              <p className="text-xs leading-relaxed text-muted-foreground">
                Single Agent has no web access and cannot create files — most scheduled
                reports need Workforce.
              </p>
            )}
          </Section>

          <Section
            title="Schedule"
            description={isFollower ? 'Starts when another task in this workspace finishes.' : `Times are in ${timezone}.`}
          >
            <div className={cn('flex flex-col gap-2', isFollower && 'hidden')}>
              <Label htmlFor="schedule-text">Describe it</Label>
              <div className="flex gap-2">
                <Input
                  id="schedule-text"
                  value={scheduleText}
                  maxLength={300}
                  onChange={(event) => setScheduleText(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      applyScheduleText()
                    }
                  }}
                  placeholder="e.g. her pazartesi sabah 9, hafta içi 08:30, ayın 1'inde"
                />
                <Button
                  type="button"
                  variant="outline"
                  onClick={applyScheduleText}
                  disabled={!scheduleText.trim() || parsing}
                  className="shrink-0"
                >
                  <Sparkles className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
                  {parsing ? 'Reading…' : 'Set'}
                </Button>
              </div>
              {parseNote && (
                <p
                  className={cn('text-xs', parseNote.ok ? 'text-muted-foreground' : 'text-destructive')}
                  role={parseNote.ok ? 'status' : 'alert'}
                >
                  {parseNote.text}
                </p>
              )}
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="flex flex-col gap-2">
                <Label htmlFor="schedule-kind">Repeats</Label>
                <select
                  id="schedule-kind"
                  className={selectClass}
                  value={schedule.kind}
                  onChange={(event) => {
                    const kind = event.target.value
                    const time = schedule.time || '09:00'
                    const defaults = {
                      daily: { kind, time },
                      weekdays: { kind, time },
                      weekly: { kind, time, weekday: schedule.weekday ?? 1 },
                      monthly: { kind, time, dayOfMonth: schedule.dayOfMonth ?? 1 },
                      once: { kind, time, date: schedule.date || '' },
                      after_task: {
                        kind,
                        upstreamTaskId: schedule.upstreamTaskId || '',
                        condition: schedule.condition || 'success',
                      },
                      custom: { kind, cron: trigger.cron_expr || '0 9 * * 1' },
                    }
                    update({ schedule: defaults[kind] })
                  }}
                >
                  {SCHEDULE_KINDS.map((kind) => (
                    <option key={kind.value} value={kind.value}>
                      {kind.label}
                    </option>
                  ))}
                </select>
              </div>

              {schedule.kind === 'weekly' && (
                <div className="flex flex-col gap-2">
                  <Label htmlFor="schedule-weekday">Day</Label>
                  <select
                    id="schedule-weekday"
                    className={selectClass}
                    value={schedule.weekday}
                    onChange={(event) => updateSchedule({ weekday: Number(event.target.value) })}
                  >
                    {WEEKDAYS.map((day) => (
                      <option key={day.value} value={day.value}>
                        {day.label}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {schedule.kind === 'monthly' && (
                <div className="flex flex-col gap-2">
                  <Label htmlFor="schedule-dom">Day of month</Label>
                  <Input
                    id="schedule-dom"
                    type="number"
                    min={1}
                    max={28}
                    value={schedule.dayOfMonth}
                    onChange={(event) => updateSchedule({ dayOfMonth: Number(event.target.value) })}
                  />
                </div>
              )}
              {schedule.kind === 'once' && (
                <div className="flex flex-col gap-2">
                  <Label htmlFor="schedule-date">Date</Label>
                  <Input
                    id="schedule-date"
                    type="date"
                    value={schedule.date || ''}
                    onChange={(event) => updateSchedule({ date: event.target.value })}
                  />
                </div>
              )}
              {isFollower && (
                <div className="flex flex-col gap-2">
                  <Label htmlFor="schedule-upstream">Runs after</Label>
                  <select
                    id="schedule-upstream"
                    className={selectClass}
                    value={schedule.upstreamTaskId || ''}
                    onChange={(event) => {
                      const nextUpstream = tasks.find((item) => item.task_id === event.target.value)
                      updateSchedule({
                        upstreamTaskId: event.target.value,
                        // "change" only means something after a watch task.
                        condition:
                          schedule.condition === 'change' && !nextUpstream?.watch
                            ? 'success'
                            : schedule.condition,
                      })
                    }}
                  >
                    <option value="" disabled>
                      {upstreamOptions.length ? 'Choose a task…' : 'No other tasks yet'}
                    </option>
                    {upstreamOptions.map((candidate) => (
                      <option key={candidate.task_id} value={candidate.task_id}>
                        {candidate.name}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {isFollower ? null : schedule.kind === 'custom' ? (
                <div className="flex flex-col gap-2 sm:col-span-2">
                  <Label htmlFor="schedule-cron">Cron expression</Label>
                  <Input
                    id="schedule-cron"
                    value={schedule.cron || ''}
                    onChange={(event) => updateSchedule({ cron: event.target.value })}
                    placeholder="minute hour day month weekday — e.g. 0 9 * * 1"
                    className="font-mono"
                  />
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  <Label htmlFor="schedule-time">Time</Label>
                  <Input
                    id="schedule-time"
                    type="time"
                    value={schedule.time || ''}
                    onChange={(event) => updateSchedule({ time: event.target.value })}
                  />
                </div>
              )}
            </div>

            {isFollower ? (
              <div className="flex flex-col gap-4">
                <div className="flex flex-col gap-3" role="radiogroup" aria-label="When it runs">
                  {CONDITIONS.map((option) => {
                    const disabled = option.value === 'change' && !upstreamIsWatch
                    return (
                      <label
                        key={option.value}
                        className={cn(
                          'flex items-start gap-3',
                          disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer',
                        )}
                      >
                        <input
                          type="radio"
                          name="schedule-condition"
                          value={option.value}
                          checked={(schedule.condition || 'success') === option.value}
                          disabled={disabled}
                          onChange={() => updateSchedule({ condition: option.value })}
                          className="mt-0.5 h-4 w-4 shrink-0 accent-[hsl(var(--primary))]"
                        />
                        <span className="flex flex-col gap-0.5">
                          <span className="text-[13px] font-medium leading-5">{option.label}</span>
                          <span className="text-xs leading-relaxed text-muted-foreground">
                            {disabled
                              ? 'Available when the chosen task watches for changes.'
                              : option.description}
                          </span>
                        </span>
                      </label>
                    )
                  })}
                </div>
                <div className="flex flex-col gap-3 border-t border-border/60 pt-4">
                  <CheckRow
                    id="schedule-pass-result"
                    checked={draft.passUpstreamResult}
                    onChange={(passUpstreamResult) => update({ passUpstreamResult })}
                    title="Give it that task's report"
                    description="The report is added as context, so the instructions can say “summarize the previous report”."
                  />
                  <CheckRow
                    id="schedule-pass-files"
                    checked={draft.passUpstreamFiles}
                    onChange={(passUpstreamFiles) => update({ passUpstreamFiles })}
                    title="Give it that task's files"
                    description="Files the run created (PDF, Word, Excel…) are attached, up to 10 files and 50 MB."
                  />
                </div>
                {preview.error && (
                  <p className="text-xs text-destructive" role="alert">
                    {preview.error}
                  </p>
                )}
              </div>
            ) : (
            <div className="rounded-md bg-muted/40 px-3 py-2.5">
              {preview.error ? (
                <p className="text-xs text-destructive" role="alert">
                  {preview.error}
                </p>
              ) : preview.runs.length > 0 ? (
                <div className="flex flex-col gap-1">
                  <span className="text-xs text-muted-foreground">Next runs</span>
                  <ul className="flex flex-wrap gap-x-4 gap-y-1">
                    {preview.runs.slice(0, 4).map((run) => (
                      <li key={run} className="text-[13px] tabular-nums">
                        {formatDateTime(run, timezone)}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">Checking schedule…</p>
              )}
            </div>
            )}
          </Section>

          <Section
            title="Files"
            description="Attached to every run, e.g. a template to fill in or a spreadsheet to update."
          >
            {draft.attachedFiles.length > 0 && (
              <ul className="flex flex-wrap gap-2">
                {draft.attachedFiles.map((file) => (
                  <li
                    key={file.file_id}
                    className="flex max-w-full items-center gap-1.5 rounded-md border border-border/70 bg-muted/40 py-1 pl-2.5 pr-1 text-[13px]"
                  >
                    <Paperclip className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                    <span className="truncate">{file.filename}</span>
                    <button
                      type="button"
                      onClick={() =>
                        update({
                          attachedFiles: draft.attachedFiles.filter((item) => item.file_id !== file.file_id),
                        })
                      }
                      aria-label={`Remove ${file.filename}`}
                      className="rounded p-0.5 text-muted-foreground transition-colors duration-150 hover:bg-accent hover:text-foreground"
                    >
                      <X className="h-3.5 w-3.5" aria-hidden="true" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <div className="flex items-center gap-3">
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept={ACCEPTED_UPLOAD_EXTENSIONS.join(',')}
                className="hidden"
                onChange={(event) => attachFiles(event.target.files)}
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading || draft.attachedFiles.length >= MAX_ATTACHED_FILES}
              >
                <Paperclip className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
                {uploading ? 'Uploading…' : 'Attach files'}
              </Button>
              {uploadError && (
                <span className="text-xs text-destructive" role="alert">
                  {uploadError}
                </span>
              )}
            </div>
          </Section>

          <Section title="Results">
            <CheckRow
              id="schedule-previous"
              checked={draft.includePreviousSummary}
              onChange={(includePreviousSummary) => update({ includePreviousSummary })}
              title="Focus on what is new"
              description="Each run gets a summary of the previous result, so the report highlights changes instead of repeating itself."
            />
            <CheckRow
              id="schedule-watch"
              checked={draft.watchEnabled}
              onChange={(watchEnabled) => update({ watchEnabled })}
              title="Only flag runs where something changed"
              description="Runs without a material change are recorded quietly and do not show up as new."
            />
            {draft.watchEnabled && (
              <div className="ml-7 flex flex-col gap-4 border-l border-border/70 pl-4">
                <CheckRow
                  id="watch-diff"
                  checked={draft.watch.strategies.includes('research_diff')}
                  onChange={(on) => toggleStrategy('research_diff', on)}
                  title="Compare each report with the previous one"
                  description="A short model call decides whether the new report contains a material change."
                />
                <CheckRow
                  id="watch-urls"
                  checked={watchUrlsOn}
                  onChange={(on) => toggleStrategy('url_hash', on)}
                  title="Run only when these pages change"
                  description="The pages are checked first; if none changed, the run is skipped without using the model."
                />
                {watchUrlsOn && (
                  <Textarea
                    aria-label="Pages to watch"
                    value={draft.watch.urls}
                    rows={3}
                    onChange={(event) => updateWatch({ urls: event.target.value })}
                    placeholder={'https://www.resmigazete.gov.tr/\nOne URL per line, up to 5'}
                    className="resize-none font-mono text-xs"
                  />
                )}
                <div className="flex flex-col gap-2">
                  <Label htmlFor="watch-criteria">
                    What counts as a change{' '}
                    <span className="font-normal text-muted-foreground">optional</span>
                  </Label>
                  <Input
                    id="watch-criteria"
                    value={draft.watch.criteria}
                    maxLength={1000}
                    onChange={(event) => updateWatch({ criteria: event.target.value })}
                    placeholder="e.g. new regulations affecting the construction sector"
                  />
                </div>
              </div>
            )}
          </Section>

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
            <Button type="submit" disabled={saving} aria-busy={saving}>
              {saving ? 'Saving…' : task ? 'Save changes' : 'Create task'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
