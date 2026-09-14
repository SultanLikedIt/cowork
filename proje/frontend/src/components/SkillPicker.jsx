import { Sparkles } from 'lucide-react'

import { cn } from '../lib/utils.js'
import { Button } from './ui/button.jsx'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from './ui/dropdown-menu.jsx'
import { SimpleTooltip } from './ui/tooltip.jsx'

/**
 * The Skills control in the composer.
 *
 * Deliberately a menu, not a panel: a skill is a capability you pick on the
 * way to sending a message, in the same gesture as attaching a file or
 * choosing a mode -- not something you configure. The catalog is
 * product-owned and read-only, so there is nothing here to edit and no
 * "manage skills" affordance to lead anyone into one.
 *
 * It occupies the slot the disabled "Enhance prompt" placeholder used to
 * sit in, which is the same intent (change how the request is handled)
 * finally backed by something real.
 */
export default function SkillPicker({
  catalog = [],
  maxActive = 3,
  selected = [],
  onChange,
  disabled = false,
}) {
  if (catalog.length === 0) return null

  const selectedSet = new Set(selected)
  const atCap = selectedSet.size >= maxActive

  const toggle = (skillId) => {
    if (selectedSet.has(skillId)) {
      onChange(selected.filter((id) => id !== skillId))
      return
    }
    if (atCap) return
    // Catalog order, not click order -- the same set of skills then always
    // produces the same chip order and the same server-side prompt block.
    onChange(
      catalog
        .map((skill) => skill.skill_id)
        .filter((id) => selectedSet.has(id) || id === skillId),
    )
  }

  return (
    <DropdownMenu>
      <SimpleTooltip label="Skills — methods applied to how the work is done">
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            disabled={disabled}
            aria-label="Choose skills"
            className={cn(
              'data-[state=open]:bg-accent data-[state=open]:text-foreground',
              selectedSet.size > 0 && 'text-link hover:text-link',
            )}
          >
            <Sparkles className="h-4 w-4" aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
      </SimpleTooltip>

      {/* 13 two-line rows outgrow a laptop viewport; cap the height to the
          space Radix reports and let the list scroll instead of clipping. */}
      <DropdownMenuContent
        align="start"
        className="w-[19rem] p-1.5 max-h-[min(34rem,var(--radix-dropdown-menu-content-available-height))] overflow-y-auto"
      >
        <div className="flex items-baseline justify-between gap-2 px-2 pb-1.5 pt-1">
          <span className="eyebrow">Skills</span>
          <span className="text-[11px] tabular-nums text-muted-foreground/70">
            {selectedSet.size}/{maxActive}
          </span>
        </div>
        <p className="px-2 pb-2 text-xs leading-relaxed text-muted-foreground">
          Applied to how the work is carried out. They do not change which
          agents run.
        </p>
        {catalog.map((skill) => {
          const checked = selectedSet.has(skill.skill_id)
          return (
            <DropdownMenuCheckboxItem
              key={skill.skill_id}
              checked={checked}
              // At the cap, the unselected rows are disabled rather than
              // hidden: the catalog stays legible, and the reason you
              // cannot add a fourth is visible in the counter above.
              disabled={!checked && atCap}
              onCheckedChange={() => toggle(skill.skill_id)}
            >
              <span className="flex min-w-0 flex-col gap-0.5">
                <span
                  className={cn(
                    'text-[13px] leading-5',
                    checked ? 'font-medium text-foreground' : 'text-foreground/90',
                  )}
                >
                  {skill.name}
                </span>
                <span className="text-xs leading-snug text-muted-foreground">
                  {skill.description}
                </span>
              </span>
            </DropdownMenuCheckboxItem>
          )
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
