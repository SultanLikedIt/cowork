import { BookOpen, Check, Clock, Code2, FileText, Globe, PanelLeft, Search } from 'lucide-react'

import { BotIcon } from '../icons.jsx'
import {
  ConfluenceLogo,
  GitHubLogo,
  HackerNewsLogo,
  JiraLogo,
  OutlookLogo,
  RedditLogo,
} from '../brandLogos.jsx'
import { ERROR_JOB_STATUSES, SUCCESS_JOB_STATUSES, isActiveJob } from '../stages.js'
import { cn } from '../lib/utils.js'
import { Button } from './ui/button.jsx'
import { SimpleTooltip } from './ui/tooltip.jsx'
import Composer from './Composer.jsx'
import LibraryPanel from './LibraryPanel.jsx'
import SpaceSelector from './SpaceSelector.jsx'

const WORKFORCE_AGENTS = [
  {
    key: 'Coding',
    label: 'Coding',
    description: 'Write, debug & execute code',
    badgeClass: 'bg-emerald-500/90 text-white',
    BadgeIcon: Code2,
  },
  {
    key: 'Document',
    label: 'Document',
    description: 'Analyze, summarize & draft',
    badgeClass: 'bg-amber-500/90 text-white',
    BadgeIcon: FileText,
  },
  {
    key: 'Research',
    label: 'Research',
    description: 'Web search & source synthesis',
    badgeClass: 'bg-sky-500/90 text-white',
    BadgeIcon: Globe,
  },
]

// Built-in, but only when the backend roster has it (KNOWLEDGE_ENABLED).
// Works inside the app, so the bot + badge form like the three above rather
// than a service logo.
const KNOWLEDGE_AGENT = {
  key: 'Knowledge',
  label: 'Knowledge',
  description: 'Search your documents',
  badgeClass: 'bg-violet-500/90 text-white',
  BadgeIcon: BookOpen,
  rosterName: 'Knowledge Agent',
}

// Agents that read from an outside service, shown with that service's logo.
// Listed only when the backend roster (/workers) includes the agent behind
// them -- the same promise the right panel makes: a card here is a source
// this deployment can actually use. Reddit and Hacker News are one Social
// Agent at runtime but two sources to the person choosing what to ask.
const INTEGRATION_AGENTS = [
  {
    key: 'Outlook',
    label: 'Outlook',
    description: 'Read your inbox & sent mail',
    Logo: OutlookLogo,
    rosterName: 'Mail Agent',
  },
  {
    key: 'Reddit',
    label: 'Reddit',
    description: 'Discussions & opinions',
    Logo: RedditLogo,
    rosterName: 'Social Agent',
  },
  {
    key: 'HackerNews',
    label: 'Hacker News',
    description: 'Tech news & discussions',
    Logo: HackerNewsLogo,
    rosterName: 'Social Agent',
  },
  {
    key: 'Jira',
    label: 'Jira',
    description: 'Find & summarize your issues',
    Logo: JiraLogo,
    rosterName: 'Jira Agent',
  },
  {
    key: 'Confluence',
    label: 'Confluence',
    description: 'Find & summarize wiki pages',
    Logo: ConfluenceLogo,
    rosterName: 'Confluence Agent',
  },
  {
    key: 'GitHub',
    label: 'GitHub',
    description: 'Find & read public repositories',
    Logo: GitHubLogo,
    rosterName: 'GitHub Agent',
  },
]

// One per agent, in the roster's own order, so the row below it reads as
// "these are the three, and this is what you'd ask each of them". Two of
// these used to belong to Document while Coding had none. `agent` keys
// into WORKFORCE_AGENTS for the hover text; the pairing is carried by
// order and by there being exactly one each, not by colour.
const STARTERS = [
  {
    label: 'Write a script',
    Icon: Code2,
    prompt: 'Write a script that ',
    agent: 'Coding',
  },
  {
    label: 'Analyze a document',
    Icon: FileText,
    prompt: 'Analyze the attached document and ',
    agent: 'Document',
  },
  {
    label: 'Research a topic',
    Icon: Search,
    prompt: 'Research ',
    agent: 'Research',
  },
]

const AGENT_BY_KEY = Object.fromEntries(
  WORKFORCE_AGENTS.map((agent) => [agent.key, agent]),
)

