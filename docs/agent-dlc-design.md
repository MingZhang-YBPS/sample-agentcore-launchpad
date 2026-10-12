# Agent-DLC alignment — design

Status: **draft for review** · Branch: `feat/agent-dlc` · Owner: platform team · 中文版: [agent-dlc-design.zh-CN.md](agent-dlc-design.zh-CN.md)

This document designs how Launchpad implements the Agent-DLC methodology
(Agent Development Lifecycle, "Evaluation-Driven Deployment") end to end on the
platform. Sources: the Agent-DLC handbook (架构师实践手册), the L200 TTT deck, the
FCD deck, the evaluation-methodology workshop deck, the NetEase briefing and the
OnePage. Where the methodology says a **person** decides, the platform does not
decide for them: it records the decision, enforces it, and puts the evidence that
decision needs on one screen.

Decisions already taken:

- **Load / concurrency testing is out of scope.** Performance is measured from
  traces (P50/P95, first-token latency) and gated on those; a load-test report can
  be attached as release evidence, but the platform does not generate load.
- **pass^k is an opt-in run option.** Choosing k > 1 shows a cost estimate
  before the run is submitted and records the actual spend afterwards.

---

## 1. Principles

1. **The criterion is the unit.** Everything — evaluators, golden examples,
   thresholds, gate decisions, drift alerts, admissions — hangs off a versioned
   criterion row (判据). A score that is not attached to a criterion is evidence,
   not a decision.
2. **Humans own the ruler, the platform runs it.** Business owners write criteria
   and thresholds, annotators write golden answers, a named person admits samples
   and signs releases. The platform never edits a criterion, threshold or golden
   answer by itself (methodology: "回流可以自动化取样本，不能自动化定期望").
3. **Deterministic first.** The UI steers each criterion up the evaluator ladder
   (code assertion → trajectory match → builtin → custom judge → human); a judge
   criterion cannot gate until it is calibrated.
4. **Gate before traffic.** A new agent version is evaluated behind a named
   endpoint; production traffic moves only after the gate passes.
5. **Lineage or it did not happen.** Every score carries the five versions that
   produced it: criteria set, golden set, evaluator set, agent version, endpoint.
6. **Existing architecture rules hold.** AWS stays the source of truth; the ledger
   stores identifiers plus derived results; all clients come from
   `services/aws_clients.py`; preview-API drift stays inside `agentcore/` and
   `evaluation/agentcore_eval.py`; new routes are classified in `route_policy.py`;
   V2 only; every string i18n'd.

## 2. Methodology → platform map

| Ring | Deliverable (methodology) | Platform object | Human decision it supports |
|---|---|---|---|
| ① Define | criteria table + first golden set, signed | `CriteriaSet` vN + `GoldenSet` (dataset) vN | write criteria, choose tier + threshold, sign |
| ② Build | runnable, *evaluable* agent | agent + snapshot + **field-alignment check** | — (engineering) |
| ③ Evaluate | per-dimension report, failure list, calibration record | `EvalRun` + `CriterionResult` + `CalibrationRecord` | calibrate judges, attribute failures |
| ④ Release | gate report, release record, rollback-able version | `GateReport` + `ReleaseRecord` + named `live` endpoint | sign off, approve waivers |
| ⑤ Observe | online trend, drift alerts, cost/latency board | `WatchConfig` + scheduled checks + alerts | triage drift (agent vs judge) |
| ⑥ Feedback | larger golden set, revised criteria | `AdmissionCandidate` queue → new versions | admit samples, bump criteria |

Five dimensions are a fixed enum on every criterion:
`cognition | quality | responsibility | cost | performance` (认知 / 质量 / 责任 / 成本 / 性能),
in that order everywhere in the UI.

Three tiers: `redline` (boolean, 0 violations, never weighted, never waived),
`gate` (threshold, per dimension, waivable with expiry), `observe` (recorded,
never blocks).

## 3. Current state and gaps

What exists and is reused (file references are to `backend/app/` unless noted):

| Capability | Where | Reused as |
|---|---|---|
| Local datasets + AWS Dataset sync, immutable versions | `evaluation/models.py:21` `EvalDataset`, `evaluation/routers.py:459,578` | golden-set storage and versions |
| Batch eval runs with platform-side scenario replay, retries, budget stops | `evaluation/service.py:263` `execute_run` | the run engine; pass^k plugs into its scenario loop |
| Per-session, per-evaluator labels/values/explanations | `evaluation/service.py:676` `run_results` (read live from CloudWatch, **not persisted**) | input to criterion results (must be snapshotted) |
| Builtin / ThirdParty / Custom / CustomDerived / CustomCode evaluators, trajectory matchers | `evaluation/agentcore_eval.py:88,117,603,672,746` | criterion executors |
| Online eval configs + per-evaluator time series | `evaluation/online.py:569` | watch signals |
| Insights (FailureAnalysis / UserIntent / ExecutionSummary) with affected-session counts | `evaluation/agentcore_eval.py:519,553` | admission candidates, impact ranking |
| Session → dataset builder with dedupe | `evaluation/pipeline_routers.py:181` | admission "accept" path |
| Thumbs feedback, issue box (with fixes + history trail), SME review links | `services/feedback.py`, `services/issues.py`, `services/review_links.py` | admission sources; account-free annotator links |
| Architect evaluation plan (golden tests, evaluator entries with `blocking`/`threshold`, fishbone over five dimensions) | `assistant/evaluation_plan.py:221,451`, `assistant/proposal.py:127` | importer into a `CriteriaSet` |
| Spec snapshots, release bundles, promotions, release gates, canary, experiments | `services/snapshots.py`, `services/promotion*.py`, `services/release_gates.py`, `optimization/*` | release path, gate host, A/B evidence |
| Runtime + harness named endpoints, invoke `qualifier` | `services/agentcore/runtime.py:314-426`, `services/agentcore/harness.py:112-187` | the `live` / `candidate` endpoints |
| Permission keys, audit events, self-approval refusal | `services/users.py:32`, `services/audit.py:16`, `services/promotion.py:399` | sign-off pattern |

Gaps this design closes:

