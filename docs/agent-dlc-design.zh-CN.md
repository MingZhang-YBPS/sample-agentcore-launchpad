# Agent-DLC 方法论对齐 — 设计文档

状态：**评审稿** · 分支：`feat/agent-dlc` · 负责人：平台团队 · 英文版：[agent-dlc-design.md](agent-dlc-design.md)

本文设计 Launchpad 如何在平台上端到端落地 Agent-DLC 方法论（Agent Development
Lifecycle，评估驱动部署）。依据材料：《Agent-DLC · 架构师实践手册》、L200 TTT deck、
FCD deck、《企业智能体评估方法论》workshop deck、网易 briefing 与 OnePage。凡方法论
要求**由人判断**的地方，平台不替人决定：平台负责记录决定、执行决定，并在一个界面上
给出做这个判断所需的全部证据。

已确定的决策：

- **压测 / 并发测试不在平台范围内。** 性能维从 trace 读取（P50/P95、首 token 延迟）
  并据此设门禁；外部压测报告可作为附件挂到发布记录上，平台本身不产生压测流量。
- **pass^k 为可选的运行选项。** 选择 k > 1 时，提交前先展示成本估算，运行结束后记录
  实际花费。

---

## 1. 设计原则

1. **判据是基本单位。** 评估器、黄金样本、阈值、门控结论、漂移告警、入集决定，全部
   挂在一行带版本的判据上。不挂在判据上的分数只是证据，不是结论。
2. **尺子归人，平台执行。** 业务方写判据与阈值，标注人写标准答案，具名的人决定入集、
   签字放行。平台从不自行修改判据、阈值或标准答案（方法论原话：「回流可以自动化取
   样本，不能自动化定期望」）。
3. **确定性优先。** 界面引导每条判据沿评估器阶梯向上放：代码断言 → 轨迹匹配 → 内置
   评估器 → 自定义裁判 → 人工；裁判型判据在校准通过之前不能做门禁。
4. **先过门控，再接流量。** 新版本在具名端点后面接受评估；门控通过后生产流量才切换。
5. **没有血缘就不算数。** 每个分数都带上产生它的五个版本：判据集、黄金集、评估器集、
   agent 版本、端点。
6. **沿用现有架构约定。** AWS 是事实来源；ledger 只存标识与派生结果；所有客户端由
   `services/aws_clients.py` 构造；预览 API 的变化限制在 `agentcore/` 与
   `evaluation/agentcore_eval.py` 内；新路由在 `route_policy.py` 中分类；只做 V2；
   所有文案走 i18n。

## 2. 方法论 → 平台映射

| 环 | 方法论交付物 | 平台对象 | 支撑的人工决定 |
|---|---|---|---|
| ① 定义 | 判据表 + 第一批黄金集，业务方签字 | `CriteriaSet` vN + 黄金集（数据集）vN | 写判据、定档位与阈值、签字 |
| ② 构建 | 可运行且**可评估**的 agent | agent + 快照 + **埋点字段对齐检查** | —（工程） |
| ③ 评估 | 分维评分报告、失败清单、校准记录 | `EvalRun` + `CriterionResult` + `CalibrationRecord` | 校准裁判、失败归因 |
| ④ 发布 | 门控报告、发布记录、可回滚版本 | `GateReport` + `ReleaseRecord` + 具名 `live` 端点 | 签字放行、审批例外 |
| ⑤ 观测 | 线上趋势、漂移告警、成本/延迟看板 | `WatchConfig` + 定时检查 + 告警 | 判定漂移来源（agent 还是裁判） |
| ⑥ 回流 | 扩充后的黄金集、修订后的判据 | `AdmissionCandidate` 队列 → 新版本 | 决定入集、判据升版 |

五个维度是每条判据上的固定枚举：
`cognition | quality | responsibility | cost | performance`（认知 / 质量 / 责任 / 成本 / 性能），
界面上处处按这个顺序排列。

三个档位：`redline` 红线（布尔，0 违例，不参与加权，不可例外）、`gate` 门禁（阈值，
逐维判定，可申请带到期日的例外）、`observe` 观测（只记录，永不阻断）。

## 3. 现状与差距

现有可复用能力（文件路径相对 `backend/app/`，另有说明除外）：

| 能力 | 位置 | 复用为 |
|---|---|---|
| 本地数据集 + 同步 AWS Dataset、不可变版本 | `evaluation/models.py:21` `EvalDataset`、`evaluation/routers.py:459,578` | 黄金集存储与版本 |
| 批量评估：平台侧回放场景、重试、预算截停 | `evaluation/service.py:263` `execute_run` | 运行引擎；pass^k 接入其场景循环 |
| 逐会话、逐评估器的 label/value/explanation | `evaluation/service.py:676` `run_results`（从 CloudWatch 实时读取，**未落库**） | 判据结果的输入（需落快照） |
| Builtin / ThirdParty / Custom / CustomDerived / CustomCode 评估器、轨迹匹配 | `evaluation/agentcore_eval.py:88,117,603,672,746` | 判据执行器 |
| 在线评估配置 + 逐评估器时间序列 | `evaluation/online.py:569` | 观测信号 |
| Insights（FailureAnalysis / UserIntent / ExecutionSummary），带受影响会话数 | `evaluation/agentcore_eval.py:519,553` | 入集候选、影响面排序 |
| 会话 → 数据集构建器（含去重） | `evaluation/pipeline_routers.py:181` | 入集的「采纳」路径 |
| 点赞点踩、问题箱（含修复记录与状态轨迹）、SME 评审链接 | `services/feedback.py`、`services/issues.py`、`services/review_links.py` | 入集来源；免登录标注链接 |
| 架构助手评估计划（黄金用例、带 `blocking`/`threshold` 的评估器条目、五维鱼骨图） | `assistant/evaluation_plan.py:221,451`、`assistant/proposal.py:127` | 导入为 `CriteriaSet` |
| 规格快照、发布包、发布申请、发布闸门、canary、实验 | `services/snapshots.py`、`services/promotion*.py`、`services/release_gates.py`、`optimization/*` | 发布路径、门控载体、A/B 证据 |
| Runtime 与 harness 具名端点、调用时的 `qualifier` | `services/agentcore/runtime.py:314-426`、`services/agentcore/harness.py:112-187` | `live` / `candidate` 端点 |
| 权限键、审计事件、禁止自审 | `services/users.py:32`、`services/audit.py:16`、`services/promotion.py:399` | 签字审批的范式 |

本设计要补的差距：

