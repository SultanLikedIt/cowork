import { clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

/**
 * Merge conditional class names, with later Tailwind utilities winning
 * over earlier ones in the same group (twMerge). This is what lets a
 * caller pass `className="bg-card"` to a component whose variant already
 * sets `bg-panel` and actually get the override.
 */
export function cn(...inputs) {
  return twMerge(clsx(inputs))
}
