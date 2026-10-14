# Guided console — design

> Source: customer feedback (2026-10-14). When something fails, the console only says that it failed, not where to go next. A first-time user can read the word "criteria" (判据) but does not know which steps use it or who owns it.

## 1. Goals and principles

Goal: on any page a user can answer three questions — **what is this**, **where am I in the flow**, **where do I go next**.

1. **Every error comes with a way out.** Each error the user can fix says why and offers a next step they can click. For one they cannot fix (another role, an administrator), it says whom to ask.
2. **A disabled button says why.** Not just greyed out: what is missing and where to supply it.
3. **Terms explain their use, not only their definition.** The explanation names the steps the term appears in and who owns it, and each step is a link.
4. **Facts, not clicks.** Preconditions and completed steps are computed from what actually exists (as `LaunchSequence` already does), never from a "got it" click.
5. **Routes live only in the frontend.** The backend says which action comes next and with which parameters; a frontend action table decides the URL, so a page redesign does not touch the backend.
6. **No new debt.** Copy, actions and terms are checked in CI; a new error code or term without copy fails the build.

## 2. Current state

| Area | Today | Problem |
|---|---|---|
| Error envelope | `{code, message, detail}` (`backend/app/core/errors.py`); the frontend translates `apiErrors.<code>` (`localizedMessage` in `lib/api.ts`) | Says what went wrong, never what to do next |
| Error copy | ~618 backend error codes, ~185 with `zh-CN` copy | Agent-DLC's `release.*`, `golden.*`, `criteria.*`, `admission.*`, `waiver.*`, `calibration.*` have almost no Chinese; users see the log-oriented English, e.g. `release.no_criteria` |
| Error display | ~677 `toast(...)` calls | Gone in seconds, cannot carry a link, far from where the error happened |
| Disabled buttons | ~248 `disabled={…}` in V3, ~29 with a `title` | The button is grey with no reason, e.g. "New criteria set" while no agent is running |
| Terms | `<Term>` (`v3/onboarding/Glossary.tsx`): one sentence on hover, click opens the glossary; 19 terms registered (`GLOSSARY_TERMS` in `v2/Glossary.tsx`) | No Agent-DLC terms (`criteria` has copy but is not registered); 5 uses in V3; definition only, no use or steps |
| Guidance | Home `LaunchSequence`: five steps, each with a `to` link, state computed from data | Covers only the start; feature pages have no "what this page needs first" |

## 3. A next step on every error

### 3.1 Envelope extension

`envelope` gains an optional `next`; old clients ignore it, so it is backward compatible:

```jsonc
{
  "code": "release.no_criteria",
  "message": "agent has no published criteria set",
  "detail": null,
  "next": [
    { "action": "criteria.publish", "params": { "agent_id": "ag-123" } },   // first = primary
    { "action": "doc.flywheel_evaluate" }                                    // optional secondary
  ],
  "needs": "perm:golden.admit"   // optional: only a holder of this permission can fix it
}
```

- `AppError` gains optional `next: list[NextStep] | None` and `needs: str | None`; `app_error_handler` passes them through. `NextStep` is `{action: str, params: dict[str, str]}`.
- The public `/v1` API and `/share` pages **omit** `next` and `needs`: they cross the API-key trust boundary and console routes mean nothing to those callers (the same reason `client_error_handler` hides the AWS text).
- Validation errors (422) carry no `next`; the frontend already places them on fields.

### 3.2 Frontend action table

New file `frontend/src/v3/next.ts`:

```ts
export const NEXT_ACTIONS = {
  "criteria.publish":   { to: (p) => `/v3/standards?agent=${p.agent_id}&view=criteria`, label: "apiNext.criteria.publish" },
  "golden.open":        { to: (p) => `/v3/standards?agent=${p.agent_id}&view=golden&id=${p.dataset_id}`, label: "apiNext.golden.open" },
  "agent.create":       { to: () => "/v3/create", label: "apiNext.agent.create" },
  "workspace.fix_role": { to: (p) => `/v3/workspaces?id=${p.workspace_id}`, label: "apiNext.workspace.fix_role" },
  // …
} satisfies Record<string, NextAction>;
```

- `label` is an `apiNext.<action>` key, required in en and zh-CN, e.g. "Publish the criteria set".
- An action the table does not know is dropped silently, so frontend and backend can ship separately.
- Some codes need no backend change: next to the table the frontend keeps `DEFAULT_NEXT: Record<code, action>` for parameterless defaults, e.g. `aws.access_denied` → workspace role settings. A `next` from the backend wins.

### 3.3 Permission errors

For `auth.permission_required`, or whenever `needs` is set, the message has three parts: the missing permission, the role that usually holds it, and where it is granted:

> Curating the splits needs `golden.admit`, usually held by `biz-owner`. Ask a holder to do it, or ask an administrator to grant it on the **Users** page. [Open Users →]

The link shows only to administrators; for anyone else it would lead to another 403.

### 3.4 Display: `ErrorCallout` replaces error toasts

New `ErrorCallout`, built on the existing `Notice` (`v3/ui.tsx`):