1. 没有判据对象；评估计划里的 `blocking`/`threshold` 只做展示（`assistant/evaluation_plan.py:228`）。
2. 发布闸门只是把所有评估器均分再取一个平均值，与 `min_eval_score` 比较（`services/release_gates.py:263-305`）；没有红线、没有分母核对、没有逐维门禁、没有 INCONCLUSIVE。
3. 逐场景结果不落库；运行不关联 agent 版本、评估器集版本或判据版本。`promotion._evaluation_of` 取的是「最近一次完成的运行」，不管它评的是什么。
4. 生产流量与离线评估都打 `DEFAULT`，而 DEFAULT 每次更新都会自动切到新版本，所以每次重新部署、回滚、实验 promote、非 canary 发布，都在任何门控**之前**就已上线。
5. 没有 pass^k、没有校准（κ）、没有 Wilson 区间、没有样本量提示、没有评估成本核算。
6. 黄金集没有拆分（开发 / 回归 / 留出），样本没有来源信息，入集没有审核。
7. 没有后台调度（只有每小时一次的模型价格刷新），所以没有定时重评、漂移检测和无人值守告警。
8. 没有例外审批、没有带血缘的发布记录、没有审计查询接口。

### 3.1 摸底中发现的前置修复

以下是 `main` 上本设计依赖的缺陷或不一致，最先修复，每项附回归测试。

| # | 缺陷 | 位置 |
|---|---|---|
| F1 | 错误率与延迟告警永远读到「未知」：`_dashboard_value` 在顶层读 `traces/errors/p95_ms`，而 `get_dashboard` 把它们放在 `tiles` 下；测试桩用的是扁平结构，掩盖了问题。`30d` 窗口在 `RANGE_HOURS` 里会抛 `KeyError`。 | `services/alerts.py:84-103`、`tests/test_costs_alerts.py:270` |
| F2 | 成本报表按 `agent.name == service.name` 匹配，但 service name 带端点后缀（`<runtime>.DEFAULT`），大部分花费落到「未知」。 | `services/costs.py:121-134`（改用 `observability.build_agent_resolver`） |
| F3 | `agent_versions.CANARY_ENDPOINTS` 按字面量 `stable`/`treatment` 匹配，实际名称是 `stable<id6>`/`treat<id6>`。 | `services/agent_versions.py:31`、`optimization/canary_service.py:647` |
| F4 | 非 canary 的发布 smoke 失败后仍留在线上（只撤销 canary）。 | `services/promotion_exec.py:882` |
| F5 | canary `act_complete` 直接写 `agent.spec`/`version`，不记录规格快照。 | `optimization/canary_service.py:1095-1104` |
| F6 | 实验 `promote` 只要求存在 verdict 产物，不要求 treatment 胜出或显式确认放行。 | `optimization/service.py:1460-1468` |

## 4. 领域模型

所有新表都按工作区隔离（加入 `WORKSPACE_SCOPED_TABLES`，`core/db.py:23`），在 `init_db` 注册，只用追加式 `ALTER TABLE` 演进。JSON 列存结构化子对象；凡是 AWS 拥有的状态只存标识，使用时回读。

### 4.1 判据集与判据（判据表）

```
criteria_sets
  id, workspace_id
  kind               template | agent
  agent_id           模板为空
  template_id, template_version   （仅 agent 判据集：继承的模板版本）
  name, description, scenario      （scenario：场景标签，用于推荐模板）
  version            整数，单调递增；草稿 = 最高的未发布版本
  status             draft | published | superseded
  signed_by, signed_at, sign_note        # 业务方签字（§7.1）
  parent_version     草稿复制自哪个版本
  source             manual | assistant_plan | import
  created_by, created_at, updated_at
  unique(workspace_id, kind, agent_id, name, version)

criteria
  id, workspace_id, set_id, set_version
  key                跨版本稳定的标识，如 "C-003"（血缘锚点）
  text               判据原文；主语必须是 agent，谓语必须可观测
  dimension          cognition | quality | responsibility | cost | performance
  tier               redline | gate | observe
  threshold          门禁：0..1 的通过率；红线：null（恒为 0 违例）；
                     成本/性能：{metric, op, value, unit}，如 p95_ms <= 3000
  level              session | trace | tool_call
  executor           {kind: evaluator | metric | human,
                      evaluator_id?, metric?: latency_p95|first_token_p95|cost_per_success|tokens_per_session,
                      label_map?: {pass: [...], fail: [...], inconclusive: [...]},
                      score_rule?: {op: ">=", value: 0.5}}       # 数值 → 单条通过/不通过
  denominator        sessions | turns | fields（业务方确认，同一版本内冻结）
  expected_type      deterministic | redline | trajectory | compliance | soft | efficiency
  online             offline_only | online_ok（派生：用到标准答案占位符 ⇒ offline_only）
  pass_k             null | {k, mode: all | majority}（可选，§5.3）
  calibration_required  布尔（裁判型执行器为 true）
  attribution_layer  01..07（§7.4 七层），可选提示
  owner              业务负责人用户名
  examples           [{dataset_item_ref, polarity: positive|negative, note}]
  notes
```

服务端校验（对应手册 §1.10.3 的 `validate`）：

- 红线不得使用裁判型执行器（基于 LLM 的评估器），只能用代码、轨迹或指标。
- 成本与性能判据必须用 `kind=metric`。
- 没有通过校准的裁判型判据，**有效档位一律为观测**，不论声明的档位；界面同时显示两者。
- 判据原文出现「让客户 / 客户觉得」一类主语时给出提示（主语必须是 agent）。
- 每个已发布的判据集每一维至少一条判据，或对缺失的维度写明「不适用」；至少一条红线。
- 编辑器上的汇总：三档分布；裁判型占比（<20% 每次提交全量跑；20–50% 拆快集/全集；≥50% 提示拆判据）；「实际生效的门禁数」（门禁数减去未校准裁判）。

发布即冻结版本；编辑已发布的判据集会生成草稿 vN+1，`parent_version = N`。已发布版本不可修改。

**模板。** `template` 判据集存放同一场景（如「工业品电商客服」）下所有 agent 共用的判据，单独管理版本。`agent` 判据集引用一个模板版本，只保存自己的**覆盖项**（同一 `key`，改了阈值 / 档位 / 执行器 / 原文）与**新增项**（新的 key）；它的有效判据 = `模板 vT ⊕ 覆盖项 ⊕ 新增项`，并物化为自己的已发布版本，所以血缘仍只有一个 `criteria_set_version`。

- 模板出新版本不会悄悄改变 agent 判据集：受影响的判据集显示「模板 v(T+1) 可用」及差异；采用新版本会生成新的 agent 判据集草稿，由该 agent 的业务负责人签字。
- 覆盖项可以收紧模板里的红线（加条件），不能把红线降为门禁 / 观测；删除模板判据必须在 agent 判据集上写明理由。
- 模板按工作区隔离；发布包携带物化后的 agent 判据集，目标工作区不需要有这个模板。

