# V3 management experience design QA

Source visual truth: the existing V3 console at `http://localhost:5199/v3`,
captured after the user signed in. Implementation: the current worktree at
`http://localhost:5200/v3`, using the same backend and signed console session.
This is an incremental UX improvement of that visual target, not a new theme.

## Evidence and normalization

All source and corresponding implementation captures are 810 × 778 pixels at
an 810 × 778 CSS viewport, with no density scaling. Paired images are 1620 × 778
pixels, source on the left and implementation on the right.

| Step | Source | Implementation | Combined evidence | Health |
|---|---|---|---|---|
| 1. Command center | `design/v3-ux/01-home.jpg` | `design/v3-ux/05-home-after.jpg` | `design/v3-ux/compare-home.jpg` | Passed: metrics precede compact onboarding; header controls fit |
| 2. Agent list | `design/v3-ux/02-agents.jpg` | `design/v3-ux/06-agents-after.jpg` | `design/v3-ux/compare-agents.jpg` | Passed: readable names, explicit states, sort and result count |
| 3. Agent detail | `design/v3-ux/03-detail.jpg` | `design/v3-ux/07-detail-after.jpg` | `design/v3-ux/compare-detail.jpg` | Passed: actions wrap coherently; runtime status is explicit |
| 4. Create | `design/v3-ux/04-create.jpg` | `design/v3-ux/09-create-after.jpg` | `design/v3-ux/compare-create.jpg` | Passed: workspace target is visible; existing scenarios and form are retained |

Focused header evidence: `design/v3-ux/compare-header.jpg` pairs the top 96 px of
the source and implementation at equal scale. Mobile evidence at 390 × 844 CSS
and image pixels: `08-mobile-agents.jpg`, `10-mobile-detail.jpg`, and
`11-mobile-stages.jpg` under `design/v3-ux/`. These are responsive adaptations,
not fidelity comparisons against a provided mobile mock.

## Findings and comparison history

- **P2, source header overflow:** search text ran outside its control and
  persistent account actions were cropped. Fixed with bounded flex items,
  truncation, narrow search treatment, and responsive navigation. Combined
  header evidence shows all persistent actions within the 810 px viewport.
- **P2, source onboarding hierarchy:** completed setup cards occupied most of
  the first screen. Fixed by moving metrics first and collapsing setup in
  workspaces with agents. `compare-home.jpg` verifies the intended hierarchy.
- **P2, source agent affordances:** state was conveyed by a lamp; names used
  click-only table rows. Fixed with text chips, linked names, URL filters, and
  a labeled scroll region. `compare-agents.jpg` verifies the denser readable
  rows. Keyboard ArrowRight moved the focused mobile scroll region by 40 px.
- **P2, first implementation mobile header:** the account chip still crowded
  workspace context because a later shared chip rule overrode the responsive
  rule. Increased selector specificity and moved responsive overrides after
  shared primitives. Revised `08-mobile-agents.jpg` shows the complete default
  workspace name and region. Document width and viewport were both 390 px.
- **P2, first implementation mobile deployment table:** technical details were
  clipped by the desktop table treatment. Converted deployment rows to a small
  screen grid and enabled long-string wrapping. `11-mobile-stages.jpg` shows
  readable full details and textual stage state.

## Required visual surfaces

- **Typography:** existing Archivo/PingFang and IBM Plex Mono retained. Narrow
  page titles wrap; names remain readable instead of being squeezed by columns.
  Labels and secondary text are brighter. Global focus rings are visible.
- **Spacing/layout:** existing panel rhythm and radii retained. Rail shrinks
  to 200 px at tablet widths and folds on mobile. Header and page actions wrap
  without covering content. Table overflow is local and keyboard accessible.
- **Colors/tokens:** existing mint, amber, and coral semantics retained.
  Secondary text changes from `#66748f` to `#93a1bb`; no new decorative palette.
- **Assets:** existing brand and Lucide icons retained; no raster assets or
  placeholder art introduced. The visual calming follow-up removes decorative
  background grids and glows while retaining the existing dark palette.
- **Copy/content:** new copy exists in both en and zh-CN. Unknown/error states
  are distinguished from empty/healthy states. Default model differences between
  the source checkout and this worktree are pre-existing code/config differences,
  not part of this design change.

## Interaction checks and limits

Verified: onboarding expand/collapse; search updates URL and result count;
detail navigation and return preserve search; empty-result clearing; mobile
navigation and Escape focus return; table keyboard scroll; scenario arrow-key
selection; invalid form focus; workspace switch resets scoped fields and updates
deployment target. Layouts inspected at 810 × 778 and 390 × 844.
Browser console output was checked: no error-level entries were recorded;
the existing React Router v7 migration warnings remain.

Live traffic was empty and the inspected agent was ungated. Release coverage
failure and budget handling were verified with unit tests. Real invocation,
deployment, candidate promotion, and signing were not performed. Screenshot and
keyboard checks do not establish full accessibility compliance.

No actionable P0/P1/P2 findings remain in the changed surfaces. Other modules
inherit shell improvements but have not received a full journey audit.

## Visual calming follow-up

Equal-scale desktop evidence pairs `13-calm-before.jpg` and `14-calm-after.jpg`
at 1131 × 778 pixels in `design/v3-ux/compare-calm.jpg`. Background grids and
radial glows are removed; panels and navigation use solid fills. Decorative
corner ticks, numeric glow, and normal-state lamp animation are removed.
Semantic mint, amber, and coral remain visible through text, dots, and borders.
Depth shadows remain for overlays and focus indicators remain intact.

`15-calm-mobile.jpg` verifies the command center at 390 × 844 pixels. Titles,
workspace context, actions, metrics, and compact onboarding fit without overlap.
The temporary viewport override was reset after inspection. Desktop and mobile
visual checks passed; these changes intentionally reduce decoration and do not
alter layout hierarchy or backend behavior.

final result: passed
