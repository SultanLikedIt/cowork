import * as React from 'react'

import { cn } from '../../lib/utils.js'

const Input = React.forwardRef(({ className, type = 'text', ...props }, ref) => (
  <input
    ref={ref}
    type={type}
    className={cn(
      'flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 text-sm transition-colors duration-150 placeholder:text-muted-foreground hover:border-border disabled:cursor-not-allowed disabled:opacity-50',
      className,
    )}
    {...props}
  />
))
Input.displayName = 'Input'

const Textarea = React.forwardRef(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
      'flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm transition-colors duration-150 placeholder:text-muted-foreground disabled:cursor-not-allowed disabled:opacity-50',
      className,
    )}
    {...props}
  />
))
Textarea.displayName = 'Textarea'

const Label = React.forwardRef(({ className, ...props }, ref) => (
  <label
    ref={ref}
    className={cn('text-[13px] font-medium leading-none', className)}
    {...props}
  />
))
Label.displayName = 'Label'

export { Input, Textarea, Label }