function AgentAvatar({
  label,
  description,
  badgeClass,
  BadgeIcon,
  Logo,
  showLabel = false,
}) {
  const avatar = (
    <span
      className={cn(
        'relative flex h-10 w-10 shrink-0 items-center justify-center rounded-[11px]',
        'border border-border/60 bg-muted/60 text-foreground/80',
      )}
      aria-label={showLabel ? undefined : label}
    >
      {/* A service logo is the identity on its own; the bot + badge pair is
          for agents that work inside the app. */}
      {Logo ? (
        <Logo width={20} height={20} aria-hidden="true" />
      ) : (
        <BotIcon width={18} height={18} aria-hidden="true" />
      )}
      {!Logo && BadgeIcon && (
        <span
          className={cn(
            'absolute -right-1 -top-1 flex h-4 w-4 items-center justify-center',
            'rounded-full shadow-sm ring-2 ring-background',
            badgeClass,
          )}
          aria-hidden="true"
        >
          <BadgeIcon className="h-2.5 w-2.5" strokeWidth={2.5} />
        </span>
      )}
    </span>
  )

  // Named inline, so what an agent does is readable without hovering it.
  // Carded like the AGENTS list in the right panel, and laid out the same
  // way -- avatar left, name and line of text right -- so the two places
  // describing the same agents look related instead of like two unrelated
  // widgets. Both modes use this; the tooltip form is the fallback for any
  // caller that wants a bare avatar.
  if (showLabel) {
    return (
      <span
        className={cn(
          'flex min-w-0 flex-1 items-center gap-2.5 rounded-lg border border-border/60',
          'bg-card/50 px-3 py-2.5 text-left',
        )}
      >
        {avatar}
        <span className="flex min-w-0 flex-col">
          <span className="truncate text-[13px] font-medium leading-5 text-foreground">
            {label}
          </span>
          {description && (
            <span className="text-[11px] leading-[1.35] text-muted-foreground">
              {description}
            </span>
          )}
        </span>
      </span>
    )
  }

  return (
    <SimpleTooltip label={description ? `${label} — ${description}` : label}>
      {avatar}
    </SimpleTooltip>
  )
}

