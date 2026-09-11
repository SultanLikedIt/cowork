import * as React from 'react'
import * as DropdownMenuPrimitive from '@radix-ui/react-dropdown-menu'
import { Check } from 'lucide-react'

import { cn } from '../../lib/utils.js'

const DropdownMenu = DropdownMenuPrimitive.Root
const DropdownMenuTrigger = DropdownMenuPrimitive.Trigger
const DropdownMenuGroup = DropdownMenuPrimitive.Group
const DropdownMenuPortal = DropdownMenuPrimitive.Portal

const DropdownMenuContent = React.forwardRef(
  ({ className, sideOffset = 6, align = 'end', ...props }, ref) => (
    <DropdownMenuPrimitive.Portal>
      <DropdownMenuPrimitive.Content
        ref={ref}
        align={align}
        sideOffset={sideOffset}
        className={cn(
          'z-50 min-w-[10rem] overflow-hidden rounded-lg border border-border bg-popover p-1 text-popover-foreground shadow-xl data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95',
          className,
        )}
        {...props}
      />
    </DropdownMenuPrimitive.Portal>
  ),
)
DropdownMenuContent.displayName = DropdownMenuPrimitive.Content.displayName

const DropdownMenuItem = React.forwardRef(
  ({ className, inset, ...props }, ref) => (
    <DropdownMenuPrimitive.Item
      ref={ref}
      className={cn(
        'relative flex cursor-pointer select-none items-center gap-2 rounded-md px-2 py-1.5 text-[13px] outline-none transition-colors duration-150 focus:bg-accent focus:text-accent-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-50 [&_svg]:h-4 [&_svg]:w-4 [&_svg]:shrink-0 [&_svg]:text-muted-foreground',
        inset && 'pl-8',
        className,
      )}
      {...props}
    />
  ),
)
DropdownMenuItem.displayName = DropdownMenuPrimitive.Item.displayName

// Multi-select row. `onSelect` is suppressed so picking one item does not
// close the menu -- selecting several skills in a row is the normal case,
// and a menu that closed after each pick would make that four round trips.
const DropdownMenuCheckboxItem = React.forwardRef(
  ({ className, children, checked, disabled, ...props }, ref) => (
    <DropdownMenuPrimitive.CheckboxItem
      ref={ref}
      checked={checked}
      disabled={disabled}
      onSelect={(event) => event.preventDefault()}
      className={cn(
        'relative flex cursor-pointer select-none items-start gap-2.5 rounded-md px-2 py-2 text-[13px] outline-none transition-colors duration-150 focus:bg-accent data-[disabled]:pointer-events-none data-[disabled]:opacity-40',
        className,
      )}
      {...props}
    >
      <span
        aria-hidden="true"
        className={cn(
          'mt-[3px] flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-[4px] border transition-colors duration-150',
          checked
            ? 'border-primary bg-primary text-primary-foreground'
            : 'border-border',
        )}
      >
        <DropdownMenuPrimitive.ItemIndicator>
          <Check className="h-2.5 w-2.5" strokeWidth={3.5} />
        </DropdownMenuPrimitive.ItemIndicator>
      </span>
      {children}
    </DropdownMenuPrimitive.CheckboxItem>
  ),
)
DropdownMenuCheckboxItem.displayName =
  DropdownMenuPrimitive.CheckboxItem.displayName

const DropdownMenuLabel = React.forwardRef(({ className, ...props }, ref) => (
  <DropdownMenuPrimitive.Label
    ref={ref}
    className={cn(
      'px-2 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground',
      className,
    )}
    {...props}
  />
))
DropdownMenuLabel.displayName = DropdownMenuPrimitive.Label.displayName

const DropdownMenuSeparator = React.forwardRef(({ className, ...props }, ref) => (
  <DropdownMenuPrimitive.Separator
    ref={ref}
    className={cn('-mx-1 my-1 h-px bg-border', className)}
    {...props}
  />
))
DropdownMenuSeparator.displayName = DropdownMenuPrimitive.Separator.displayName

export {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuGroup,
  DropdownMenuPortal,
}