### 4.2 黄金集：建在 EvalDataset 之上

黄金集是 `role = golden` 的 `EvalDataset`，带三个具名子集。不新建样本存储：内容与版本仍以 AWS Dataset 为准。

`eval_datasets` 新增列：`role`（`scratch | golden`）、`criteria_set_id`、`split_of`（父黄金集 id，父行为空）、`split`（`dev | regression | holdout`）。

每个子集各是一个 AWS Dataset（各自有不可变版本），父行把它们归组。入集流程永远不写留出集（§7.6）。

样本级元数据写进现有的自由格式 `metadata` 字段，随 AWS 往返（`metadata.dlc`）：

```
dlc: {
  case_tier: known_good | known_bad | ambiguous | adversarial
  criteria_ids: ["C-003", ...]              # 这条样本检验哪些判据
  origin: manual | session | synthetic | issue | insight | public
  source_session, source_issue_id, cluster_id, fault_category
  expected_source: annotator | consensus | adjudicated | agent_observed
  annotators: [用户...], adjudicator
  coverage_tags: [...]
  added_in_version, retired_in_version
}
```

`expected_source = agent_observed`（即现在 from-sessions 产出的样本）会带警示展示，重新标注前不能支撑门禁判据。

### 4.3 运行的扩展

`eval_runs` 新增列：

- `criteria_set_id`、`criteria_set_version`
- `agent_version`、`endpoint_qualifier`：实际调用的版本与端点（§6）
- `evaluator_set_hash`：排序后的评估器 id 与其 AWS `updatedAt` 的 sha256
- `split`：跑的是哪个黄金子集
- `repeats`：k（默认 1）；`repeat_mode`
- `cost_estimate` `{agent_usd, judge_usd, total_usd, basis}` 与 `cost_actual`（由 span token 用量和评估结果的 `tokenUsage` 计算）
- `denominator` `{expected_items, invoked, evaluated, inconclusive, guardrail_blocked, errored}`
- `result_snapshot_key`：落库的逐条结果集（见下）

新表 `criterion_results`（运行完成时对 CloudWatch 结果流做快照，即使日志过期，门控报告也可复现）：

```
criterion_results
  id, workspace_id, run_id, criterion_key, scenario_id, attempt (1..k), session_id
  evaluator_id, level, raw_value, raw_label, explanation
  verdict          pass | fail | inconclusive | error
  error_code
```

逐判据的运行汇总派生后缓存在运行上（`criteria_summary` JSON）：每条判据 `{n, pass, fail, inconclusive, rate, wilson_low, wilson_high, pass_k?}`。

### 4.4 校准

```
annotation_tasks
  id, workspace_id, criteria_set_id, criterion_key, dataset_id, dataset_version
  purpose          golden_answer | judge_calibration | admission
  item_refs        [scenario_id ...]       （每条判据起步 15–20 条；硬下限 10 条）
  annotators       [用户 | review_link_id]  （≥2 人，独立标注，互相不可见、看不到裁判结果）
  adjudicator      用户
  status           open | labeling | adjudicating | closed
  created_by, created_at, closed_at

annotations
  id, task_id, item_ref, annotator, label（pass|fail|inconclusive；标准答案任务为自由文本）,
  rationale, created_at
  unique(task_id, item_ref, annotator)

calibration_records
  id, workspace_id, criterion_key, criteria_set_version, evaluator_id, evaluator_updated_at
  task_id, run_id                         # 裁判在同一批样本上的运行
  n, human_human_kappa, judge_human_kappa, kappa_ci_low, kappa_ci_high
  confusion          {"pass/pass":n, "pass/fail":n, ...}
  position_flip_rate（仅成对比较型裁判）, self_consistency（重复判定一致率）
  verdict            aligned | not_aligned | insufficient_n
  decided_by, decided_at, note
```

一条判据对某个评估器算作**已校准**，需要同时满足：最近一条记录 `verdict = aligned`；`judge_human_kappa ≥ max(0.61, human_human_kappa − 0.05)`；记录未超过重校准周期；评估器的 `updatedAt` 此后未变化。重校准周期（默认 90 天）与 κ 下限（默认 0.61）是**工作区发布策略的配置项**（`release_policy.calibration = {period_days, kappa_floor}`），与 `services/release_gates.py` 现有策略字段一样做归一化。任一条件不满足，判据有效档位降为观测，并在收件箱生成「需重新校准」事项。

### 4.5 发布记录、例外与血缘

```
release_records
  id, workspace_id, agent_id, promotion_id（原地发布时为空）
  candidate_version, candidate_endpoint, previous_live_version
  criteria_set_version, golden_versions {dev, regression, holdout}, evaluator_set_hash
  run_ids          [...]                   # 门控读取的运行
  gate_report      JSON（§5.4，决定时冻结）
  decision         released | blocked | invalid | rolled_back
  decided_by, decided_at, note
  waiver_ids       [...]
  rollback_target  {version, verified_drill_at}
  attachments      [{kind: load_test | doc | link, ref, note}]   # 外部证据，如压测报告

waivers
  id, workspace_id, agent_id, criterion_key, criteria_set_version
  actual, threshold, reason, risk_owner, compensating_control
  expires_on       （必填；策略上限 30 天）
  status           requested | approved | rejected | expired | revoked
  requested_by, approved_by（≠ requested_by）, approved_at
```

红线判据永远不可申请例外（API 直接拒绝）。例外到期后不再生效，下一次门控判定即阻断。

血缘是元组 `(criteria_set_version, 黄金集各子集版本, evaluator_set_hash, agent_version, endpoint)`，盖在每一次运行、每一份门控报告和每一条发布记录上。评分卡（§7.9）与运行对比（§7.4）在两次运行的血缘除了被比较的那一项外还有其他差异时拒绝比较，并说明原因。

### 4.6 入集队列与观测配置

```
admission_candidates
  id, workspace_id, agent_id, source（feedback | issue | insight_failure | insight_intent | review | manual）
  source_ref, session_id, cluster_id, affected_sessions, fault_category
  proposed_criteria  [criterion_key]
  existing_judgement {criterion_key: pass|fail|inconclusive}   # 现有评估器怎么判
  duplicate_of     候选或数据集样本引用
  redaction        {status: clean|redacted|blocked, entities: [...]}
  status           new | annotating | admitted | rejected | duplicate
  decided_by, decided_at, note, admitted_item_ref, admitted_version

watch_configs
  id, workspace_id, agent_id, criteria_set_id
  schedule         类 cron {every: daily|weekly, at, tz}
  split            regression | holdout
  repeats          k（带成本估算，§5.3）
  online_config_ids [...]
  alert_rules      [ids]                   # 计数 / 分数 / 分布三类规则，§5.6
  enabled, last_run_id, last_checked_at, next_due_at
```