export default function HomeView({
  value,
  onChange,
  onSubmit,
  sendLocked = false,
  attachments,
  uploadState,
  onAttach,
  onClearAttachment,
  onCancelUpload,
  mode,
  onModeChange,
  modeFixed = false,
  modelName,
  skillCatalog,
  maxActiveSkills,
  selectedSkillIds,
  onSkillsChange,
  deepResearch,
  onDeepResearchChange,
  workers = [],
  workspaceName,
  workspaceId,
  workspaces,
  onSelectWorkspace,
  onManageWorkspaces,
  onCreateWorkspace,
  onToggleNav,
  navOpen = false,
  recentConversations = [],
  onSelectConversation,
}) {
  const isWorkforce = mode === 'multi'
  const showStarters = isWorkforce && !value.trim()
  const recent = recentConversations.slice(0, 5)
  const rosterNames = new Set((workers || []).map((worker) => worker?.name))
  const integrations = INTEGRATION_AGENTS.filter((agent) =>
    rosterNames.has(agent.rosterName),
  )
  const knowledgeOn = rosterNames.has(KNOWLEDGE_AGENT.rosterName)
  const builtIn = knowledgeOn ? [...WORKFORCE_AGENTS, KNOWLEDGE_AGENT] : WORKFORCE_AGENTS

  return (
    <div className="surface-grid relative flex min-h-0 flex-1 flex-col overflow-y-auto">
      {onToggleNav && (
        <div className="absolute left-4 top-4 z-10">
          <SimpleTooltip label={navOpen ? 'Hide chat list' : 'Show chat list'}>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              onClick={onToggleNav}
              aria-label={navOpen ? 'Hide chat list' : 'Show chat list'}
              aria-pressed={navOpen}
              className={cn(
                'text-muted-foreground hover:text-foreground',
                navOpen && 'bg-accent text-foreground',
              )}
            >
              <PanelLeft className="h-4 w-4" aria-hidden="true" />
            </Button>
          </SimpleTooltip>
        </div>
      )}

      <div className="relative mx-auto flex w-full max-w-[52rem] flex-1 flex-col px-6 sm:px-8">
        <div className="min-h-6 flex-[0.72]" aria-hidden="true" />

        <div className="flex w-full shrink-0 flex-col items-center">
          <SpaceSelector
            workspaceName={workspaceName}
            workspaceId={workspaceId}
            workspaces={workspaces}
            onSelectWorkspace={onSelectWorkspace}
            onManageWorkspaces={onManageWorkspaces}
            onCreateWorkspace={onCreateWorkspace}
          />

          <h2 className="mt-6 min-h-[40px] text-[28px] font-semibold leading-tight tracking-[-0.02em] text-foreground sm:min-h-[44px] sm:text-[32px]">
            {isWorkforce ? 'Cowork with Workforce' : 'Cowork with Single Agent'}
          </h2>

          {isWorkforce ? (
            // A grid rather than one row: with the service agents there can
            // be eight cards, which a single row squeezes into slivers.
            // Columns come from the column's own width (auto-fill, 11.5rem
            // minimum), not the viewport, because the sidebar changes how
            // wide this column is at any given screen size. At full width
            // that is four columns: built-in agents first, then the services.
            <div className="mt-5 grid w-full grid-cols-[repeat(auto-fill,minmax(11.5rem,1fr))] gap-2">
              {builtIn.map((agent) => (
                <AgentAvatar
                  key={agent.key}
                  label={agent.label}
                  description={agent.description}
                  badgeClass={agent.badgeClass}
                  BadgeIcon={agent.BadgeIcon}
                  showLabel
                />
              ))}
              {integrations.map((agent) => (
                <AgentAvatar
                  key={agent.key}
                  label={agent.label}
                  description={agent.description}
                  Logo={agent.Logo}
                  showLabel
                />
              ))}
            </div>
          ) : (
            // The lone Single Agent card is held to the width one grid
            // column gets, so the two modes show the same card rather than
            // a wider one-off.
            <div className="mx-auto mt-5 flex w-full sm:max-w-[15rem]">
              <AgentAvatar
                label="Single Agent"
                description="One assistant answers directly"
                showLabel
              />
            </div>
          )}

          <div className="mt-8 w-full">
            <Composer
              value={value}
              onChange={onChange}
              onSubmit={onSubmit}
              sendLocked={sendLocked}
              attachments={attachments}
              uploadState={uploadState}
              onAttach={onAttach}
              onClearAttachment={onClearAttachment}
              onCancelUpload={onCancelUpload}
              mode={mode}
              onModeChange={onModeChange}
              modeFixed={modeFixed}
              modelName={modelName}
              skillCatalog={skillCatalog}
              maxActiveSkills={maxActiveSkills}
              selectedSkillIds={selectedSkillIds}
              onSkillsChange={onSkillsChange}
              deepResearch={deepResearch}
              onDeepResearchChange={onDeepResearchChange}
              autoFocus
            />
          </div>

          <div
            className="mt-4 flex min-h-10 w-full flex-wrap items-start justify-center gap-2"
            aria-hidden={!showStarters}
          >
            {showStarters
              ? STARTERS.map(({ label, Icon, prompt, agent }) => {
                  const owner = AGENT_BY_KEY[agent]
                  return (
                    <button
                      type="button"
                      key={label}
                      onClick={() => onChange(prompt)}
                      disabled={sendLocked}
                      title={
                        owner ? `${owner.label} — ${owner.description}` : undefined
                      }
                      className={cn(
                        'group inline-flex items-center gap-2 rounded-lg border border-border/60 bg-card/60 px-3.5 py-2',
                        'text-[13px] text-muted-foreground',
                        'transition-all duration-200',
                        'hover:border-border hover:bg-card hover:text-foreground hover:shadow-sm',
                        'disabled:opacity-50 disabled:cursor-not-allowed',
                      )}
                    >
                      {/* Plain icon, no badge fill. One per agent in the
                          roster's order already carries the pairing; three
                          filled colour chips under three coloured cards read
                          as decoration rather than as a relationship. */}
                      <Icon
                        className="h-3.5 w-3.5 shrink-0 transition-transform duration-200 group-hover:scale-110"
                        aria-hidden="true"
                      />
                      {label}
                    </button>
                  )
                })
              : null}
          </div>
        </div>

        <div className="flex min-h-8 w-full flex-[1.15] flex-col">
          {/* Where the Knowledge Agent looks: shown with it, in Workforce. */}
          {isWorkforce && knowledgeOn && (
            <div className="mt-6 w-full">
              <LibraryPanel workspaceId={workspaceId} />
            </div>
          )}
          {recent.length > 0 && (
            <div className="mt-6 w-full pb-8">
              <div className="mb-3 flex items-center justify-center gap-2">
                <Clock className="h-3.5 w-3.5 text-muted-foreground/50" aria-hidden="true" />
                <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground/70">
                  Recent chats
                </h3>
              </div>
              <div className="flex flex-col gap-1">
                {recent.map((conversation) => {
                  const key = conversation.conversationId || conversation.latestJobId
                  const active = isActiveJob({ status: conversation.status })
                  const done = SUCCESS_JOB_STATUSES.has(conversation.status)
                  const failed = ERROR_JOB_STATUSES.has(conversation.status)
                  return (
                    <button
                      key={key}
                      type="button"
                      onClick={() => onSelectConversation?.(conversation)}
                      title={conversation.title}
                      className={cn(
                        'group flex w-full items-center gap-3 rounded-full px-3.5 py-2.5 text-left',
                        'transition-colors duration-150 hover:bg-muted/50',
                      )}
                    >
                      <span
                        className={cn(
                          'flex h-5 w-5 shrink-0 items-center justify-center rounded-full',
                          done && 'bg-success/15 text-success',
                          failed && 'bg-destructive/15 text-destructive',
                          active && 'bg-link/15 text-link',
                          !done && !failed && !active && 'bg-muted text-muted-foreground/60',
                        )}
                        aria-hidden="true"
                      >
                        {done ? (
                          <Check className="h-3 w-3" strokeWidth={2.5} />
                        ) : (
                          <span className="h-1.5 w-1.5 rounded-full bg-current" />
                        )}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-[13px] text-foreground/90">
                        {conversation.title}
                      </span>
                    </button>
                  )
                })}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
