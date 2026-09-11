---
name: design-quality
description: Load before any frontend UI/layout/CSS work on this project (agent-webapp). Sets the visual-quality bar this app is held to -- spacing discipline, typographic hierarchy, restraint, and "does this look considered or accidental" checks. Use whenever building or revising a screen, component, or theme, not just when asked to "make it pretty".
---

# Design quality bar for this project

This is a custom, project-specific taste bar -- not a licensed or
installed third-party skill. It exists because generic "make it nicer"
requests keep producing changes that are functionally correct but read
as amateur: default-feeling layouts, inconsistent spacing, elements
that float in empty space without being anchored to anything. This
document is the checklist that catches that class of problem before
it ships.

The reference quality bar is what Anthropic's own Claude.ai interface
and similarly disciplined modern products (Linear, Vercel dashboards)
do by default: restraint, a real spacing system, typographic hierarchy
carried by weight/size/color rather than by adding boxes and borders,
and nothing that looks like it was dropped onto the page without a
layout plan around it.

## The failure mode to watch for

A screen that is functionally complete can still fail this bar. The
tell is usually one of:

- **A bare card floating on empty background.** A modal or panel with
  nothing else on the page reads as unfinished -- like a placeholder,
  not a designed screen. If a screen's whole content is one centered
  card, that's a sign the screen needs a real composition (a subtle
  backdrop treatment, secondary content, an illustration, layered
  depth), not just a wider card.
- **A cramped, narrow-feeling content column** inside a much wider
  viewport. Centering content in a fixed max-width is fine; centering
  it so aggressively that it reads as squeezed into a tunnel, with
  large dead margins on either side that do nothing, is not. Check
  whether the surrounding space is doing any work (secondary panels,
  breathing room that matches the content's importance) or is just
  empty.
- **Inconsistent spacing that "looks close enough".** If gaps, padding,
  and margins were not chosen from a scale, they will not read as
  intentional even if each individual value looks fine in isolation.
- **Borders and boxes standing in for hierarchy.** If every section
  needs a border or a background tint to be told apart from its
  neighbor, that's a sign type weight/size/color spacing isn't doing
  enough work yet.

## Rules to apply

1. **Spacing comes from a scale, not from eyeballing.** Pick a base
   unit (8px is the safe default; 4px if the design needs finer
   control) and only use multiples of it for padding, gaps, and
   margins. Before changing a spacing value, check what scale the
   surrounding CSS already uses (grep existing `padding`/`gap` values
   in `styles.css`) and stay on it -- don't introduce a second,
   incompatible scale.

2. **Every standalone screen needs a real composition, not just a
   centered element.** If a view's content is a single card/modal on
   an otherwise empty background, treat that as unfinished. Options,
   roughly in order of effort: give the background a subtle deliberate
   texture/gradient/pattern (not decoration for its own sake -- enough
   to make the page feel designed rather than blank), add real
   secondary content around the primary element (recent activity, a
   short explanatory line, contextual info), or restructure so the
   "screen" is actually a normal page layout with the interactive
   element as one part of it rather than the whole thing.

3. **Typographic hierarchy first, boxes/borders second.** Reach for
   font-weight, size, and color (using the existing muted/foreground
   tokens) to separate sections before reaching for a new border or
   background tint. If two adjacent elements both have their own card
   chrome, ask whether they need to.

4. **Corporate/polished, not playful, unless told otherwise.** Prefer
   restraint: fewer simultaneous accent colors, consistent border-radius
   across similar elements (don't mix a 6px radius here and a 16px
   radius there for the same kind of control), consistent icon sizing.

5. **Motion should be small and purposeful.** Hover/focus/active states
   get a fast (120-180ms) transition on the properties that actually
   change (opacity, background, transform) -- not a blanket `transition:
   all`. No motion that exists just to look busy.

6. **Don't touch the existing color palette unless asked.** The brief
   that sends you here will say explicitly if the palette itself is in
   scope. Default to reusing the existing CSS custom properties
   (`var(--cream)`, `var(--blue)`, `var(--muted)`, `var(--border)`,
   `var(--panel)`, etc.) rather than introducing new raw color values.

7. **Before calling a screen done: zoom out and look at it as a whole,
   not component-by-component.** Take a full-viewport screenshot (not
   just a zoomed crop of the one element you edited) and check: does
   this look like one considered composition, or like several pieces
   assembled without a plan? Would a designer be able to point at
   *why* each spacing/size choice is what it is, or would the honest
   answer be "it looked okay"?

## Applying this to a specific complaint

When the brief names a concrete symptom (e.g. "the chat screen feels
cramped/squeezed in the middle", "the landing page looks like a bare
modal"), map it to the rule above it violates (narrow content column
with dead margins -> rule 2/off-scale spacing; bare floating card ->
rule 2) and fix the root layout decision, not just the symptom's pixel
values. A wider max-width alone rarely fixes "feels cramped" if the
real problem is that nothing else on the page is doing any visual
work.
