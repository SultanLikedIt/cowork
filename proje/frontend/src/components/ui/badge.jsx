import * as React from 'react'
import { cva } from 'class-variance-authority'

import { cn } from '../../lib/utils.js'

// Tinted chips use --link rather than --primary for their text: the button
// blue is deliberately dark enough for white to sit on it, which makes it
// unreadable as 11px text on a near-black chip. See index.css.
const badgeVariants = cva(
  'inline-flex items-center gap-1.5 rounded-sm border px-1.5 py-px text-[11px] font-medium leading-[18px] transition-colors duration-150',
  {
    variants: {
      variant: {
        default: 'border-transparent bg-link/12 text-link',
        secondary: 'border-transparent bg-secondary text-muted-foreground',
        outline: 'border-border text-muted-foreground',
        success: 'border-transparent bg-success/12 text-success',
        warning: 'border-transparent bg-warning/12 text-warning',
        destructive: 'border-transparent bg-destructive/12 text-destructive',
        // Live/in-progress. The dot, not the chip, carries the motion.
        running: 'border-transparent bg-link/12 text-link',
        // Counts next to a section label: a number, not a status, so it
        // stays neutral and never competes with a real signal colour.
        count: 'border-transparent bg-muted px-1.5 text-muted-foreground tabular-nums',
      },
    },
    defaultVariants: { variant: 'default' },
  },
)

function Badge({ className, variant, ...props }) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}

/**
 * Fixed slot for the leading status mark on a list row.
 *
 * Every such row in the app -- sidebar conversations, panel tasks, the
 * chat's run breakdown -- pairs a small mark with a 13px title that may
 * wrap. Sizing the slot to that title's first line box (h-5 = the 20px
 * line of `text-[13px] leading-5`) makes the mark centre on the first
 * line at any content length, and centring the mark *inside* the slot
 * makes a 6px dot and a 14px icon occupy the same width. That is what
 * replaced the hand-tuned `mt-[7px]` / `mt-[3px]` / `mx-[5px]` margins
 * these marks used to carry -- four different magic numbers doing one
 * job, none of them on the spacing scale.
 */
function MarkSlot({ className, children }) {
  return (
    <span
      aria-hidden="true"
      className={cn('flex h-5 w-4 shrink-0 items-center justify-center', className)}
    >
      {children}
    </span>
  )
}

/** Small status dot, sized to sit inline with badge/label text. */
function StatusDot({ className, pulse = false, ...props }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-current',
        pulse && 'animate-pulse-soft',
        className,
      )}
      {...props}
    />
  )
}

export { Badge, MarkSlot, StatusDot, badgeVariants }