## 5. 引擎

### 5.1 判据判定

运行完成时，`criteria_engine.snapshot(run)` 把结果流完整读一遍（用游标翻页，越过目前 5000 条的上限），写入 `criterion_results`。对运行所用判据集版本中的每条判据：

- **评估器执行器**：每个（场景、第几次尝试、`level` 上的评估单元）一行。判定规则：
  - 分类型评估器：`label` 经 `label_map` 映射；映射不到的 label 记为 `inconclusive`；
  - 数值型评估器：套用 `score_rule`（默认：按极性归一后的值 ≥ 0.5 记为通过）；
  - 裁判报错、找不到目标 span，记为 `error`（永不记为通过）；CustomCode 契约本来就返回 `errorCode`；
  - 代码评估器可以返回 `INCONCLUSIVE` label（平台的 Lambda handler 契约和裁判评分模板都加上这个分类选项）。
- **指标执行器**：从本次运行自己的 span 计算（`observability` 逐 trace 汇总，按本次运行的会话 id 过滤）：P95 延迟、首 token P95、每会话 token、**每次成功完成的成本**（全部会话成本 ÷ 目标判据通过的会话数；失败会话标 `NOT_SUCCEEDED`）。判据要求时把裁判成本也计入。
- **人工执行器**：判定来自本次运行会话上已关闭的标注任务。

逐判据汇总：`n = pass + fail`（inconclusive 与 error 不进 n，但单独报出）；`rate = pass / n`；Wilson 95% 区间；红线看 `violations = fail`（不用 rate）。`inconclusive / (n + inconclusive)` 为**未判定率**：<2% 正常，2–5% 建议补跑，>5% 本次运行不能用于门控（INVALID）。

### 5.2 分母核对

`denominator.expected_items` = 钉住的子集版本条数 × k。引擎分别统计 `invoked`、`evaluated`、`guardrail_blocked`（会话以 `guardrail.blocked` 结束）、`errored`、`inconclusive`。被护栏拦截的样本**只有在判据明确这样规定时**才算红线通过；否则算它所守护的那条判据失败，绝不被悄悄剔除（即「48 题剩 34 题报 100%」的陷阱）。任一红线或门禁判据出现 `evaluated < expected_items`，门控结论为 **INVALID**，而不是 BLOCKED。

### 5.3 pass^k（可选）

任务向导上的运行选项 `repeats: k`（1–10，默认 1），判据可单独覆盖 `pass_k {k, mode}`。

- 执行：`execute_run` 把每个场景在新会话里回放 k 次；会话元数据带 `testScenarioId = scenario_id` 与 `metadata.attempt = i`，一次批量评估就能评完全部尝试（AWS 没有重复参数，但允许 `testScenarioId` 重复）。
- 汇总：每个场景 `pass^k = k 次全部通过`（模式 `all`，无人值守 / 不可逆动作的默认值）或多数通过；判据的 pass^k = 通过场景占比；同时报 `mean@k` 与**一致性差距** `mean@k − pass^k`。
- 门控只对声明了 pass^k 的判据使用 pass^k，其他判据用第 1 次尝试，因此打开 k 不会悄悄改变门控结论。

**成本提示。** 提交前向导调用 `POST /api/eval/runs/estimate`，展示：

```
条数 × k × (agent 单会话成本 + Σ 裁判评估器 × 单条裁判成本)
```

- `agent 单会话成本`：该 agent 近 7 天每会话 token 中位数（来自 span token 用量）× `model_prices`；没有历史数据时退回「模型单价 × 数据集平均轮数 × 默认 token 预算」，并标注「粗估」；
- `单条裁判成本`：该评估器历史运行中 `tokenUsage` 的中位数 × 裁判模型单价；内置 / 第三方裁判跑在服务侧容量上，标注「由 AgentCore Evaluations 计费」，已知单价时一并显示；
- 按评估队列并发数估算墙钟耗时；
- `k > 1` 且估算超过工作区的 `eval_cost_confirm_usd`（默认 5 美元）时需要二次确认；超过 `eval_cost_max_usd` 直接拒绝，除非管理员放行。

运行结束后用同样的数据源填 `cost_actual`，与估算并排展示；估算与实际长期偏差大时，据此调整兜底常数。

### 5.4 发布门控（四道关，顺序固定）

`gate_engine.decide(agent, candidate, criteria_version, runs, waivers) → GateReport`：

1. **红线**：每条 `redline` 判据在回归集 + 留出集上 `violations == 0`。任何违例 ⇒ `BLOCKED`（不可例外）。
2. **分母**：对每条红线与门禁判据执行 §5.2；未判定率 ≤ 5%，否则 ⇒ `INVALID`（「这批结果不支持任何结论，包括通过」）。
3. **逐维门禁**：每条有效档位为门禁（裁判型须已校准）的 `gate` 判据：`rate ≥ threshold`（声明了 pass^k 的用 pass^k ≥ threshold）；阈值按整数百分点比较，当阈值落在 Wilson 区间内时标注「当前样本量下无法区分」。不做加权总分。未通过但有已批准且未过期例外的判据记为 `WAIVED` 通过。
4. **观测**：记录，并与上一次发布对比趋势；永不阻断。

另有**口径留痕**检查，保证报告可复现：评估的是 *candidate* 版本、经由 candidate 端点、用的是钉住的子集版本和当前评估器集；留出集运行存在，且晚于黄金集最近一次变更；规格里每个模型的生命周期都不是 `EOL`，Legacy 模型须有不超过 15 天的回滚演练记录。

结论：`PASS | BLOCKED | INVALID`，逐判据一行 `{key, dimension, tier, effective_tier, n, rate, ci, threshold, verdict, waiver?}`，外加三类回归清单（§5.7）。报告在决定时冻结进发布记录。

`release_gates.eval_gate`（平均分）保留给**没有**已发布判据集的 agent，界面上标注为「旧版闸门」；有判据集的 agent 只用新引擎。

### 5.5 校准的计算

按判据计算 Cohen's κ（二分类或多分类；有序量表用加权 κ；≥3 名标注人用 Fleiss；有缺标时用 Krippendorff's α）。用 bootstrap 求 95% 置信区间。不用准确率，不用 Pearson。工作台（§7.3）还根据两份独立标注计算人与人之间的 κ，作为裁判必须达到的上限。

### 5.6 统计工具

一个纯函数模块（`evaluation/stats.py`，完整单测）：Wilson 区间；两比例样本量（在 A/B 和 canary 上显示：「基线 90% 时检出 5pp 每组约需 432 个会话」）；复合通过率 `Π rate_i`（在判据编辑器上显示：「8 条各 95% ⇒ 只有 66% 的会话全部通过」）；基于重复运行的 pass^k；各种 κ；意图分布的总变差距离；滚动中位数基线。

