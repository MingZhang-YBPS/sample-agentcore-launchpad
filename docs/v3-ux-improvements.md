# V3 management experience improvements

This iteration improves the existing V3 console's shell, command center, agent
list/detail, and quick creation flow. It preserves the existing dark theme,
AgentCore APIs, deployment pipeline, and named production endpoint semantics.

## Operator journeys

1. **Understand what needs attention.** Fleet metrics precede onboarding. In a
   workspace with agents, onboarding collapses to its next action and can be
   expanded. The command center exposes refresh and creation actions.
2. **Find and inspect an agent.** Search, status, and sorting are URL parameters
   (`q`, `status`, `sort`). Opening a detail and returning preserves them.
   Recent updates are the default order. Status has a text label as well as a
   color. Names are actual links, usable by keyboard and in a new tab.
3. **Understand deployment and release readiness.** Runtime status is labeled
   separately from the lifecycle. Deployment stages have textual statuses.
   Release and version read failures have retry controls. A release read failure
   is never represented as an ungated agent. Refresh reloads all detail panels;
   finishing a deployment refreshes versions and release state.
4. **Create in the intended workspace.** The quick form states its target name
   and region. Invalid submissions focus the first invalid field, including a
   missing required knowledge base. Error descriptions are associated with
   fields. Scenario radios support arrow keys, Home, and End. Submission locks
   editable fields and scenario selection until the request completes.

## Trustworthy loading and workspace boundaries

The command center reads release state for at most 24 active agents. Failed reads
and agents beyond that budget are explicitly counted as incomplete coverage;
an incomplete empty queue cannot produce an all-clear headline. Traffic errors
are distinct from having no traffic. Unknown endpoints are labeled unverified.
The release cache key includes agent IDs, status, version, and update time rather
than fleet size alone.

`useLoad` suppresses data from a previous key immediately, while retaining data
during a refresh of the same key. Native workspace-bound content remounts when
the workspace changes, matching hosted V2 behavior. This resets workspace-bound
forms and selections; announcements, videos, and video-management drafts remain
hub-global and do not remount on a workspace change.

## Responsive and keyboard behavior

The top search truncates within its control and becomes an accessible icon at
narrow widths. At 760 px and below, navigation is toggled from the header;
Escape closes it and returns focus to the toggle. Workspace context remains in
the header. Long account names cannot crowd out controls; on small screens the
account identity is included in the logout control's accessible name.

The agent table scrolls within a labeled keyboard-focusable region. Deployment
stages become stacked rows on small screens, retaining full technical details.
Secondary text is brighter and interactive elements receive visible focus rings.

## Calm management surfaces

The background uses a solid dark fill without a grid or radial glows. Panels,
header, and navigation use solid fills; decorative corner ticks and numeric
glows are removed. Healthy running lamps are static, while work-in-progress
lamps retain motion. Semantic colors, focus indicators, and overlay depth remain
available to communicate status and interaction.

## Validation scope

Browser checks cover the command center, list filtering/empty recovery,
detail/back navigation, scenario keyboard selection, invalid-form focus,
workspace changes, and desktop/tablet/mobile layouts. Actual deployment,
invocation, gate signing, and AWS mutations are outside this visual verification.
The release-coverage tests cover partial failure, the 24-agent budget, and an
inactive fleet. The canonical `make verify` gate validates all repository layers.

Local comparison captures and the audit notes are under ignored `design/v3-ux/`;
the final visual comparison record is `design-qa.md`.