1. No criterion object; `blocking`/`threshold` in the evaluation plan are display-only (`assistant/evaluation_plan.py:228`).
2. The release gate is one pooled mean of all evaluator averages vs `min_eval_score` (`services/release_gates.py:263-305`); no red line, no denominator check, no per-dimension gate, no INCONCLUSIVE.
3. Per-scenario results are not persisted; runs are not tied to an agent version, evaluator-set version or criteria version. `promotion._evaluation_of` pins "the latest completed run", whatever it evaluated.
4. Production traffic and offline evals both hit `DEFAULT`, which auto-rolls on every update, so every redeploy, rollback, experiment promote and non-canary promotion is live **before** any gate.
5. No pass^k, no calibration (κ), no Wilson intervals, no sample-size guidance, no eval cost accounting.
6. No golden-set splits (dev / regression / holdout), no item provenance, no admission review.
7. No background scheduler (only the hourly price refresher), so no scheduled re-evaluation, drift detection or unattended alerts.
8. No waivers, no release record with lineage, no audit read API.

### 3.1 Prerequisite fixes found during the inventory

These are bugs or inconsistencies on `main` that the design depends on; they are fixed first, each with a regression test.

| # | Defect | Location |
|---|---|---|
| F1 | Error-rate and latency alert rules always read "unknown": `_dashboard_value` reads `traces/errors/p95_ms` at the top level, `get_dashboard` returns them under `tiles`; the test stubs the flat shape. A `30d` window raises `KeyError` in `RANGE_HOURS`. | `services/alerts.py:84-103`, `tests/test_costs_alerts.py:270` |
| F2 | Cost report matches agents by `agent.name == service.name`, but service names carry the endpoint suffix (`<runtime>.DEFAULT`), so most spend lands in "unknown". | `services/costs.py:121-134` (use `observability.build_agent_resolver`) |
| F3 | `agent_versions.CANARY_ENDPOINTS` matches literal `stable`/`treatment`, real names are `stable<id6>`/`treat<id6>`. | `services/agent_versions.py:31`, `optimization/canary_service.py:647` |
| F4 | A non-canary promotion that fails smoke stays live (only canaries are undone). | `services/promotion_exec.py:882` |
| F5 | Canary `act_complete` writes `agent.spec`/`version` without a spec snapshot. | `optimization/canary_service.py:1095-1104` |
| F6 | Experiment `promote` only requires a verdict artifact to exist, not a winning or explicitly overridden verdict. | `optimization/service.py:1460-1468` |

## 4. Domain model

All new tables are workspace-scoped (added to `WORKSPACE_SCOPED_TABLES`, `core/db.py:23`), registered in `init_db`, and evolved only by additive `ALTER TABLE`. JSON columns hold structured sub-objects; anything AWS owns is stored as an identifier and re-read.

### 4.1 CriteriaSet and Criterion (判据表)

```
criteria_sets
  id, workspace_id
  kind               template | agent
  agent_id           null for templates
  template_id, template_version   (agent sets only: the template version it inherits)
  name, description, scenario      (scenario: free-text tag used to suggest templates)
  version            int, monotonically increasing; draft = highest unpublished
  status             draft | published | superseded
  signed_by, signed_at, sign_note        # business-owner sign-off (§7.1)
  parent_version     the version this draft was copied from
  source             manual | assistant_plan | import
  created_by, created_at, updated_at
  unique(workspace_id, kind, agent_id, name, version)

criteria
  id, workspace_id, set_id, set_version
  key                stable id across versions, e.g. "C-003" (lineage anchor)
  text               the criterion sentence; subject must be the agent, predicate observable
  dimension          cognition | quality | responsibility | cost | performance
  tier               redline | gate | observe
  threshold          float 0..1 for gate (pass rate); null for redline (always 0 violations);
                     for cost/performance: {metric, op, value, unit} e.g. p95_ms <= 3000
  level              session | trace | tool_call
  executor           {kind: evaluator | metric | human,
                      evaluator_id?, metric?: latency_p95|first_token_p95|cost_per_success|tokens_per_session,
                      label_map?: {pass: [...labels], fail: [...], inconclusive: [...]},
                      score_rule?: {op: ">=", value: 0.5}}       # numeric → pass/fail per item
  denominator        sessions | turns | fields   (business-confirmed, frozen per version)
  expected_type      deterministic | redline | trajectory | compliance | soft | efficiency
  online             offline_only | online_ok        (derived: GT placeholders ⇒ offline_only)
  pass_k             null | {k, mode: all | majority}  (opt-in, §5.3)
  calibration_required  bool (true for judge executors)
  attribution_layer  01..07 (§7.4 seven layers), optional hint
  owner              username of the business owner
  examples           [{dataset_item_ref, polarity: positive|negative, note}]
  notes
```

Validation (server-side, mirrors handbook §1.10.3 `validate`):

- `redline` must not use a judge executor (`kind=evaluator` whose evaluator is LLM-based) — code, trajectory or metric only.
- `cost` and `performance` criteria must use `kind=metric`.
- A judge criterion without a passing calibration record is **effective tier = observe** regardless of its declared tier (§5.5); the UI shows both.
- Text containing "让客户/客户觉得/users feel" style subjects is flagged (subject must be the agent).
- Every published set needs ≥1 criterion per dimension or an explicit "not applicable" note per missing dimension, and ≥1 red line.
- Summary (shown on the editor): tier distribution, judge share (<20% → every commit runs all; 20–50% → split quick/full; ≥50% → warn "split criteria"), and "effective gates" (gates minus uncalibrated judges).

Publishing freezes the version; editing a published set creates draft vN+1 with `parent_version = N`. A published version is immutable.

**Templates.** A `template` set holds criteria shared by every agent of one scenario (e.g. "industrial e-commerce customer service") and is versioned on its own. An `agent` set references one template version and stores only its **overrides** (same `key`, changed threshold/tier/executor/text) and **additions** (new keys); its effective criteria are `template vT ⊕ overrides ⊕ additions`, materialized into its own published version so lineage stays a single `criteria_set_version`.

- A new template version never changes an agent set silently: affected agent sets show "template v(T+1) available", with the diff; adopting it creates a new agent-set draft that the agent's business owner signs.
- Overrides may tighten a template red line (add conditions) but may not demote it to gate/observe; removing a template criterion requires a written reason on the agent set.
- Templates are workspace-scoped; promotion bundles carry the materialized agent set, so the target workspace does not need the template.

