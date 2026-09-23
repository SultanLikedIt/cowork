import { BotIcon, CodeIcon, FileIcon, LibraryIcon, SearchIcon } from './icons.jsx'
import { GitHubLogo, JiraLogo, OutlookLogo, SocialLogos } from './brandLogos.jsx'

// Agents that work inside this app get a line glyph; agents that read from
// an outside service get that service's logo (brandLogos.jsx).
const BUILT_IN_AGENT_ICONS = new Map([
  ['research agent', SearchIcon],
  ['research', SearchIcon],
  ['document agent', FileIcon],
  ['document', FileIcon],
  ['coding agent', CodeIcon],
  ['coding', CodeIcon],
  ['knowledge agent', LibraryIcon],
  ['knowledge', LibraryIcon],
  ['mail agent', OutlookLogo],
  ['mail', OutlookLogo],
  ['social agent', SocialLogos],
  ['social', SocialLogos],
  ['jira agent', JiraLogo],
  ['jira', JiraLogo],
  ['github agent', GitHubLogo],
  ['github', GitHubLogo],
])

export function iconForAgent(name) {
  const key = typeof name === 'string' ? name.trim().toLowerCase() : ''
  return BUILT_IN_AGENT_ICONS.get(key) || BotIcon
}

export function AgentIcon({ name, ...props }) {
  const Icon = iconForAgent(name)
  return <Icon {...props} />
}
