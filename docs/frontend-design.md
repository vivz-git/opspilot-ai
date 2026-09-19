# OpsPilot Frontend Design Direction

> Status: **authoritative**, v1. Applies to every screen from FE-004 onward.
> FE-001..003 already conform to almost all of this (it codifies decisions
> made during FE-001's redesign pass); this document is what makes those
> decisions binding and repeatable instead of implicit. See §7 for what to
> do about the small drift found during the FE-004 audit.

## 1. What this has to feel like

**"An operator is watching an AI system execute real work."**

Not a marketing dashboard, not a chat product, not a generic admin panel
skinned dark. The reader is a person accountable for what the agent does —
they need to see what happened, what is about to happen, and what they are
personally authorizing. Every screen answers a version of: *what state is
this in, what's the evidence, what can I do about it.*

Six words the UI has to communicate, structurally, not decoratively:

| Word | Means, concretely |
|---|---|
| **Control** | The operator can always find "what can I do right now" without hunting. |
| **Clarity** | One glance gives status; one more gives why. Never a third glance for either. |
| **Evidence** | A claim ("verified", "sent") is always backed by a value on screen — a check, a timestamp, a payload — never just a green word. |
| **State** | Every status uses the vocabulary the backend actually returns (`RunStatus`/`StepStatus`/`ApprovalStatus`/`VerificationStatus`) — architecture.md §3.1's rule, restated visually: the frontend renders state, it doesn't invent it. |
| **Action** | An actionable moment (approve, reject, retry) is visually unmistakable and never competes with decoration for attention. |
| **Consequence** | Irreversible or outbound actions (§9 in architecture.md — approval-gated tools) are visually weighted differently from a read. Not alarmed — *weighted*. |

## 2. Absolutely not

This list exists because these are the default outputs of "make it look
like a modern AI product," and every one of them actively works against
§1. Treat a PR that adds any of these as a design regression, not a style
preference:

- AI-gradient backgrounds or borders (the purple→blue sweep)
- Glow, blur-behind, or neon edge lighting on any element
- Glassmorphism (frosted translucent panels) as a default surface treatment
- Oversized rounded corners (`rounded-xl`/`2xl`/`3xl`, pill-shaped cards)
- Heavy or multi-layer shadows (`shadow-md`/`lg`/`xl`) for elevation
- Decorative blobs, mesh gradients, or ambient background shapes
- Animation that doesn't represent a real state transition
- Sparkle/magic-wand iconography, or any "✨ AI-generated" affordance
- Generic SaaS dashboard chrome: stat-tile rows with big colorful numbers
  and no connection to a real record, KPI-card grids, gauge charts
- Cyberpunk/control-room styling: scanlines, terminal-green-on-black CRT
  effects, HUD corner brackets, glitch text
- Trend-chasing effects with no information payoff (parallax, tilt-on-hover,
  spring-bounce entrances, confetti, skeleton shimmer beyond a plain pulse)
- Fake urgency on approvals: countdown timers styled like alarms, red pulsing
  borders, siren icons, exclamation-mark spam

## 3. Foundation tokens

Defined once in `frontend/src/app/globals.css` (CSS variables) and
`frontend/tailwind.config.ts` (Tailwind color/font mapping). Never hardcode
a hex value in a component — reference the token.

### 3.1 Color

One dark theme, committed — not a light theme with a dark mode bolted on
(§1.2 already made this call; nothing here reopens it).

| Token | Hex | Use |
|---|---|---|
| `background` | `#0F172A` | Page background only |
| `card` | `#1B2336` | One step up from background — the *only* elevation signal we use instead of shadow |
| `foreground` | `#F8FAFC` | Primary text |
| `muted-foreground` | `#94A3B8` | Secondary text, labels, metadata |
| `border` | `#2A3554` | Hairline separators — at `/60` opacity by default so they read as structure, not boxes |
| `accent` | `#26314A` | Hover/active surface, never a resting background |
| `primary` | `#22C55E` | The one brand accent — "run green." Primary CTAs, active nav, the one thing on a dense screen that's allowed to draw the eye first |
| `success` | `#22C55E` | Same value as primary, deliberately — a passed check and a primary action share one visual language: "this is settled, in a good way" |
| `warning` | `#F59E0B` | In-progress-but-needs-attention: pending/awaiting approval, unconfirmed verification |
| `destructive` | `#EF4444` | Failed, rejected, error severity |
| `info` | `#3B82F6` | "Running" — in flight, distinct from settled success |

Rules, not suggestions:

- **One accent, not a palette.** `primary` is the only color allowed to mean
  "this is the important interactive thing here." If two elements on one
  screen both want to be the accent, one of them is wrong.
- **Status color is semantic only.** Green/amber/red/blue map to the
  backend's status vocabulary (§1's "State" row) and nothing else. Never use
  `destructive` red for decoration or `primary` green for a non-actionable
  label.