### 4.2 Golden set (黄金集) on top of EvalDataset

A golden set is an `EvalDataset` with `role = golden` plus three named splits. No new example store: the AWS Dataset remains the authority for content and versions.

New columns on `eval_datasets`: `role` (`scratch | golden`), `criteria_set_id`, `split_of` (parent golden dataset id, null for the parent), `split` (`dev | regression | holdout`).

Each split is its own AWS Dataset (so each has its own immutable versions); the parent row groups them. Holdout is never written by the admission flow (§7.6).

Per-item metadata, written into the existing free-form `metadata` field so it round-trips through AWS (`metadata.dlc`):

```
dlc: {
  case_tier: known_good | known_bad | ambiguous | adversarial
  criteria_ids: ["C-003", ...]              # which criteria this item exercises
  origin: manual | session | synthetic | issue | insight | public
  source_session, source_issue_id, cluster_id, fault_category
  expected_source: annotator | consensus | adjudicated | agent_observed
  annotators: [user...], adjudicator
  coverage_tags: [...]
  added_in_version, retired_in_version
}
```

`expected_source = agent_observed` (today's from-sessions output) is shown with a warning and cannot back a gate criterion until re-annotated.

### 4.3 Run extensions

New columns on `eval_runs`:

- `criteria_set_id`, `criteria_set_version`
- `agent_version`, `endpoint_qualifier` — what was actually invoked (§6)
- `evaluator_set_hash` — sha256 over sorted evaluator ids + their AWS `updatedAt`
- `split` — which golden split was run
- `repeats` — k (default 1); `repeat_mode`
- `cost_estimate` `{agent_usd, judge_usd, total_usd, basis}` and `cost_actual` (filled from span token usage + `tokenUsage` of evaluator results)
- `denominator` `{expected_items, invoked, evaluated, inconclusive, guardrail_blocked, errored}`
- `result_snapshot_key` — the persisted per-item result set (below)

New table `criterion_results` (persisted snapshot of the CloudWatch results stream, written when the run completes, so gate reports stay reproducible after log retention):

```
criterion_results
  id, workspace_id, run_id, criterion_key, scenario_id, attempt (1..k), session_id
  evaluator_id, level, raw_value, raw_label, explanation
  verdict          pass | fail | inconclusive | error
  error_code
```

Per-run aggregates are derived and cached on the run (`criteria_summary` JSON): per criterion `{n, pass, fail, inconclusive, rate, wilson_low, wilson_high, pass_k?}`.

### 4.4 Calibration

```
annotation_tasks
  id, workspace_id, criteria_set_id, criterion_key, dataset_id, dataset_version
  purpose          golden_answer | judge_calibration | admission
  item_refs        [scenario_id ...]       (15–20 per criterion to start; ≥10 hard minimum)
  annotators       [user | review_link_id]  (≥2, independent, blind to each other and to the judge)
  adjudicator      user
  status           open | labeling | adjudicating | closed
  created_by, created_at, closed_at

annotations
  id, task_id, item_ref, annotator, label (pass|fail|inconclusive | free text for golden answers),
  rationale, created_at
  unique(task_id, item_ref, annotator)

calibration_records
  id, workspace_id, criterion_key, criteria_set_version, evaluator_id, evaluator_updated_at
  task_id, run_id                         # the judge run over the same items
  n, human_human_kappa, judge_human_kappa, kappa_ci_low, kappa_ci_high
  confusion          {"pass/pass":n, "pass/fail":n, ...}
  position_flip_rate (pairwise judges only), self_consistency (repeat agreement)
  verdict            aligned | not_aligned | insufficient_n
  decided_by, decided_at, note
```

A criterion is **calibrated** for a given evaluator when its latest record has `verdict = aligned`, `judge_human_kappa ≥ max(0.61, human_human_kappa − 0.05)`, and the record is younger than the recalibration period and the evaluator's `updatedAt` has not changed since. The recalibration period (default 90 days) and the κ floor (default 0.61) are **workspace release-policy settings** (`release_policy.calibration = {period_days, kappa_floor}`), normalized like the existing policy fields in `services/release_gates.py`. Any of those failing demotes it to effective `observe` and raises a "recalibrate" inbox item.

### 4.5 Release record, waivers, lineage

```
release_records
  id, workspace_id, agent_id, promotion_id (nullable for in-place releases)
  candidate_version, candidate_endpoint, previous_live_version
  criteria_set_version, golden_versions {dev, regression, holdout}, evaluator_set_hash
  run_ids          [...]                   # the runs the gate read
  gate_report      JSON (§5.4, frozen at decision time)
  decision         released | blocked | invalid | rolled_back
  decided_by, decided_at, note
  waiver_ids       [...]
  rollback_target  {version, verified_drill_at}
  attachments      [{kind: load_test | doc | link, ref, note}]   # external evidence, e.g. a load-test report

waivers
  id, workspace_id, agent_id, criterion_key, criteria_set_version
  actual, threshold, reason, risk_owner, compensating_control
  expires_on       (required; max 30 days by policy)
  status           requested | approved | rejected | expired | revoked
  requested_by, approved_by (≠ requested_by), approved_at
```

Red-line criteria are never waivable (rejected at the API). On expiry the waiver no longer counts and the next gate evaluation blocks.

Lineage is the tuple `(criteria_set_version, golden split versions, evaluator_set_hash, agent_version, endpoint)` and is stamped on every run, gate report and release record. The scorecard (§7.9) and run comparison (§7.4) refuse to compare runs whose lineage differs in more than the dimension being compared, and say why.

### 4.6 Admission queue and watch

```
admission_candidates
  id, workspace_id, agent_id, source (feedback | issue | insight_failure | insight_intent | review | manual)
  source_ref, session_id, cluster_id, affected_sessions, fault_category
  proposed_criteria  [criterion_key]
  existing_judgement {criterion_key: pass|fail|inconclusive}   # what current evaluators say
  duplicate_of     candidate or dataset item ref
  redaction        {status: clean|redacted|blocked, entities: [...]}
  status           new | annotating | admitted | rejected | duplicate
  decided_by, decided_at, note, admitted_item_ref, admitted_version

watch_configs
  id, workspace_id, agent_id, criteria_set_id
  schedule         cron-like {every: daily|weekly, at, tz}
  split            regression | holdout
  repeats          k (cost-estimated, §5.3)
  online_config_ids [...]
  alert_rules      [ids]                   # count / score / distribution rules, §5.6
  enabled, last_run_id, last_checked_at, next_due_at
```

## 5. Engines

### 5.1 Criterion evaluation

When a run completes, `criteria_engine.snapshot(run)` reads the full results stream once (paging past today's 5 000-record cap with a cursor) and writes `criterion_results`. For each criterion in the run's criteria-set version:

- **evaluator executor** — one row per (scenario, attempt, evaluated unit at `level`). Verdict:
  - categorical evaluators: `label` mapped through `label_map`; unmapped labels → `inconclusive`;
  - numeric evaluators: `score_rule` applied (default: polarity-normalized value ≥ 0.5 ⇒ pass);
  - judge errors and missing target spans → `error` (never `pass`); the CustomCode contract already returns `errorCode`;
  - code evaluators may return the label `INCONCLUSIVE` (added to the platform's Lambda handler contract and to the judge rating-scale templates as a categorical option).
- **metric executor** — computed from the run's own spans (`observability` per-trace aggregates filtered to the run's session ids): P95 latency, first-token P95, tokens per session, cost per *successful* session (cost of all sessions ÷ sessions whose goal criterion passed; failed sessions are tagged `NOT_SUCCEEDED`). Includes judge cost when the criterion says so.
- **human executor** — verdicts come from a closed annotation task over the run's sessions.

Aggregation per criterion: `n = pass + fail` (inconclusive and error excluded from n but reported); `rate = pass / n`; Wilson 95% interval; redline: `violations = fail` (rate unused). `inconclusive / (n + inconclusive)` is the **undetermined rate**: < 2% fine, 2–5% "re-run advised", > 5% the run is INVALID for gating.

### 5.2 Denominator accounting

`denominator.expected_items` = items in the pinned split version × k. The engine counts `invoked`, `evaluated`, `guardrail_blocked` (session ended in `guardrail.blocked`), `errored`, `inconclusive`. A guardrail block **counts as a red-line pass only if** the criterion says so; otherwise it is a failure for the criterion it was guarding — it is never silently dropped (the "48 → 34 → 100%" trap). If `evaluated < expected_items` for any gate or red-line criterion the gate verdict is **INVALID**, not BLOCKED.

### 5.3 pass^k (opt-in)

Run option `repeats: k` (1–10, default 1) on the task wizard, per criterion override `pass_k {k, mode}`.

- Execution: `execute_run` replays each scenario k times in fresh sessions; session metadata carries `testScenarioId = scenario_id` and `metadata.attempt = i`, so one batch evaluates all attempts (AWS has no repetition parameter; `testScenarioId` may repeat).
- Aggregation: per scenario, `pass^k = all attempts pass` (mode `all`, default for unattended/irreversible actions) or majority; criterion pass^k = share of scenarios passing; also report `mean@k` and the **consistency gap** `mean@k − pass^k`.
- The gate uses pass^k only for criteria that declare it; others use attempt 1, so enabling k never silently changes a gate.

**Cost prompt.** Before submit, the wizard calls `POST /api/eval/runs/estimate` and shows:

```
items × k × (agent_cost_per_session + Σ judge evaluators × judge_cost_per_item)
```

- `agent_cost_per_session`: median tokens per session of this agent over the last 7 days (span token usage) × `model_prices`, falling back to the model's price × the dataset's average turn count × a default token budget, labelled "rough";
- `judge_cost_per_item`: the evaluator's recorded `tokenUsage` median from previous runs × judge model price; builtin/ThirdParty judges run on service capacity and are shown as "billed by AgentCore Evaluations" with the per-evaluation unit price when known;
- wall-clock estimate from the queue's concurrency;
- a confirmation step when `k > 1` and the estimate exceeds the workspace's `eval_cost_confirm_usd` (default $5), and a hard refusal above `eval_cost_max_usd` unless an admin overrides.

After the run, `cost_actual` is filled from the same sources and shown next to the estimate; repeated large gaps between estimate and actual tune the fallback constants.

### 5.4 Release gate (four gates, fixed order)

`gate_engine.decide(agent, candidate, criteria_version, runs, waivers) → GateReport`:

1. **Red line** — every `redline` criterion: `violations == 0` on regression + holdout splits. Any violation ⇒ `BLOCKED` (not waivable).
2. **Denominator** — §5.2 for every red-line and gate criterion; undetermined rate ≤ 5% ⇒ else `INVALID` ("results do not support any conclusion, including PASS").
3. **Per-dimension gates** — every `gate` criterion with effective tier gate (calibrated if judge): `rate ≥ threshold` (or pass^k ≥ threshold when declared); thresholds compared at integer-percent precision and flagged when `threshold` lies inside the Wilson interval ("not distinguishable at this n"). No weighted total. A failing criterion with an approved, unexpired waiver passes as `WAIVED`.
4. **Observe** — recorded with trend vs the previous release; never blocks.

Plus **provenance** checks that make the report reproducible: runs evaluated the *candidate* version through the candidate endpoint, on the pinned split versions, with the current evaluator set; holdout run exists and is newer than the last golden-set change; model lifecycle of every model in the spec is not `EOL` and Legacy models carry a rollback drill younger than 15 days.

Outcome: `PASS | BLOCKED | INVALID`, with per-criterion rows `{key, dimension, tier, effective_tier, n, rate, ci, threshold, verdict, waiver?}` and the three regression lists (§5.7). The report is frozen into the release record at decision time.

`release_gates.eval_gate` (pooled mean) is kept for agents **without** a published criteria set and labelled "legacy gate" in the UI; agents with a criteria set use only the new engine.

### 5.5 Calibration maths

Cohen's κ per criterion (binary or categorical; weighted κ for ordinal scales; Fleiss for ≥3 annotators; Krippendorff's α when labels are missing). Bootstrap 95% CI. Never accuracy, never Pearson. The workbench (§7.3) also computes human–human κ from the two independent annotations, which is the ceiling the judge must reach.

### 5.6 Statistics helpers

One module (`evaluation/stats.py`, pure functions, fully unit-tested): Wilson interval, two-proportion sample size (shown on A/B and canary: "≈432 sessions per arm to detect 5 pp at 90% baseline"), compound pass rate (`Π rate_i`, shown on the criteria editor: "8 criteria at 95% each ⇒ 66% of sessions pass all"), pass^k from repeats, κ variants, total variation distance for intent distributions, rolling median baselines.

### 5.7 Run comparison

`compare(run_a, run_b)`:

- refuses unless both runs share the criteria-set version and split version (or explains which differs);
- per criterion: Δrate with significance (two-proportion test), and per item the transition lists **fixed** (fail→pass), **new failures** (pass→fail), **still failing**;
- "two numbers" mode across a criteria/golden version bump: the new run is scored under both the old and the new version where items overlap, answering "did the agent get better" and "how much harder did the standard get" separately.

## 6. Release path: gate before traffic

### 6.1 Endpoint model

Every runtime-backed and harness agent gets a named endpoint **`live`**. All production invoke paths (`services/invoke.py` sync and streaming, JWT bearer, A2A, `/v1`, channels, share/review links, smoke) pass `qualifier = live`. `DEFAULT` keeps auto-rolling to the newest version but carries no production traffic; it becomes the "latest build" pointer.

An update (redeploy, rollback, experiment promote, promotion) therefore:

1. mints the new version (`UpdateAgentRuntime` / `UpdateHarness`) — DEFAULT moves, `live` does not;
2. creates or repoints endpoint **`candidate`** to that version;
3. runs the gate's evaluations against `qualifier = candidate` (`execute_run` gains an `endpoint_qualifier`; online/Insights data sources filter on the `<runtime>.candidate` service name);
4. on `PASS`: `UpdateAgentRuntimeEndpoint(live → version)` / `ensure_harness_endpoint(live)`; on `BLOCKED/INVALID`: `live` stays, the version remains for inspection;
5. rollback = repoint `live` to `previous_live_version` (no rebuild).

Canary keeps its own stable/treatment endpoints; on completion it repoints `live` (today it relies on DEFAULT having already rolled — F5 and §3 gap 4).

### 6.2 Release modes and migration

Workspace setting `release_mode`: `direct` (today's behaviour) | `gated`.

- `direct` — no change except that invokes already use `live`, kept in lock-step with DEFAULT after every successful deploy. This lets the invoke change ship first and safely.
- `gated` — §6.1 flow; required for agents with a published criteria set in a `prod`-tier workspace, optional elsewhere.

Default, in two steps: **(1)** when P3 ships, every workspace — including new `prod` ones — defaults to `direct` and `gated` is switched on per workspace; **(2)** once P3 has run in the field (exit criteria: ≥ 2 workspaces on `gated` for ≥ 4 weeks, no gate-engine defect blocking a release, rollback-by-repoint drilled), new `prod`-tier workspaces default to `gated`. Step 2 is a tracked follow-up, not an open-ended "manual forever"; existing workspaces are never flipped automatically.

Migration job (idempotent, resumable like other jobs): for each active agent create `live` pinned to its current `version`, wait READY, then flip the agent's `endpoint_mode` column from `default` to `live`. Agents created before migration keep working through DEFAULT until flipped. Discovered (imported) agents are never modified: they stay on DEFAULT and are marked "not gate-able".

### 6.3 What changes in existing release flows

| Flow | Today | Gated mode |
|---|---|---|
| Redeploy / snapshot rollback | DEFAULT rolls, no gate | candidate endpoint → gate → repoint live |
| Experiment promote | DEFAULT rolls; needs only a verdict artifact | requires winning verdict (F6) and the gate |
| Promotion execute (non-canary) | DEFAULT rolls before smoke; failure stays live (F4) | candidate → smoke → gate → live |
| Promotion execute (canary) | candidate behind stable; DEFAULT already rolled | unchanged ramp; complete repoints live |
| Release record | promotion row + audit | `release_records` row for every decision |

Release gates for promotions run in the **target** workspace against the target's criteria set (or the bundle's pinned version when the target has none, flagged).

## 7. Human-judgment workbenches

Each workbench is designed around one decision: who makes it, what evidence they need on screen, what they can do, and what the platform records. All are V2 pages using the `?view=` pattern; charts are hand-rolled SVG like the existing dashboard (§9.2).

### 7.1 Criteria editor and threshold assistant — *business owner defines and signs*

Route `/v2/eval/criteria?agent=…` (`?view=set&id=…&v=…`).

Shows:
- the criteria table (key, text, dimension, tier, effective tier, executor, level, threshold, denominator, owner) grouped by dimension in fixed order, with the validation findings inline;
- **threshold assistant** per criterion: V0 baseline rate with Wilson interval at the current golden-set size, human baseline (entered), business-acceptable floor (entered), and the resulting threshold proposal "baseline + one step"; a warning when the threshold falls inside the CI;
- **compound panel**: product of all gate thresholds ("all 8 gates at 95% ⇒ 66% of sessions clear every gate"), and the per-criterion threshold needed for a target overall rate;
- **ladder hint**: for each judge criterion, "could this be a code assertion?" checklist (structure/format/number/latency/cost → CustomCode; order → TrajectoryInOrderMatch);
- coverage: number of golden items per criterion per tier (known good / bad / ambiguous / adversarial), red where < 3.

Actions: create from a template (pick scenario → template version) or blank, edit overrides/additions against the template (inherited rows marked), adopt a newer template version (diff + re-sign), save a set as a new template, edit draft, import from the architect evaluation plan (maps `blocking → gate`, `threshold → threshold`, golden tests → items, fishbone dimension → dimension), publish, **sign** (requires `criteria.sign`; signer ≠ last editor unless admin; records name, time, note), diff two versions.

### 7.2 Golden-set curator — *engineer + business build the examples*

Route `/v2/eval/data?view=golden&id=…`.

Shows: split sizes and versions (dev / regression / holdout), a **criteria × items coverage matrix** (cells = which criteria an item exercises, coloured by case tier), source mix (production / tickets / expert / synthetic / regression-from-fix) with the bias note for each source, items with `expected_source = agent_observed` highlighted, retired items, and per-version change log ("+12 from issues, −3 retired, holdout untouched").

Actions: add / edit items (with `metadata.dlc`), move between dev and regression (never into holdout except at creation), retire (moves to a regression pool, out of gate denominators), publish split versions (AWS `CreateDatasetVersion`), open an annotation task for items lacking human expected answers.

### 7.3 Annotation and calibration bench — *two annotators, then judge vs human*

Routes `/v2/eval/calibration?criterion=…` (console) and `/r/annotate/<token>` (account-free, reusing the share-link primitive `services/share_links.py:92` with `kind = annotate`). Account-free links are allowed only in `dev` and `staging` tier workspaces; in a `prod` tier workspace annotation requires a console account, the link-creation API refuses `kind = annotate`, and existing annotate links stop resolving if the workspace is re-tiered to `prod`.

Annotator view: one item at a time — transcript, trace summary (tool calls, retrieved context), the criterion text and its rubric, positive/negative examples; label pass / fail / inconclusive + rationale. Annotators never see each other's labels or the judge's until the task closes; items are presented in random order, not grouped by category (anchoring).

Calibration view (per criterion):
- human–human κ and judge–human κ side by side, with bootstrap CI and the Landis–Koch band ("≥0.80 auto-gate · 0.61–0.79 gate with human review of hard failures · ≤0.60 observe only");
- confusion matrix (judge × adjudicated human);
- disagreement list with both rationales and the judge explanation — the primary repair tool;
- position-swap flip rate (pairwise judges) and self-consistency across repeats;
- history: κ per rubric revision, so "score went up but κ did not" is visible;
- verdict buttons: **aligned → may gate** / **not aligned → observe** (requires `judge.calibrate`); the record is versioned with the criterion.

Guidance shown when κ is low: split the criterion, add 2–3 positive/negative examples to the rubric, re-label a round — before swapping the judge model.

### 7.4 Run comparison and attribution — *engineer decides what to change*

Route `/v2/eval/tasks?view=compare&runs=a,b,c…`.

Shows:
- the **fix ladder**: a sequence of runs on the same split version, one row per run with its change note (from the snapshot diff), overall gate status, red-line status and per-dimension rates — the "47.6 → 52.4 → 71.4 → 76.2 → 100" view;
- per criterion: Δ with significance and the three item lists (fixed / new failures / still failing);
- "two numbers" when versions differ (§5.7);
- drill-down: session → trajectory (span tree) → tool call (arguments/result when the opt-in OTel fields are enabled; a banner explains when they are not);
- **per-tool table**: calls, mis-selected (trajectory mismatch), missing (expected but not called), error rate;
- **seven-layer attribution hint** per failure cluster (01 context · 02 decision rules · 03 tools/skills · 04 orchestration · 05 runtime · 06 model · 07 permissions/guardrails), derived from the failing criterion's `attribution_layer`, trajectory evidence and pass^k gap (large `mean@k − pass^k` ⇒ suspect 05/06), with "change one layer at a time" enforced as a warning when the compared snapshots differ in more than one layer.

### 7.5 Gate report and sign-off — *signer releases or blocks*

Rendered on the promotion detail and on the agent's release card (`?view=release&record=…`).

Shows, in gate order: red lines (violations, sample sessions); denominator panel (expected / invoked / evaluated / blocked / errored / inconclusive, undetermined rate); per-dimension gates (rate, CI bar with threshold marker, effective tier, waiver chip); observe items with trend vs last release; provenance (five-version lineage, model lifecycle, rollback target + drill date); new failures since the last release; attached external evidence (e.g. a load-test report).

Actions: **release** (only when PASS; requires `promotion.approve` or `release.sign`; signer ≠ requester; records the decision), request waiver for a failing gate criterion (opens §7.5.1), **block** with note, re-run gate evaluations.

#### 7.5.1 Waiver approval — *risk owner*

Inbox item and modal: criterion, actual vs threshold with CI, reason, compensating control, expiry (≤30 days), how many times this criterion has been waived, open waivers count and oldest age. Approve requires `waiver.approve` and approver ≠ requester. Red lines show "not waivable".

### 7.6 Admission review — *business owner decides what becomes standard*

Route `/v2/issues?view=admission` (next to the existing issue box).

Candidate sources, merged and de-duplicated: thumbs-down and SME review verdicts (`chat_feedback`), issues, Insights FailureAnalysis root causes and UserIntent clusters (with `affectedSessionCount`), and online-eval failures.

Shows per candidate: transcript with **PII redaction preview** (`guardrail.screen(mode="anonymize")` entities highlighted; candidates that cannot be redacted are blocked from admission); the cluster it belongs to and affected sessions; **what the current evaluators say** on it (judged wrong ⇒ prioritized, it is both a new case and a calibration sample); nearest existing golden items to flag duplicates — exact and normalized-text match (case, whitespace, punctuation, full/half-width folded) on the first user input, no embedding call; the reviewer sees the closest matches and decides; proposed criteria; the source's bias label.

Actions: admit to dev or regression split (never holdout) with a human-written or human-confirmed expected answer (`expected_source` recorded), reject with reason, mark duplicate (counts toward the cluster), open an annotation task, open a criteria change ("this is a new boundary — add a criterion"). Admission requires `golden.admit`; admitted items enter a draft split version; publishing the version shows the before/after pass rates of the last run on both versions.

Queue view sorts clusters by impact (affected sessions ÷ estimated fix cost when an engineer has set one) and shows the noise count separately.

### 7.7 Watch and drift triage — *on-call decides agent drift vs judge drift*

Route `/v2/eval/watch?agent=…`.

Shows:
- per-dimension time series of online criterion scores (from online-eval series per evaluator mapped to criteria) with a rolling-median baseline band, scheduled re-run points (watch config runs on regression/holdout) and release markers;
- the three alert families separately: **count** (red-line violations, errors — absolute, page immediately), **score** (relative to rolling median, quiet below 8 days of baseline, daily digest), **distribution** (intent mix via TVD, naming the component that moved: "returns 8% → 21%");
- "no alert" heartbeat row so silence is distinguishable from a dead monitor;
- **judge-drift check**: re-run the frozen calibration set; if the judge's labels changed on items humans labelled, it is judge drift, not agent drift;
- low-score share (tail) next to the mean, since tails hide under averages;
- current sampling settings (trace sampling, Transaction Search index %, online-eval sampling %) and log retention vs the comparison window.

Actions: acknowledge, open admission candidates from a cluster, roll back `live` (one click, audited), pause a broken online evaluator. On-call cannot edit criteria, thresholds or sampling from this page (handbook §6 on-call boundary).

### 7.8 Human-handoff point — *business owner chooses reliability vs cost*

Panel on the criteria editor: for agents with a confidence or escalation signal, a table of operating points from the latest run — autonomous accuracy, handoff share, achievable reliability (assuming a recorded human review success rate), cost per successful session — and the owner selects one row. The selection becomes three criteria: "low confidence must hand off" (redline), "handoff share ≤ chosen value" (gate), "handoff accuracy" (observe).

### 7.9 Agent scorecard — *everyone, one glance*

Agent detail `?tab=dlc`: five dimension tiles in fixed order, each with current vs standard, effective gates count, red-line status, last gate verdict, trend sparkline; coverage × pass rate (not pass rate alone); open waivers; calibration debt (judges due for recalibration); lineage of the live version; link into every workbench above.

## 8. Roles, permissions, audit

New permission keys (added to `AGENT_PERMISSIONS`, `DEFAULT_BY_ROLE`, `route_policy.py`, `frontend/src/lib/api.ts`):

| Key | Default holders | Guards |
|---|---|---|
| `criteria.manage` | member, operator | edit criteria drafts, golden-set curation |
| `criteria.sign` | none by default (granted to named business owners); admin | publish/sign a criteria set |
| `golden.admit` | none by default; admin | admit candidates, publish golden split versions |
| `judge.calibrate` | none by default; admin | record calibration verdicts |
| `waiver.approve` | operator; admin | approve waivers |
| `release.sign` | operator; admin | sign gated in-place releases (promotions keep `promotion.approve`) |

Account-free annotators use `annotate` share links (scoped to one task, expiring, revocable), allowed in `dev`/`staging` tier workspaces only; `prod` tier requires console accounts (§7.3). Separation rules enforced server-side: signer ≠ last editor of the set, release signer ≠ requester, waiver approver ≠ requester (admins included, matching `review_promotion`).

All decisions write `audit_events` in the same transaction. New read API `GET /api/audit?target=…&action=…` (admin, plus `criteria.sign` holders for their agents) feeds a "decision history" panel on each workbench.

Prod-tier protection (`PROD_PROTECTED`) covers: criteria publish/sign, golden version publish, waiver approve, release sign, `live` repoint/rollback, watch config changes.

## 9. API and frontend surface

### 9.1 API (all under `/api`, classified in `route_policy.py`)

```
criteria-sets           GET (?kind=template|agent), POST
criteria-sets/{id}/adopt-template  POST  (agent set → new draft on template vT)
criteria-sets/{id}      GET (?version=), PUT (draft only), DELETE (draft only)
criteria-sets/{id}/publish | /sign | /diff?a=&b= | /import-plan
criteria/{key}/coverage
eval/runs/estimate      POST   (cost + duration estimate, §5.3)
eval/runs               POST   + criteria_set_version, split, repeats, endpoint_qualifier
eval/runs/{id}/criteria GET    (criterion_results summary + items)
eval/runs/compare       GET    ?runs=a,b
eval/golden/{id}        GET, PUT splits; /publish?split=
annotation-tasks        GET, POST; /{id} GET; /{id}/labels POST; /{id}/close POST
calibration/{criterion} GET history; POST verdict
agents/{id}/gate        POST evaluate candidate; GET latest report
agents/{id}/release     POST sign | block; POST rollback
release-records         GET (?agent=), /{id} GET
waivers                 GET, POST; /{id}/approve | /reject | /revoke
admission               GET (?status, ?agent); /{id}/admit | /reject | /duplicate
watch                   GET, POST, PUT; /{id}/run-now
audit                   GET
share/annotate/{token}  GET, POST  (public, rate-limited like review links)
```

### 9.2 Frontend

- Nav (eval group): add **Criteria**, **Calibration**, **Watch**; Admission lives under Issues; Compare under Tasks; Gate report under Promotions and agent detail; scorecard as an agent-detail tab.
- New shared V2 components: `Sparkline`, `BandChart` (series + baseline band + markers), `CiBar` (rate, interval, threshold marker), `ConfusionMatrix`, `CoverageMatrix`, `LadderChart`, `TierChip`, `DimensionTiles`. Hand-rolled SVG like `observability/Dashboard.tsx`; no new chart dependency.
- `Table` gains a row-selection API (today pages hand-roll checkboxes, `data/Traces.tsx:78`).
- Task wizard: dataset **version** picker (missing in V2 today), split picker, criteria-set picker, repeats with live cost estimate.
- All strings en + zh-CN; methodology terms keep the handbook's Chinese wording (判据, 红线, 门禁, 观测, 黄金集, 认知/质量/责任/成本/性能).

## 10. Background scheduler

A single in-process scheduler thread started in `main.py` lifespan (same pattern as `model_prices.start_auto_refresh`), ticking every 60 s:

- due `watch_configs` → submit eval runs (respecting the eval queue and cost limits; a run whose estimate exceeds the cap is skipped and alerts instead);
- alert rule evaluation (fixes the "only when someone asks" gap) with transition-only notification;
- calibration expiry and waiver expiry sweeps → inbox items;
- Insights batch for agents with clustering enabled.

Single-writer safety: each task claims a ledger row (`scheduler_claims(task, due_at, claimed_by, claimed_at)`) with a conditional update, so a second backend process does not double-run; claims older than 10 minutes are reclaimable. Resumable like existing jobs on restart.

## 11. AWS constraints and how the design handles them

| Constraint (verified in the installed service models, botocore 1.43.103 / bedrock-agentcore 1.17.0) | Handling |
|---|---|
| No gate, threshold or pass-criteria object anywhere in AgentCore | gate engine is platform-side (§5.4); evidence frozen in release records |
| No per-scenario repetition parameter | platform replays k sessions per scenario, `testScenarioId` repeats (§5.3) |
| `GetBatchEvaluation` returns aggregates only; per-session results only in the CloudWatch output log group | snapshot into `criterion_results` at completion; retention ≥ comparison window enforced |
| Categorical rating scales have free-form labels, no INCONCLUSIVE | platform adds an `INCONCLUSIVE` label to its judge templates and code-evaluator contract; `label_map` per criterion |
| Ground-truth evaluators cannot be used in online evaluation | `criteria.online` derived; watch shows the offline-only criteria as "scheduled re-run only" |
| Online eval: ≤ 25 evaluators per config in the model (platform keeps its 10 limit), `samplingPercentage` 0.01–100 is a percent | config builder splits red-line (100% or guardrail) and gate (10–20%) configs; UI shows "%" explicitly |
| Custom evaluators referenced by an ENABLED online config are locked | calibration changes create a cloned evaluator + new criteria version; the watch config switches atomically |
| Datasets, Insights, Recommendations, A/B are preview; Optimization calls are not in CloudTrail | wrappers stay in `agentcore_eval.py`; release record notes "optimization evidence not in CloudTrail" |
| A/B tests: ≤ 2 variants, gateway-only, results with pValue + CI | unchanged; sample-size helper added; promote requires a winning verdict (F6) |
| Runtime and harness endpoints exist; invoke accepts `qualifier` | `live` / `candidate` endpoints (§6) |
| OTel GenAI `tool.call.arguments/result` are opt-in; `conversation.id` conditionally required | build-ring **field-alignment check** per agent (§12, P1) reports missing fields; templates turn them on |

## 12. Phasing

Each phase follows `docs/roadmap.md` definition of done (hermetic tests, i18n parity, architecture section, `make verify`, e2e on the throwaway stack twice, security review before push).

| Phase | Scope | Unlocks | Delivered |
|---|---|---|---|
| **P0 — foundations** | F1–F6 fixes; `evaluation/stats.py`; persist `criterion_results`; run lineage columns; V2 dataset-version picker | trustworthy numbers | ✅ |
| **P1 — define + gate** | `criteria_sets/criteria` + editor (§7.1, import from assistant plan); criterion engine (§5.1–5.2); gate report (§5.4, §7.5) on the existing pooled-mean path as a second gate; INCONCLUSIVE label; field-alignment check | workshop core: define → evaluate → "red line blocks" demo | ✅ |
| **P2 — calibration + golden lifecycle** | annotation tasks, annotate links, κ engine, calibration bench (§7.3); golden splits + coverage matrix (§7.2); effective-tier demotion | "uncalibrated judges cannot gate" | ✅ |
| **P3 — gated release** | `live`/`candidate` endpoints, migration job, `release_mode`, release records, waivers, rollback-by-repoint (§6, §4.5); run comparison + fix ladder (§7.4) | gate before traffic; F1→F5 ladder demo | ✅ |
| **P4 — pass^k + cost** | repeats in `execute_run`, estimate endpoint, wizard prompt, actual-cost accounting (§5.3) | reliability gating with cost awareness | ✅ |
| **P5 — observe + feedback** | scheduler (§10), watch configs and drift bench (§7.7), admission queue (§7.6), two-number reporting, scorecard (§7.9), handoff-point panel (§7.8), audit read API | the flywheel turns unattended | ✅ |

Workshop readiness: P0 + P1 support the 1-day criteria workshop (define, build, evaluate, block on red line, fix, re-run); P2 + P3 make the release-gate story true on the platform itself.

**Delivered 2026-10-07.** All six phases are implemented, with the console at
`/v2/eval/standards` (nine `?view=` sub-pages), 56 routes in `app/routers/dlc.py` plus
the public `/share/annotate/*` pair, and the loop verified against real AWS by
`backend/scripts/e2e_agent_dlc.py` (38 checks) and `e2e_agent_dlc_browser.py`
(nine views × two languages). Three things the real-AWS run changed in the design's
assumptions, all now in `docs/architecture.md`: telemetry is **per endpoint** (a gate
run must read `…-candidate`, a gated agent's dashboards must read `…-live`), endpoint
deletion is **asynchronous** (the agent delete has to wait it out), and the cost
estimate is **enforced** on both entry points rather than only displayed.

## 13. Testing

- Hermetic unit tests for every engine (stats, criterion verdict mapping, denominator, gate order, waivers, κ) with table-driven cases taken from the handbook's worked examples (48/34 denominator trap, 0.95⁸ compound, κ = 0 rubber-stamp judge, 18 → 30 golden-set drop).
- Contract tests on the snapshot reader against recorded CloudWatch result records.
- Route-policy drift tests extended to the new namespaces and `PROD_PROTECTED` entries.
- E2E `backend/scripts/e2e_agent_dlc.py`: create criteria set (incl. one red line on a CustomCode evaluator) → golden set → run V1 (red line fails) → gate BLOCKED → fix prompt → run V2 → gate PASS → repoint `live` → rollback by repoint; pass^k run with k=3 and estimate vs actual; calibration with a scripted annotator; cleanup of every `rm-e2e-*` resource.

## 14. Out of scope

- Load / stress / concurrency generation (performance criteria read traces; external load-test reports can be attached to a release record).
- Automatic edits to criteria, thresholds or golden answers.
- Self-evolution of layers 06 (model/parameters) and 07 (permissions/guardrails).
- Replacing customers' existing tracing stacks (Langfuse, Phoenix, …): OTel-compatible data is read, not migrated.

## 15. Decisions (resolved 2026-10-06)

1. **Criteria sets are templated.** Scenario templates are versioned independently; agent sets inherit a template version plus overrides and additions (§4.1, §7.1).
2. **Account-free annotation links in `dev`/`staging` only.** In `prod`-tier workspaces only console accounts annotate, so every label is attributable (§7.3, §8).
3. **Calibration period and κ floor are workspace release-policy settings**, defaults 90 days and 0.61 (§4.4).
4. **Admission dedupe uses exact + normalized-text matching**; no embedding calls for now (§7.6).
5. **`release_mode` default in two steps:** manual opt-in when P3 ships; after P3 meets its field exit criteria, new `prod`-tier workspaces default to `gated` (§6.2).