### 5.7 运行对比

`compare(run_a, run_b)`：

- 两次运行必须共享判据集版本与子集版本，否则拒绝并说明哪一项不同；
- 逐判据：Δrate 及其显著性（两比例检验），以及逐条样本的变化清单：**已修复**（fail→pass）、**新引入**（pass→fail）、**仍失败**；
- 判据或黄金集升版时的「两个数」模式：在重叠样本上分别用旧版本和新版本给新运行打分，分开回答「agent 有没有变好」和「标准硬了多少」。

## 6. 发布路径：先过门控，再接流量

### 6.1 端点模型

每个基于 runtime 的 agent 和每个 harness agent 都有一个具名端点 **`live`**。所有生产调用路径（`services/invoke.py` 的同步与流式、JWT bearer、A2A、`/v1`、渠道、分享 / 评审链接、smoke）都传 `qualifier = live`。`DEFAULT` 仍会自动跟随最新版本，但不再承接生产流量，成为「最新构建」的指针。

一次更新（重新部署、回滚、实验 promote、发布申请）因此变为：

1. 生成新版本（`UpdateAgentRuntime` / `UpdateHarness`）：DEFAULT 跟着变，`live` 不变；
2. 创建或改指端点 **`candidate`** 到该版本；
3. 门控所需的评估对 `qualifier = candidate` 执行（`execute_run` 增加 `endpoint_qualifier`；在线评估 / Insights 的数据源按 `<runtime>.candidate` 的 service name 过滤）；
4. 结论为 `PASS`：`UpdateAgentRuntimeEndpoint(live → 新版本)` / `ensure_harness_endpoint(live)`；结论为 `BLOCKED/INVALID`：`live` 不动，新版本保留供排查；
5. 回滚 = 把 `live` 改指回 `previous_live_version`（不需要重新构建）。

canary 保留自己的 stable / treatment 端点；完成时改指 `live`（目前依赖 DEFAULT 已经切换的副作用，见 F5 与 §3 差距 4）。

### 6.2 发布模式与迁移

工作区设置 `release_mode`：`direct`（当前行为）| `gated`。

- `direct`：除了调用改走 `live` 之外不变，每次部署成功后 `live` 与 DEFAULT 保持同步。这样调用路径的改动可以先单独、安全地上线。
- `gated`：按 §6.1 的流程；`prod` 级工作区中有已发布判据集的 agent 必须用，其他情况可选。

默认值分两步：**（1）** P3 上线时，所有工作区（包括新建的 `prod` 工作区）默认 `direct`，按工作区手动开启 `gated`；**（2）** P3 在实际环境跑稳后（退出标准：≥2 个工作区使用 `gated` 满 4 周、没有门控引擎缺陷阻塞发布、改指回滚演练过），新建的 `prod` 级工作区默认 `gated`。第 2 步是有跟踪的后续任务，不是「永远手动」；已有工作区永不被自动切换。

迁移任务（幂等、可恢复，和其他 job 一样）：为每个活跃 agent 创建指向其当前 `version` 的 `live` 端点，等待 READY，然后把 agent 的 `endpoint_mode` 列从 `default` 改为 `live`。迁移前创建的 agent 在切换之前继续走 DEFAULT。导入的已有 agent（discovered）永不修改：保持 DEFAULT，并标记为「不可门控」。

### 6.3 现有发布流程的变化

| 流程 | 现在 | gated 模式 |
|---|---|---|
| 重新部署 / 快照回滚 | DEFAULT 切换，无门控 | candidate 端点 → 门控 → 改指 live |
| 实验 promote | DEFAULT 切换；只要求存在 verdict | 要求 treatment 胜出（F6）并通过门控 |
| 发布执行（非 canary） | smoke 前 DEFAULT 已切换；失败仍在线（F4） | candidate → smoke → 门控 → live |
| 发布执行（canary） | 候选在 stable 后面；DEFAULT 已切换 | 放量流程不变；完成时改指 live |
| 发布记录 | 发布申请行 + 审计 | 每次决定都写一条 `release_records` |

发布申请的门控在**目标**工作区执行，使用目标工作区的判据集（目标没有判据集时，用发布包钉住的版本，并加标注）。

## 7. 人工判断工作台

每个工作台围绕一个决定设计：谁来做、界面上需要哪些证据、能做哪些操作、平台记录什么。全部是使用 `?view=` 模式的 V2 页面；图表和现有看板一样用手写 SVG（§9.2）。

### 7.1 判据编辑器与阈值助手：业务方定义并签字

路由 `/v2/eval/criteria?agent=…`（`?view=set&id=…&v=…`）。

展示内容：
- 判据表（key、原文、维度、档位、有效档位、执行器、层级、阈值、分母、负责人），按固定的维度顺序分组，校验结果就地显示；
- 每条判据的**阈值助手**：V0 基线通过率及当前黄金集规模下的 Wilson 区间、人工基线（填写）、业务可接受下限（填写），给出「基线往上取一档」的阈值建议；阈值落在置信区间内时给出警告；
- **复合通过率面板**：全部门禁阈值的乘积（「8 条门禁各 95% ⇒ 只有 66% 的会话全部过线」），以及要达到某个整体通过率时每条判据需要的阈值；
- **阶梯提示**：对每条裁判型判据给出「能不能改成代码断言」的检查清单（结构 / 格式 / 数值 / 延迟 / 成本 → CustomCode；顺序 → TrajectoryInOrderMatch）；
- 覆盖度：每条判据在各档样本（已知好 / 已知坏 / 模糊 / 对抗）上的数量，少于 3 条标红。

操作：从模板创建（选场景 → 模板版本）或从空白创建；在模板基础上编辑覆盖项 / 新增项（继承的行有标记）；采用新的模板版本（看差异 + 重新签字）；把一个判据集另存为新模板；编辑草稿；从架构助手的评估计划导入（`blocking → 门禁`、`threshold → 阈值`、黄金用例 → 样本、鱼骨图维度 → 维度）；发布；**签字**（需要 `criteria.sign`；签字人不能是最后编辑人，管理员除外；记录姓名、时间、备注）；对比两个版本。

### 7.2 黄金集管理：工程与业务共建样本

路由 `/v2/eval/data?view=golden&id=…`。

展示：各子集（开发 / 回归 / 留出）的条数与版本；**判据 × 样本覆盖矩阵**（格子表示样本检验了哪些判据，按样本档位着色）；来源构成（生产日志 / 工单 / 专家手写 / 合成 / 修复回灌）及每类来源的偏差说明；高亮 `expected_source = agent_observed` 的样本；已退役样本；每个版本的变更记录（「+12 条来自问题箱，−3 条退役，留出集未动」）。