```
⚠ The agent has no published criteria set, so the gate cannot evaluate it.
  The gate judges each published criterion; a deployment locks the criteria version published at that moment.
  [Publish the criteria set →]   [What is this step?]
```

- Three lines: **what went wrong** (`apiErrors.<code>`) → **why** (optional `apiErrors.<code>_why`) → **next** (the `next` buttons, plus an optional "What is this step?" that opens the term card, §5).
- Placement: an error caused by a user action sits next to the button or form that triggered it and stays until the user acts again or dismisses it; a failed background poll sits at the top of its panel.
- `toast` stays for success messages and background events unrelated to the current page; such a toast with a `next` shows an action button and stays until dismissed.
- Moving the ~677 `toast(errorMessage(err))` calls to `ErrorCallout` is mechanical and goes page by page (§8); new code is held by CI (§7).

### 3.5 First batch of error codes

The codes most often hit on the workshop's main path (Prepare → Define → Build → Evaluate → Release → Observe → Feedback); each gets en + zh-CN copy, a reason and a `next`.

| Code | Next step |
|---|---|
| `release.no_criteria` | `criteria.publish` |
| `release.no_golden` / `release.empty_golden` | `golden.open`: the golden set linked to the criteria set, focused on "Curate the splits" when not yet curated |
| `release.not_evaluated` / `release.gate_not_passed` | `gate.open`: the candidate card on the gate |
| `release.self_sign` / `criteria.self_sign` / `waiver.self_approve` | No link; says another person must sign and lists the permission holders |
| `criteria.agent_required` | `agent.create`; `agent.open` when an agent exists but is not running |
| `criteria.draft_exists` | `criteria.open_draft` |
| `criteria.sign_unpublished` | `criteria.publish` |
| `golden.duplicate_name` (see `agent-dlc-design` §4.2.1) | `golden.open`: the existing set with that name |
| `golden.holdout_sealed` / `golden.holdout_closed` | Explains why it is sealed; `admission.open`: new items go through admission |
| `golden.duplicate_scenario` / `golden.bad_case_tier` | Points at the offending item in the curate dialog; `doc.golden_format` |
| `admission.no_golden_set` | `golden.create` |
| `waiver.redline` | Explains red lines cannot be waived; `criteria.open` on that criterion |
| `workspace.assume_role_failed` / `aws.access_denied` | `workspace.fix_role`, with the missing IAM action (`detail.operation`) |
| `workspace.not_ready` | `workspace.open`: bootstrap progress |
| `auth.permission_required` | Per §3.3 |

## 4. Disabled buttons say why

- `Btn` gains `disabledReason?: { text: string; next?: NextStep }`. When set, the button stays focusable (`aria-disabled` instead of `disabled`) and shows the reason on hover and focus, with a link when `next` is given.
- Reasons are computed from data in pure, unit-tested functions:

  ```ts
  // standards/guards.ts
  export function canCreateCriteria(agents): Reason | null {
    if (!agents.some((a) => a.status === "active"))
      return { text: "v3.reason.needActiveAgent", next: { action: "agent.create" } };
    return null;
  }
  ```

- A button disabled only while its request is in flight needs no reason; it uses a `busy` state and the CI rule ignores it.

## 5. Term cards

### 5.1 From one sentence to a card

```
Criterion
A quality rule you can check, e.g. "never promise a refund the policy does not allow".
Used in: Define (draft) → Evaluate (publish, sign) → Gate (judged one by one) → Observe (drift watch)
Owner: developer drafts, biz-owner signs
Related: criteria set · red line / gate / observe · golden set
[Open criteria →]
```

The data moves from one `glossary.<term>` string to:

```ts
interface GlossaryEntry {
  short: string;                     // the hover sentence (existing copy)
  stages?: { stage: Stage; what: string; action?: string; params?: string[] }[];
  owner?: string;
  related?: GlossaryTerm[];
  action?: string;                   // the card's main link, through the §3.2 action table
}
type Stage = "prepare" | "define" | "build" | "evaluate" | "release" | "observe" | "feedback";
```

- Hover / focus shows `short` and `stages` (one line joined by →); click opens the full card in the glossary.
- Each stage is a link carrying the agent selected in the page header (`params: ["agent_id"]`); with none selected it just opens the page.
- Stage names match the workshop and the home `LaunchSequence`, so users see one flow.
- Keys: `glossary.<term>.short`, `glossary.<term>.stage.<stage>`, `glossary.<term>.owner`. The existing `glossary.<term>` moves to `.short`; V2's `HintIcon` and `HintLabel` read `.short`.

### 5.2 First batch (Agent-DLC)

Added to `GLOSSARY_TERMS` (Chinese wording as in `agent-dlc-design` §9.2 and the workshop):

