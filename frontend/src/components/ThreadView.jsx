import { useEffect, useRef } from 'react'

import ChatExchange from './ChatExchange.jsx'
import Composer from './Composer.jsx'
import { ScrollArea } from './ui/scroll-area.jsx'

// One selected conversation rendered as a running chat log: each persisted
// job is an exchange (user message + response) in chronological order, with
// a single composer pinned below the last one for follow-ups.
export default function ThreadView({ jobs, composerProps, onSwitchToWorkforce, onRepeat }) {
  const logEndRef = useRef(null)
  const lastJobId = jobs.length > 0 ? jobs[jobs.length - 1].job_id : null
  const lastJobStatus = jobs.length > 0 ? jobs[jobs.length - 1].status : null

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
  }, [jobs.length, lastJobId, lastJobStatus, jobs[jobs.length - 1]?._optimistic])

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {jobs.length > 0 && (
        <ScrollArea className="min-h-0 flex-1">
          {/* Generous top padding and a wide gap between exchanges: the
              separation between one question and the next is carried by
              space alone, so the log needs no rules or alternating
              backgrounds to be readable. */}
          <div className="mx-auto flex w-full max-w-[46rem] flex-col gap-12 px-6 pb-10 pt-8">
            {jobs.map((job, index) => (
              <ChatExchange
                key={job.job_id}
                job={job}
                onRepeat={onRepeat}
                {...(index === jobs.length - 1
                  ? { onSwitchToWorkforce }
                  : {})}
              />
            ))}
            <div ref={logEndRef} />
          </div>
        </ScrollArea>
      )}

      {/* The composer sits on its own band rather than floating over the
          log, so long answers scroll behind a stable edge instead of
          disappearing under a translucent overlay. */}
      <div className="shrink-0 border-t border-border/60 bg-background/85 backdrop-blur-sm">
        <div className="mx-auto w-full max-w-[46rem] px-6 pb-5 pt-4">
          <Composer {...composerProps} autoFocus placeholder="Send a follow-up…" />
        </div>
      </div>
    </div>
  )
}