操作：新增 / 编辑样本（含 `metadata.dlc`）；在开发集与回归集之间移动（除建立时外不进留出集）；退役（移入回归池，不再计入门禁分母）；发布子集版本（AWS `CreateDatasetVersion`）；对缺少人工标准答案的样本发起标注任务。

### 7.3 标注与校准工作台：先两人独立标，再比裁判与人

路由 `/v2/eval/calibration?criterion=…`（控制台）与 `/r/annotate/<token>`（免登录，复用分享链接原语 `services/share_links.py:92`，`kind = annotate`）。免登录链接只在 `dev` 与 `staging` 级工作区可用；`prod` 级工作区的标注必须使用控制台账号，创建链接的 API 拒绝 `kind = annotate`，工作区被改为 `prod` 级后已有的标注链接立即失效。

标注人视图：一次一条样本，展示对话、trace 摘要（工具调用、检索到的上下文）、判据原文及其评分口径、正反例；标注通过 / 不通过 / 无法判定 + 理由。任务关闭前，标注人看不到彼此的标注，也看不到裁判结果；样本随机顺序呈现，不按类别分组（避免锚定）。

校准视图（逐判据）：
- 人与人 κ 和裁判与人 κ 并排显示，带 bootstrap 区间与 Landis–Koch 分档（「≥0.80 可自动门控 · 0.61–0.79 可门控但硬失败需人工复核 · ≤0.60 只能观测」）；
- 混淆矩阵（裁判 × 裁决后的人工结论）；
- 分歧清单，并列展示双方理由与裁判 explanation，这是主要的修复工具；
- 交换顺序后的翻转率（成对比较型裁判）与重复判定的自洽率；
- 历史：每次评分口径修订后的 κ，让「分数涨了但 κ 没动」一目了然；
- 结论按钮：**已对齐 → 可做门禁** / **未对齐 → 只能观测**（需要 `judge.calibrate`）；校准记录随判据一起版本化。

κ 偏低时的引导：先拆判据、往评分口径里补 2–3 个正反例、重标一轮，然后才考虑换裁判模型。

### 7.4 运行对比与归因：工程师决定改哪里

路由 `/v2/eval/tasks?view=compare&runs=a,b,c…`。

展示：
- **修复阶梯**：同一子集版本上的一串运行，每次一行，带变更说明（来自快照 diff）、整体门控状态、红线状态与各维通过率，即「47.6 → 52.4 → 71.4 → 76.2 → 100」那张图；
- 逐判据：Δ 及其显著性，以及三类样本清单（已修复 / 新引入 / 仍失败）；
- 版本不同时的「两个数」（§5.7）；
- 下钻：会话 → 轨迹（span 树）→ 工具调用（启用了 opt-in 的 OTel 字段时显示参数与返回；未启用时用横幅说明）；
- **按工具统计**：调用次数、误选（轨迹不匹配）、漏调（期望调用但没调）、错误率；
- 每个失败簇的**七层归因提示**（01 结构化上下文 · 02 决策规则与阈值 · 03 工具与 Skill · 04 编排与子 agent · 05 运行时与中间件 · 06 模型与参数 · 07 权限与护栏），依据失败判据的 `attribution_layer`、轨迹证据和 pass^k 差距（`mean@k − pass^k` 大 ⇒ 怀疑 05/06）；被对比的两个快照改动跨了不止一层时，按「一次只改一层」给出警告。

### 7.5 门控报告与签字：签字人放行或阻断

显示在发布申请详情页和 agent 的发布卡片上（`?view=release&record=…`）。

按门控顺序展示：红线（违例数、样本会话）；分母面板（应评 / 已调用 / 已评估 / 被拦截 / 出错 / 无法判定，未判定率）；逐维门禁（通过率、带阈值标记的置信区间条、有效档位、例外标记）；观测项及与上次发布的趋势；口径留痕（五版本血缘、模型生命周期、回滚目标与演练日期）；自上次发布以来新引入的失败；附带的外部证据（如压测报告）。

操作：**放行**（仅当 PASS；需要 `promotion.approve` 或 `release.sign`；签字人不能是申请人；记录决定）；为未过线的门禁判据申请例外（见 §7.5.1）；**阻断**并填写说明；重跑门控评估。

#### 7.5.1 例外审批：风险负责人

收件箱事项与弹窗：判据、实际值对比阈值及置信区间、理由、补偿措施、到期日（≤30 天）、该判据已被例外过几次、未关闭例外数及最老一条的时长。批准需要 `waiver.approve`，且批准人不能是申请人。红线显示「不可例外」。

### 7.6 入集审核：业务方决定什么成为标准

路由 `/v2/issues?view=admission`（与现有问题箱并列）。

候选来源（合并并去重）：点踩与 SME 评审结论（`chat_feedback`）、问题箱、Insights 的 FailureAnalysis 根因与 UserIntent 聚类（带 `affectedSessionCount`）、在线评估失败。

每个候选展示：带 **PII 脱敏预览**的对话（`guardrail.screen(mode="anonymize")` 识别出的实体高亮；无法脱敏的候选禁止入集）；所属聚类与受影响会话；**现有评估器对它怎么判**（判错的优先，它既是新用例也是校准样本）；最相近的已有黄金样本，用于标记重复：对首条用户输入做精确匹配与归一化文本匹配（统一大小写、空白、标点、全半角），不调用 embedding；审核人看到最相近的几条后自行判断；建议关联的判据；来源的偏差标签。

操作：入集到开发集或回归集（永不进留出集），标准答案必须由人撰写或确认（记录 `expected_source`）；拒绝并写理由；标记为重复（计入所属聚类的数量）；发起标注任务；发起判据变更（「这是新边界，需要加判据」）。入集需要 `golden.admit`；入集样本进入子集的草稿版本；发布版本时显示最近一次运行在新旧两个版本上的通过率。

队列视图按影响面排序聚类（受影响会话数 ÷ 工程师填写的修复代价，如有），并单独显示被判为噪声的数量。

### 7.7 漂移盯盘：值班判断是 agent 退化还是裁判漂移

路由 `/v2/eval/watch?agent=…`。

展示：
- 各维度在线判据得分的时间序列（在线评估逐评估器序列映射到判据），叠加滚动中位数基线带、定时重跑点（观测配置在回归集 / 留出集上的运行）与发布标记；
- 三类告警分开展示：**计数类**（红线违例、错误；绝对阈值，立即呼叫）、**分数类**（相对滚动中位数；基线不足 8 天时静默；每日摘要）、**分布类**（用 TVD 看意图构成，并指出哪一类在变：「退货类 8% → 21%」）；
- 「无告警」心跳行，区分「连续正常」与「监控挂了」；
- **裁判漂移检查**：在冻结校准集上重跑；如果裁判在人已标注的样本上改判了，是裁判漂移，不是 agent 退化；
- 均值旁边显示低分占比（尾部问题会被平均值掩盖）；
- 当前采样设置（trace 采样、Transaction Search 索引比例、在线评估采样率）以及日志留存期与对比窗口的关系。

