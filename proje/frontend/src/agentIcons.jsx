import {
  BotIcon,
  CodeIcon,
  FileIcon,
  DiscussionIcon,
  MailIcon,
  SearchIcon,
  TaskIcon,
} from './icons.jsx'

const BUILT_IN_AGENT_ICONS = new Map([
  ['research agent', SearchIcon],
  ['research', SearchIcon],
  ['document agent', FileIcon],
  ['document', FileIcon],
  ['coding agent', CodeIcon],
  ['coding', CodeIcon],
  ['mail agent', MailIcon],
  ['mail', MailIcon],
  ['social agent', DiscussionIcon],
  ['social', DiscussionIcon],
  ['jira agent', TaskIcon],
  ['jira', TaskIcon],
])

export function iconForAgent(name) {
  const key = typeof name === 'string' ? name.trim().toLowerCase() : ''
  return BUILT_IN_AGENT_ICONS.get(key) || BotIcon
}

export function AgentIcon({ name, ...props }) {
  const Icon = iconForAgent(name)
  return <Icon {...props} />
}
