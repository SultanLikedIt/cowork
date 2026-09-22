// Brand marks for agents that stand for an outside service (Jira, Reddit,
// Hacker News, Outlook). Unlike the line glyphs in icons.jsx these keep the
// service's own colour: a logo is only worth showing if it is recognisable
// at a glance, and a grey Reddit alien is not.
//
// Paths are from Simple Icons (https://simpleicons.org, CC0-1.0), v16.32.0.
// The trademarks belong to their owners; they are shown here only to name
// the service an agent reads from.

// Atlassian's lighter Jira blue rather than the #0052CC Simple Icons lists:
// the app is dark-first, and #0052CC on the dark card ground is ~2.3:1.
const JIRA_BLUE = '#2684FF'
const REDDIT_ORANGE = '#FF4500'
const HN_ORANGE = '#F0652F'
const OUTLOOK_BLUE = '#0078D4'

const JIRA_PATH =
  'M11.571 11.513H0a5.218 5.218 0 0 0 5.232 5.215h2.13v2.057A5.215 5.215 0 0 0 12.575 24V12.518a1.005 1.005 0 0 0-1.005-1.005zm5.723-5.756H5.736a5.215 5.215 0 0 0 5.215 5.214h2.129v2.058a5.218 5.218 0 0 0 5.215 5.214V6.758a1.001 1.001 0 0 0-1.001-1.001zM23.013 0H11.455a5.215 5.215 0 0 0 5.215 5.215h2.129v2.057A5.215 5.215 0 0 0 24 12.483V1.005A1.001 1.001 0 0 0 23.013 0Z'

const REDDIT_PATH =
  'M12 0C5.373 0 0 5.373 0 12c0 3.314 1.343 6.314 3.515 8.485l-2.286 2.286C.775 23.225 1.097 24 1.738 24H12c6.627 0 12-5.373 12-12S18.627 0 12 0Zm4.388 3.199c1.104 0 1.999.895 1.999 1.999 0 1.105-.895 2-1.999 2-.946 0-1.739-.657-1.947-1.539v.002c-1.147.162-2.032 1.15-2.032 2.341v.007c1.776.067 3.4.567 4.686 1.363.473-.363 1.064-.58 1.707-.58 1.547 0 2.802 1.254 2.802 2.802 0 1.117-.655 2.081-1.601 2.531-.088 3.256-3.637 5.876-7.997 5.876-4.361 0-7.905-2.617-7.998-5.87-.954-.447-1.614-1.415-1.614-2.538 0-1.548 1.255-2.802 2.803-2.802.645 0 1.239.218 1.712.585 1.275-.79 2.881-1.291 4.64-1.365v-.01c0-1.663 1.263-3.034 2.88-3.207.188-.911.993-1.595 1.959-1.595Zm-8.085 8.376c-.784 0-1.459.78-1.506 1.797-.047 1.016.64 1.429 1.426 1.429.786 0 1.371-.369 1.418-1.385.047-1.017-.553-1.841-1.338-1.841Zm7.406 0c-.786 0-1.385.824-1.338 1.841.047 1.017.634 1.385 1.418 1.385.785 0 1.473-.413 1.426-1.429-.046-1.017-.721-1.797-1.506-1.797Zm-3.703 4.013c-.974 0-1.907.048-2.77.135-.147.015-.241.168-.183.305.483 1.154 1.622 1.964 2.953 1.964 1.33 0 2.47-.81 2.953-1.964.057-.137-.037-.29-.184-.305-.863-.087-1.795-.135-2.769-.135Z'

// Hacker News' own mark is the Y Combinator "Y" on orange.
const HN_PATH =
  'M0 24V0h24v24H0zM6.951 5.896l4.112 7.708v5.064h1.583v-4.972l4.148-7.799h-1.749l-2.457 4.875c-.372.745-.688 1.434-.688 1.434s-.297-.708-.651-1.434L8.831 5.896h-1.88z'

const OUTLOOK_PATH =
  'M7.88 12.04q0 .45-.11.87-.1.41-.33.74-.22.33-.58.52-.37.2-.87.2t-.85-.2q-.35-.21-.57-.55-.22-.33-.33-.75-.1-.42-.1-.86t.1-.87q.1-.43.34-.76.22-.34.59-.54.36-.2.87-.2t.86.2q.35.21.57.55.22.34.31.77.1.43.1.88zM24 12v9.38q0 .46-.33.8-.33.32-.8.32H7.13q-.46 0-.8-.33-.32-.33-.32-.8V18H1q-.41 0-.7-.3-.3-.29-.3-.7V7q0-.41.3-.7Q.58 6 1 6h6.5V2.55q0-.44.3-.75.3-.3.75-.3h12.9q.44 0 .75.3.3.3.3.75V10.85l1.24.72h.01q.1.07.18.18.07.12.07.25zm-6-8.25v3h3v-3zm0 4.5v3h3v-3zm0 4.5v1.83l3.05-1.83zm-5.25-9v3h3.75v-3zm0 4.5v3h3.75v-3zm0 4.5v2.03l2.41 1.5 1.34-.8v-2.73zM9 3.75V6h2l.13.01.12.04v-2.3zM5.98 15.98q.9 0 1.6-.3.7-.32 1.19-.86.48-.55.73-1.28.25-.74.25-1.61 0-.83-.25-1.55-.24-.71-.71-1.24t-1.15-.83q-.68-.3-1.55-.3-.92 0-1.64.3-.71.3-1.2.85-.5.54-.75 1.3-.25.74-.25 1.63 0 .85.26 1.56.26.72.74 1.23.48.52 1.17.81.69.3 1.56.3zM7.5 21h12.39L12 16.08V17q0 .41-.3.7-.29.3-.7.3H7.5zm15-.13v-7.24l-5.9 3.54Z'

// Same default size as the line glyphs (icons.jsx), so a card never has to
// know which kind of mark it is holding.
const frame = { width: 18, height: 18, viewBox: '0 0 24 24', 'aria-hidden': true }

export const JiraLogo = (p) => (
  <svg {...frame} {...p}>
    <path fill={JIRA_BLUE} d={JIRA_PATH} />
  </svg>
)

export const RedditLogo = (p) => (
  <svg {...frame} {...p}>
    <path fill={REDDIT_ORANGE} d={REDDIT_PATH} />
  </svg>
)

export const HackerNewsLogo = (p) => (
  <svg {...frame} {...p}>
    <path fill={HN_ORANGE} d={HN_PATH} />
  </svg>
)

export const OutlookLogo = (p) => (
  <svg {...frame} {...p}>
    <path fill={OUTLOOK_BLUE} d={OUTLOOK_PATH} />
  </svg>
)

// The Social Agent reads both Reddit and Hacker News, so its mark is the
// pair: Reddit top-left, HN bottom-right, each at half size and meeting at
// the centre without overlapping -- an overlap would need a cut-out ring in
// the tile's background colour, which changes with the agent's state.
export const SocialLogos = (p) => (
  <svg {...frame} {...p}>
    <g transform="scale(0.5)">
      <path fill={REDDIT_ORANGE} d={REDDIT_PATH} />
    </g>
    <g transform="translate(12 12) scale(0.5)">
      <path fill={HN_ORANGE} d={HN_PATH} />
    </g>
  </svg>
)