| Key | zh-CN | Main stages |
|---|---|---|
| `criteria` | 判据 | Define, Evaluate, Gate, Observe, Feedback |
| `criteriaSet` | 判据表 | Define, Evaluate, Release |
| `criteriaTier` | 红线 / 门限 / 观察 | Define, Gate |
| `goldenSet` | 黄金集 | Define, Evaluate, Gate, Feedback |
| `goldenSplit` | 开发集 / 回归集 / 保留集 | Evaluate, Gate |
| `caseTier` | 用例档位 | Define, Evaluate |
| `admission` | 样本准入 | Feedback |
| `judgeCalibration` | 裁判校准 | Evaluate |
| `gate` | 放行门 | Evaluate, Release |
| `passK` | pass^k | Evaluate |
| `waiver` | 豁免 | Release |
| `releaseRecord` | 决策记录 | Release, Observe |
| `driftWatch` | 漂移监测 | Observe |
| `repairLadder` | 修复阶梯 | Observe, Feedback |
| `scorecard` | 记分卡 | Evaluate, Observe |

The 19 existing general terms get `stages` in P2 (optional).

### 5.3 Where terms are marked

- **Explicit**: wrap terms in page titles, sub-tab names (e.g. the `Filters` options on the criteria page), table headers and form labels with `<Term>`. No DOM scanning — automatic matching would hit body text and user data.
- **First occurrence per page**: a page-level `TermScope` context remembers what it has marked; later occurrences render plain.
- **Can be turned off**: a "Term hints" switch in the help menu, saved per user (same storage as `useLaunchHidden`); off removes the underlines, ⌘K and the glossary still work.
- **Terms on this page**: the header help button lists only the terms registered in the page's `TermScope`.

## 6. Per-page preconditions

`LaunchSequence`, generalised: a page or sub-tab declares its preconditions; while any is unmet, its empty state (`Empty`) shows the checklist, each row with a link.

```ts
// standards/requirements.ts — pure, unit-tested
export function releaseRequirements(s: AgentDlcState): Requirement[] {
  return [
    { key: "agentActive",        done: s.agent?.status === "active", action: "agent.open" },
    { key: "criteriaPublished",  done: !!s.publishedCriteria,        action: "criteria.publish" },
    { key: "criteriaSigned",     done: !!s.publishedCriteria?.signed, action: "criteria.sign", needs: "biz-owner" },
    { key: "goldenSealed",       done: s.golden?.holdoutSealed,       action: "golden.open",   needs: "golden.admit" },
    { key: "redeployedAfter",    done: s.lockedCriteriaVersion != null, action: "agent.redeploy" },
  ];
}
```

First batch: **Criteria set**, **Golden set**, **Gate** and **Judge calibration** on the criteria page, and **Tasks → New task**. Hidden when all are met; otherwise the next one is lit and the rest dimmed, as in `LaunchSequence`. When `needs` is not the current user, the row reads "needs biz-owner".

## 7. Copy and CI checks

New frontend test `frontend/src/v3/guidance.test.ts` and backend test `backend/tests/test_error_guidance.py`:

1. **Error copy**: every `AppError` / `NotFoundError` code extracted from the backend source has en and zh-CN `apiErrors.<code>`. Today's gaps live in a baseline `frontend/src/locales/missing-api-errors.json` that **may only shrink**: a new code without copy fails, and so does copy added without removing the baseline entry.
2. **Action table**: every `next.action` in backend code is in `NEXT_ACTIONS`; every action has en + zh-CN copy; the path `to()` produces starts with a route in `nav.ts`.
3. **Terms**: every term in `GLOSSARY_TERMS` has `.short` and `name` in en + zh-CN; actions referenced by `stages` exist.
4. **Disabled reasons**: an ESLint rule warns on V3 `<Btn disabled=…>` without `disabledReason` that is not `busy`; existing sites go into a shrink-only baseline.

## 8. Phases

| Phase | Scope | Exit criterion |
|---|---|---|
| P1 | Envelope `next` / `needs`; `NEXT_ACTIONS`; `ErrorCallout`; the §3.5 codes; the 15 §5.2 term cards, marked on the criteria and tasks pages; CI checks 1–3 | Walking the workshop end to end, every error on the criteria page, tasks page and gate has Chinese copy and a next step |
| P2 | `disabledReason` and the ESLint rule; §6 preconditions; `stages` for general terms; error toasts on all V3 pages replaced by `ErrorCallout` | No disabled button without a reason in V3 (baseline 0); no error toasts |
| P3 | Empty the error-copy baseline; V2 pages as needed | `missing-api-errors.json` is empty |

## 9. Testing

- Backend: envelope output of `AppError(next=…)`; `/v1` and `/share` omit `next` / `needs`; every §3.5 code carries complete `next` params (table-driven).
- Frontend: `ErrorCallout` renders the three lines and the action buttons; unknown actions are dropped; non-administrators see no grant link; `TermScope` marks once per page; turning hints off removes underlines; table-driven cases for each `requirements` function.
- E2E: in an empty workspace, trigger `release.no_criteria`, `release.empty_golden` and `golden.duplicate_name` along the workshop path; the next-step button lands on the right page and sub-tab with the right agent selected in the header.

## 10. Out of scope

- Fixing things for the user (e.g. one click publishes the criteria set): a next step only leads the way; the action stays on the target page, keeping sign-off and audit semantics.
- A site-wide step-by-step interactive tutorial: the existing `Tour` is unchanged.
- Changing error codes themselves or HTTP status codes.
