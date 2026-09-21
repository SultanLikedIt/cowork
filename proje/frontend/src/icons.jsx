// Minimal line icons in the mockup's style (1.5px stroke, rounded caps).
//
// Scope note: general-purpose UI iconography now comes from lucide-react,
// which ships with shadcn and covers the whole app. What stays here is only
// what lucide does not give us in the same visual language:
//
//   * the agent-identity glyphs (see agentIcons.jsx) -- these carry
//     product identity, mapping a specific worker role to a specific mark,
//     so swapping them for generic icons would lose that association; and
//   * the two composer send/stop marks, which are drawn to match the agent
//     glyph weight rather than lucide's slightly different stroke rhythm.
//
// Everything else that used to live here was deleted with the Tailwind
// migration -- 23 exports whose only callers were the pre-migration
// components.
const base = {
  width: 18,
  height: 18,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.6,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
}

// --- agent identity (agentIcons.jsx) ---------------------------------

export const SearchIcon = (p) => (
  <svg {...base} {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </svg>
)

export const FileIcon = (p) => (
  <svg {...base} {...p}>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
    <path d="M14 3v5h5" />
  </svg>
)

export const CodeIcon = (p) => (
  <svg {...base} {...p}>
    <polyline points="16 18 22 12 16 6" />
    <polyline points="8 6 2 12 8 18" />
  </svg>
)

export const MailIcon = (p) => (
  <svg {...base} {...p}>
    <rect x="3" y="5" width="18" height="14" rx="2" />
    <path d="m3.5 7 8.5 6 8.5-6" />
  </svg>
)

export const DiscussionIcon = (p) => (
  <svg {...base} {...p}>
    <path d="M20 12a7 7 0 0 1-7 7H8l-4 3v-4.5A7 7 0 0 1 8 5h5a7 7 0 0 1 7 7z" />
    <path d="M8.5 10.5h7" />
    <path d="M8.5 14h4" />
  </svg>
)

// A task clipboard: the Jira Agent reads tickets.
export const TaskIcon = (p) => (
  <svg {...base} {...p}>
    <rect x="5" y="4" width="14" height="17" rx="2" />
    <path d="M9 4.5V3h6v1.5" />
    <path d="m9 13 2 2 4-4" />
  </svg>
)

export const BotIcon = (p) => (
  <svg {...base} {...p}>
    <rect x="5" y="9" width="14" height="11" rx="3" />
    <path d="M12 9V5" />
    <circle cx="12" cy="3.5" r="1.5" />
    <circle cx="9" cy="14.5" r="1.2" fill="currentColor" />
    <circle cx="15" cy="14.5" r="1.2" fill="currentColor" />
    <path d="M2 13h3M19 13h3" />
  </svg>
)

// --- composer send / stop --------------------------------------------

export const ArrowUpIcon = (p) => (
  <svg {...base} {...p}>
    <path d="M12 19V5M6 11l6-6 6 6" />
  </svg>
)

export const StopIcon = (p) => (
  <svg {...base} {...p}>
    <rect x="6" y="6" width="12" height="12" rx="2" />
  </svg>
)