- **Elevation is background-lightness, not shadow.** `card` vs `background`
  is the entire elevation system. A shadow is a last resort for a genuinely
  floating element (a popover), never for "this section feels separate."

### 3.2 Typography

IBM Plex Sans (UI text) + JetBrains Mono (`--font-plex-sans` /
`--font-jetbrains-mono`, wired in `layout.tsx`). This pairing is the
project's one typographic identity — don't introduce a third family.

| Use mono for | Use sans for |
|---|---|
| Run/step/approval IDs, hashes, tool names | Headings, body copy, descriptions |
| Numeric data in tables (`tabular-nums`) | Labels, button text |
| JSON payloads (input/output/error blocks) | Everything a human wrote, as opposed to the system |
| Status badges | — |

Hierarchy is weight and size, not color. `text-lg font-semibold` for a page
title, `text-sm font-medium` for a card title, `text-xs uppercase
tracking-wide text-muted-foreground` for a field label — that three-step
scale is already the vocabulary in `run-header.tsx`/`plan-vs-actual.tsx`;
extend it rather than inventing a fourth weight.

### 3.3 Spacing and shape

- Radius: `rounded-md` (6px) for interactive controls, `rounded-lg` (8px)
  for containers. Nothing larger. This is a deliberate ceiling, not the
  default Tailwind scale — a bigger radius is the single fastest way to
  make a serious tool look like a marketing site.
- Spacing follows Tailwind's default 4px scale. Dense table rows use the
  existing `TableCell` padding (`px-3 py-2.5`); page-level sections use
  `gap-6`. Don't invent a third spacing rhythm.
- Borders at `/60` or `/40` opacity by default (`border-border/60`) — a
  full-opacity border reads as a harder edge than this UI wants for routine
  structure; save full-opacity borders for a state that should stand out
  (the approval-gap block, an active selection).

### 3.4 Motion

Two categories exist in the codebase today and that's the whole policy:

1. **Loading** — `Skeleton`'s plain opacity pulse, a spinner
   (`Loader2` + `animate-spin`) while a page/fetch is in flight.
2. **Live state** — `animate-live-pulse` on a status dot for a genuinely
   in-progress backend state (`running`, `awaiting_approval`). It marks
   "this is not settled yet," nothing else.

Every future addition must fit one of these two categories — "a real state
transition such as loading, approval, or live execution," per the brief —
or it doesn't ship. No hover-lift, no entrance animation, no easing flourish
for its own sake. `.animate-live-pulse` already respects
`prefers-reduced-motion: reduce` (`globals.css`); any new animation must do
the same, in the same place (the CSS layer, not a per-component check).

## 4. Component conventions

Primitives live in `frontend/src/components/ui/` (`Button`, `Card`, `Badge`,
`Input`, `Table`, `Separator`, `Skeleton`). **Before adding a new one, look
here first** — most needs (a labelled field, a dense row, a status pill) are
already met. A new primitive is justified by a genuine new interaction
pattern (e.g., a form field group), not by wanting slightly different
spacing than what exists.

### 4.1 Card is not the default container

`Card` means "this is a distinct, self-contained record" — a run's header
summary, a pending approval, one panel of a multi-panel layout. It is not
"a div with a border because sections need visual separation." A page
section that's just prose or a label + value can be a plain `<div>` with a
heading and, if it needs separation from what's above it, a top border
(`border-t border-border/60 pt-4` — see `run-header.tsx`'s stat row) rather
than another nested `Card`.

Concretely: don't wrap every subsection of a page in its own `Card`. If a
page reads as a stack of identical bordered boxes, that's the "generic
dashboard template" anti-pattern in different clothes — flatten what can be
flattened; reserve the container for things that are actually separate
records.

### 4.2 Tables over cards for tabular data

Already established (`RunsTable`, the approvals queue, `PlanVsActual`): when
rows share the same fields, that's a `Table`, not a stack of cards. Sticky
header, dense rows (`text-sm`, `px-3 py-2.5`), `tabular-nums` on numeric
columns, mono for IDs. This is non-negotiable for anything genuinely
tabular — it's also just correct information density for an operator
scanning many records.

### 4.3 Status

`StatusBadge` (`components/status-badge.tsx`) is the *only* status
renderer. It already covers `RunStatus`, `ApprovalStatus`, `StepStatus` and
`VerificationStatus` in one tone map. Extend that map for a new status
value; never build a second badge component or inline a status color.

### 4.4 Icons

`lucide-react`, line style, `h-3.5–5 w-3.5–5`, `aria-hidden` when
decorative. Pick the icon that names the concept plainly (a pause glyph for
a paused approval, a shield for risk) — never a generic AI/magic icon
(sparkles, wands, orbiting dots) standing in for "this is smart."

## 5. Approvals — the highest-stakes screen in the product

FE-004 is next and this is where "control / clarity / evidence / action /
consequence" is graded hardest, because this is the one screen where a
human's click causes a real outbound or mutating effect (architecture.md
§9). Contract, not suggestions:

1. **What** — the exact action in plain language first (`ApprovalResource
   .title`/`.summary`, already returned), tool name and risk level visible
   without a click.
2. **Why** — which run and step this belongs to, and (once FE-004 wires it)
   a path back to the plan/timeline that produced it — the operator should
   never have to take the system's word for why this step exists.
3. **The exact authorized payload** — `payload_preview` rendered in full,
   using the same redacted-JSON block pattern `StepInspector` already uses
   for tool input/output (`JsonBlock` in `step-inspector.tsx`) — reuse that
   pattern rather than inventing a second payload renderer. Render exactly
   what the server returns; this frontend does not build a second
   redaction system (architecture.md §14.5 owns redaction).
4. **Approve / Reject** — two buttons, unmistakably different from each
   other (`primary` for approve, `destructive` for reject — the one place
   `destructive` red is a button fill rather than a status color), same
   size, same row, no default/pre-focused state that makes one accidentally
   easier to hit than the other.
5. **Calm, not alarmed.** Risk is communicated through the existing
   `warning`-toned badge/border language already in `ApprovalPanel` —
   amber, a shield icon, a plain "HIGH" badge. No red pulsing borders, no
   siren icon, no countdown styled like a bomb timer. `expires_at` is
   informational text, not a stopwatch.
6. **No second approval mechanism.** Every screen that shows a pending
   approval (run detail's `ApprovalPanel`, the approvals queue) either
   links to the one approve/reject surface or, once FE-004 builds it,
   *is* that surface — never a shortcut action bolted onto a different
   page.

## 6. Process

- **Reuse first.** Read the existing primitive and the closest existing
  screen before writing a new component. FE-001/002/003's patterns
  (`Table` for lists, `StatusBadge` for status, the plan-vs-actual /
  timeline / inspector three-panel shape) are the vocabulary — extend it.
- **Cross-cutting changes only.** A shipped screen gets touched when a
  *token or primitive* changes underneath it (so every screen stays
  consistent), not for a standalone aesthetic pass. Don't redesign FE-001's
  Overview page because FE-005 wants a slightly different card treatment —
  fix the primitive, let it flow through.
- **This document changes when the direction changes**, not silently
  through one screen drifting from it. If a future screen needs to break a
  rule here, the rule gets updated first, in this file, with the reason —
  the same discipline `docs/decisions.md` applies to backend architecture
  calls.

## 7. FE-004 audit — what was already true, what needs a look

Audited against §1–§5 before FE-004 starts. The codebase already matches
almost all of this because it was built applying most of these principles
during FE-001's redesign pass; this section is the honest diff, not a
changelog of new fixes:

- **Already conforms, verified by grep across `frontend/src`:** no
  gradients, no glow/blur decoration, no shadow above `shadow-none`, no
  border-radius above `rounded-lg`, no sparkle/magic iconography, motion
  limited to the two categories in §3.4 and already respecting
  `prefers-reduced-motion`. `ApprovalPanel` and the approvals queue are
  already calm (amber + shield, no alarm styling).
- **Gap for FE-004 to fill, not a violation to fix now:** neither
  `ApprovalPanel` (run detail) nor the approvals queue table currently
  renders `payload_preview` — §5.3's "exact authorized payload" requirement
  has no UI yet. That is FE-004's actual scope (approve/reject +
  showing what's being authorized), not a pre-existing defect to patch
  ahead of it.
- **Forward guidance, not a retrofit:** §4.1's "Card is not the default
  container" is written for screens built *from here on*. FE-003's run
  detail (`RunHeader`, `PlanVsActual`, `ExecutionTimeline`, `StepInspector`
  as four stacked `Card`s) predates this document and works; per §6,
  it is not being torn up for a stylistic pass. If FE-004 or a later task
  touches that page for a real reason, bring it in line with §4.1 then —
  not as a standalone redesign now.