操作：确认告警、从聚类发起入集候选、一键回滚 `live`（有审计）、暂停出故障的在线评估器。值班人员不能在这个页面修改判据、阈值或采样率（手册 §6 的值班权限边界）。

### 7.8 人机分工点：业务方在可靠性与成本之间取舍

判据编辑器上的面板：对有置信度或转人工信号的 agent，用最近一次运行给出一组运行档位，包括自主准确率、转人工比例、可达到的可靠性（基于记录的人工复核成功率）、每次成功完成的成本，由负责人选定一行。选定后生成三条判据：「低置信必须转人工」（红线）、「转人工比例 ≤ 选定值」（门禁）、「转人工准确率」（观测）。

### 7.9 Agent 评分卡：一眼看全

Agent 详情页 `?tab=dlc`：按固定顺序排列的五个维度卡片，每张显示当前值对比标准、生效门禁数、红线状态、最近一次门控结论、趋势小图；覆盖度 × 通过率（不只看通过率）；未关闭的例外；校准欠账（到期需重校准的裁判）；线上版本的血缘；跳转到上面每一个工作台。

## 8. 角色、权限与审计

新增权限键（加入 `AGENT_PERMISSIONS`、`DEFAULT_BY_ROLE`、`route_policy.py`、`frontend/src/lib/api.ts`）：

| 键 | 默认持有者 | 管控 |
|---|---|---|
| `criteria.manage` | member、operator | 编辑判据草稿、管理黄金集 |
| `criteria.sign` | 默认无人（授予具名的业务负责人）；admin | 发布 / 签字判据集 |
| `golden.admit` | 默认无人；admin | 入集、发布黄金子集版本 |
| `judge.calibrate` | 默认无人；admin | 记录校准结论 |
| `waiver.approve` | operator；admin | 审批例外 |
| `release.sign` | operator；admin | 签字 gated 模式的原地发布（发布申请仍用 `promotion.approve`） |

免登录标注人使用 `annotate` 分享链接（限定一个任务、会过期、可撤销），仅限 `dev`/`staging` 级工作区；`prod` 级必须使用控制台账号（§7.3）。服务端强制职责分离：签字人 ≠ 判据集最后编辑人；发布签字人 ≠ 申请人；例外批准人 ≠ 申请人（管理员也不例外，与 `review_promotion` 一致）。

所有决定在同一事务内写 `audit_events`。新增查询接口 `GET /api/audit?target=…&action=…`（admin，以及对自己 agent 持有 `criteria.sign` 的人），为每个工作台提供「决定历史」面板。

prod 级工作区保护（`PROD_PROTECTED`）覆盖：判据集发布 / 签字、黄金集版本发布、例外批准、发布签字、`live` 改指 / 回滚、观测配置变更。

## 9. API 与前端

### 9.1 API（均在 `/api` 下，在 `route_policy.py` 中分类）

```
criteria-sets           GET (?kind=template|agent), POST
criteria-sets/{id}/adopt-template  POST  （agent 判据集 → 基于模板 vT 的新草稿）
criteria-sets/{id}      GET (?version=), PUT（仅草稿）, DELETE（仅草稿）
criteria-sets/{id}/publish | /sign | /diff?a=&b= | /import-plan
criteria/{key}/coverage
eval/runs/estimate      POST   （成本与耗时估算，§5.3）
eval/runs               POST   + criteria_set_version, split, repeats, endpoint_qualifier
eval/runs/{id}/criteria GET    （判据结果汇总 + 明细）
eval/runs/compare       GET    ?runs=a,b
eval/golden/{id}        GET, PUT 子集；/publish?split=
annotation-tasks        GET, POST；/{id} GET；/{id}/labels POST；/{id}/close POST
calibration/{criterion} GET 历史；POST 结论
agents/{id}/gate        POST 评估候选版本；GET 最近一份报告
agents/{id}/release     POST sign | block；POST rollback
release-records         GET (?agent=)，/{id} GET
waivers                 GET, POST；/{id}/approve | /reject | /revoke
admission               GET (?status, ?agent)；/{id}/admit | /reject | /duplicate
watch                   GET, POST, PUT；/{id}/run-now
audit                   GET
share/annotate/{token}  GET, POST  （公开，与评审链接一样限流）
```

### 9.2 前端

- 导航（评估分组）：新增 **判据**、**校准**、**盯盘**；入集审核放在问题箱下；运行对比放在评估任务下；门控报告放在发布申请与 agent 详情中；评分卡作为 agent 详情的一个页签。
- 新增 V2 共享组件：`Sparkline`、`BandChart`（序列 + 基线带 + 标记）、`CiBar`（通过率、区间、阈值标记）、`ConfusionMatrix`、`CoverageMatrix`、`LadderChart`、`TierChip`、`DimensionTiles`。与 `observability/Dashboard.tsx` 一样手写 SVG，不引入新的图表依赖。
- `Table` 增加行选择 API（目前页面手写复选框，见 `data/Traces.tsx:78`）。
- 任务向导：数据集**版本**选择（V2 目前没有）、子集选择、判据集选择、重复次数并实时显示成本估算。
- 所有文案 en + zh-CN；方法论术语沿用手册的中文说法（判据、红线、门禁、观测、黄金集、认知 / 质量 / 责任 / 成本 / 性能）。

## 10. 后台调度

在 `main.py` 的 lifespan 中启动一个进程内调度线程（与 `model_prices.start_auto_refresh` 同样的模式），每 60 秒检查一次：

- 到期的 `watch_configs` → 提交评估运行（遵守评估队列与成本上限；估算超出上限的运行跳过并改为告警）；
- 告警规则判定（补上「只有有人请求时才检查」的缺口），只在状态变化时通知；
- 校准过期与例外过期扫描 → 收件箱事项；
- 对开启聚类的 agent 运行 Insights 批量任务。

单写者保障：每个任务用条件更新认领一行 ledger 记录（`scheduler_claims(task, due_at, claimed_by, claimed_at)`），第二个后端进程不会重复执行；超过 10 分钟的认领可以被接管。重启后和现有 job 一样可恢复。

## 11. AWS 约束及应对

