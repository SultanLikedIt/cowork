import * as React from 'react'
import * as ProgressPrimitive from '@radix-ui/react-progress'
import { cva } from 'class-variance-authority'

import { cn } from '../../lib/utils.js'

const indicatorVariants = cva('h-full w-full flex-1 transition-transform duration-300', {
  variants: {
    tone: {
      default: 'bg-primary',
      success: 'bg-success',
      warning: 'bg-warning',
      destructive: 'bg-destructive',
      muted: 'bg-muted-foreground/50',
    },
  },
  defaultVariants: { tone: 'default' },
})

const Progress = React.forwardRef(
  ({ className, value = 0, tone, indeterminate = false, ...props }, ref) => (
    <ProgressPrimitive.Root
      ref={ref}
      value={indeterminate ? null : value}
      className={cn(
        'relative h-1.5 w-full overflow-hidden rounded-full bg-muted',
        className,
      )}
      {...props}
    >
      {indeterminate ? (
        // No determinate value to show (a task that is running but reports
        // no percentage): a travelling sheen reads as "working" without
        // implying a completion fraction we do not have.
        <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-primary/50 to-transparent" />
      ) : (
        <ProgressPrimitive.Indicator
          className={cn(indicatorVariants({ tone }))}
          style={{ transform: `translateX(-${100 - (value || 0)}%)` }}
        />
      )}
    </ProgressPrimitive.Root>
  ),
)
Progress.displayName = ProgressPrimitive.Root.displayName

export { Progress }