| 约束（已在安装的服务模型中核实：botocore 1.43.103 / bedrock-agentcore 1.17.0） | 应对 |
|---|---|
| AgentCore 里没有任何门控、阈值或通过标准对象 | 门控引擎在平台侧实现（§5.4）；证据冻结进发布记录 |
| 没有逐场景重复参数 | 平台为每个场景回放 k 个会话，`testScenarioId` 可重复（§5.3） |
| `GetBatchEvaluation` 只返回汇总；逐会话结果只在 CloudWatch 输出日志组里 | 运行完成时快照进 `criterion_results`；强制日志留存期 ≥ 对比窗口 |
| 分类评分的 label 是自由文本，没有 INCONCLUSIVE | 平台在裁判模板与代码评估器契约中加入 `INCONCLUSIVE`；每条判据配 `label_map` |
| 带标准答案的评估器不能用于在线评估 | 派生 `criteria.online`；盯盘把只能离线的判据显示为「仅定时重跑」 |
| 在线评估：模型允许每个配置 ≤ 25 个评估器（平台保持 10 个上限），`samplingPercentage` 0.01–100 是百分比 | 配置构建器把红线（100% 或交给护栏）与门禁（10–20%）拆成两个配置；界面明确显示「%」 |
| 被 ENABLED 在线配置引用的自定义评估器会被锁定 | 校准修改时克隆新评估器 + 判据新版本；观测配置原子切换 |
| 数据集、Insights、Recommendations、A/B 都是预览；Optimization 调用不进 CloudTrail | 封装留在 `agentcore_eval.py`；发布记录注明「优化类证据不在 CloudTrail」 |
| A/B：最多 2 个变体，只能经 gateway，结果带 pValue 与置信区间 | 保持不变；增加样本量提示；promote 要求 treatment 胜出（F6） |
| runtime 与 harness 都有端点；调用支持 `qualifier` | `live` / `candidate` 端点（§6） |
| OTel GenAI 的 `tool.call.arguments/result` 默认不上报；`conversation.id` 为条件必需 | 构建环的**埋点字段对齐检查**按 agent 报告缺失字段（§12 P1）；模板默认打开 |

## 12. 分期

每一期都按 `docs/roadmap.md` 的完成标准执行（hermetic 测试、i18n 对齐、architecture 章节、`make verify`、在临时栈上跑两遍 e2e、推送前安全检查）。

| 期 | 范围 | 解锁 | 已交付 |
|---|---|---|---|
| **P0 基础** | F1–F6 修复；`evaluation/stats.py`；`criterion_results` 落库；运行血缘列；V2 数据集版本选择 | 数字可信 | ✅ |
| **P1 定义 + 门控** | `criteria_sets/criteria` 与编辑器（§7.1，含从助手计划导入）；判据引擎（§5.1–5.2）；门控报告（§5.4、§7.5），先作为现有平均分闸门之外的第二道门；INCONCLUSIVE label；埋点字段对齐检查 | workshop 核心：定义 → 评估 →「红线拦截」演示 | ✅ |
| **P2 校准 + 黄金集生命周期** | 标注任务、标注链接、κ 引擎、校准工作台（§7.3）；黄金集子集与覆盖矩阵（§7.2）；有效档位降级 | 「未校准的裁判不能做门禁」 | ✅ |
| **P3 门控发布** | `live`/`candidate` 端点、迁移任务、`release_mode`、发布记录、例外、改指回滚（§6、§4.5）；运行对比与修复阶梯（§7.4） | 先过门控再接流量；F1→F5 阶梯演示 | ✅ |
| **P4 pass^k + 成本** | `execute_run` 重复运行、估算接口、向导提示、实际成本核算（§5.3） | 带成本意识的可靠性门控 | ✅ |
| **P5 观测 + 回流** | 调度器（§10）、观测配置与漂移盯盘（§7.7）、入集审核（§7.6）、两个数报告、评分卡（§7.9）、人机分工面板（§7.8）、审计查询接口 | 飞轮无人值守也能转 | ✅ |

Workshop 就绪度：P0 + P1 支撑 1 天判据工作坊（定义、构建、评估、红线拦截、修复、重跑）；P2 + P3 让发布门控的故事在平台本身上成立。

**已于 2026-10-07 交付。** P0–P5 全部实现：控制台在 `/v2/eval/standards`（九个 `?view=` 子页），
`app/routers/dlc.py` 提供 56 个路由，另有公开的 `/share/annotate/*` 两个；整个循环已由
`backend/scripts/e2e_agent_dlc.py`（38 项检查）与 `e2e_agent_dlc_browser.py`（九个子页 × 两种语言）
在真实 AWS 上验证。真实运行纠正了设计里的三处假设，均已写入 `docs/architecture.zh-CN.md`：
遥测**按端点区分**（放行运行必须读 `…-candidate`，门控智能体的看板必须读 `…-live`）、
端点删除是**异步的**（删除智能体必须等它完成）、成本预估在两个入口都是**强制**的，而不只是展示。

## 13. 测试

- 每个引擎（统计、判据判定映射、分母、门控顺序、例外、κ）都有 hermetic 单测，用表驱动用例复现手册里的实例（48/34 分母陷阱、0.95⁸ 复合通过率、κ = 0 的橡皮章裁判、黄金集 18 → 30 条后通过率下跌）。
- 快照读取器针对录制的 CloudWatch 结果记录做契约测试。
- 路由策略漂移测试扩展到新命名空间与 `PROD_PROTECTED` 条目。
- E2E `backend/scripts/e2e_agent_dlc.py`：创建判据集（含一条基于 CustomCode 评估器的红线）→ 黄金集 → 跑 V1（红线失败）→ 门控 BLOCKED → 修提示词 → 跑 V2 → 门控 PASS → 改指 `live` → 改指回滚；k=3 的 pass^k 运行并核对估算与实际；用脚本化标注人完成一次校准；清理所有 `rm-e2e-*` 资源。

## 14. 不在范围内

- 生成压测 / 压力 / 并发流量（性能判据读 trace；外部压测报告可以附到发布记录上）。
- 自动修改判据、阈值或标准答案。
- 第 06 层（模型与参数）和第 07 层（权限与护栏）的自进化。
- 替换客户已有的追踪栈（Langfuse、Phoenix 等）：读取兼容 OTel 的数据，不做迁移。

## 15. 已定决策（2026-10-06）

1. **判据集做成模板。** 场景模板单独管理版本；agent 判据集继承一个模板版本，加上覆盖项与新增项（§4.1、§7.1）。
2. **免登录标注链接只用于 `dev`/`staging`。** `prod` 级工作区只允许有控制台账号的人标注，每条标注都能追溯到人（§7.3、§8）。
3. **重校准周期与 κ 下限按工作区发布策略配置**，默认 90 天与 0.61（§4.4）。
4. **入集去重先做精确 + 归一化文本匹配**，暂不调用 embedding（§7.6）。
5. **`release_mode` 默认值分两步：** P3 上线时手动开启；P3 达到实际环境的退出标准后，新建 `prod` 级工作区默认 `gated`（§6.2）。
