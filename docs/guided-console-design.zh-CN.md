# 引导型控制台 — 设计文档

> 来源：客户反馈（2026-10-14）。控制台出错时只告诉用户“错了”，不告诉下一步去哪；第一次用的人认得“判据”这两个字，却不知道它在哪些步骤里用、由谁来做。

## 1. 目标与原则

目标：用户在任何一页都能回答三个问题——**这是什么**、**我在流程的哪一步**、**下一步去哪**。

1. **报错必须带出路。** 每条用户可修复的错误都说明原因，并给出一个能直接点的下一步；用户修不了的（需要别的角色、需要管理员），说清楚找谁。
2. **灰掉的按钮必须说明原因。** 不能只禁用，要告诉用户缺什么、去哪补。
3. **术语讲用途，不只讲定义。** 解释里写清楚它在哪几步出现、谁负责，每一步都能跳过去。
4. **依据事实，不依据点击。** 前置条件、已完成步骤都从实际存在的数据算出来（沿用 `LaunchSequence` 的做法），不靠用户点过“我知道了”。
5. **路由只在前端维护。** 后端只说“下一步是什么动作、带哪些参数”，具体跳到哪个 URL 由前端的动作表决定，页面改版时不用改后端。
6. **防止再欠账。** 文案、动作、术语都有 CI 检查，新增的错误码或术语缺文案就失败。

## 2. 现状

| 方面 | 现状 | 问题 |
|---|---|---|
| 错误信封 | `{code, message, detail}`（`backend/app/core/errors.py`），前端按 `apiErrors.<code>` 翻译（`lib/api.ts` 的 `localizedMessage`） | 只有“哪里错了”，没有“下一步” |
| 错误文案 | 后端约 618 个错误码，`zh-CN` 有文案的约 185 个 | Agent-DLC 的 `release.*`、`golden.*`、`criteria.*`、`admission.*`、`waiver.*`、`calibration.*` 几乎全部没有中文，直接显示后端写给日志看的英文，如 `release.no_criteria` |
| 错误展示 | 前端约 677 处 `toast(...)` | 几秒就消失，不能放链接，离出错的位置也远 |
| 禁用按钮 | V3 约 248 处 `disabled={…}`，带 `title` 的约 29 处 | 用户看到按钮灰了却不知道为什么，如没有运行中的智能体时“新建判据表”不可用 |
| 术语 | `<Term>`（`v3/onboarding/Glossary.tsx`）悬停显示一句话，点开术语表；登记 19 个词（`v2/Glossary.tsx` 的 `GLOSSARY_TERMS`） | 没有 Agent-DLC 术语（`criteria` 有文案但未登记）；V3 只有 5 处用到；解释只有定义，没有用途和步骤 |
| 引导 | 首页 `LaunchSequence`：五步、每步带 `to` 链接，状态从数据算出 | 只覆盖开局，功能页内部没有“这页要先做什么” |

## 3. 报错时给出下一步

### 3.1 信封扩展

`envelope` 增加可选的 `next` 字段，旧客户端忽略即可，向后兼容：

```jsonc
{
  "code": "release.no_criteria",
  "message": "agent has no published criteria set",
  "detail": null,
  "next": [
    { "action": "criteria.publish", "params": { "agent_id": "ag-123" } },   // 第一个是主动作
    { "action": "doc.flywheel_evaluate" }                                    // 可选的次要动作
  ],
  "needs": "perm:golden.admit"   // 可选：只有某权限能修复时给出
}
```

- `AppError` 增加可选参数 `next: list[NextStep] | None` 和 `needs: str | None`，`app_error_handler` 原样输出。`NextStep` 是 `{action: str, params: dict[str, str]}`。
- `/v1` 公共 API 和 `/share` 页面**不输出** `next` 和 `needs`：它们跨越 API key 的信任边界，控制台路由对调用方没有意义（与 `client_error_handler` 隐去 AWS 原文是同一个理由）。
- 校验错误（422）不加 `next`，前端已经能把错误定位到字段上。

### 3.2 前端动作表

新文件 `frontend/src/v3/next.ts`，一张动作表：

```ts
export const NEXT_ACTIONS = {
  "criteria.publish":   { to: (p) => `/v3/standards?agent=${p.agent_id}&view=criteria`, label: "apiNext.criteria.publish" },
  "golden.open":        { to: (p) => `/v3/standards?agent=${p.agent_id}&view=golden&id=${p.dataset_id}`, label: "apiNext.golden.open" },
  "agent.create":       { to: () => "/v3/create", label: "apiNext.agent.create" },
  "workspace.fix_role": { to: (p) => `/v3/workspaces?id=${p.workspace_id}`, label: "apiNext.workspace.fix_role" },
  // …
} satisfies Record<string, NextAction>;
```

- `label` 是 `apiNext.<action>` 文案键，en + zh-CN 都要有，例如“去发布判据表”。
- 后端给了表里没有的动作时，前端忽略这一项，不报错（前后端可以分开发版）。
- 有些错误后端不用改：前端可以在动作表旁边维护 `DEFAULT_NEXT: Record<code, action>`，给不需要参数的错误码配一个默认下一步，比如 `aws.access_denied` → 打开工作区角色设置。后端给了 `next` 时以后端为准。

### 3.3 权限类错误

`auth.permission_required` 以及 `needs` 字段有值时，提示统一成三句话：缺什么权限、通常是哪个角色、去哪里授予：

> 需要 `golden.admit` 权限才能编制切分，通常由 `biz-owner` 持有。请让持有人操作，或请管理员在 **用户** 页授予。[去用户页 →]

只有管理员才显示跳转链接；非管理员只显示文字，免得点进去还是 403。

### 3.4 展示：`ErrorCallout` 取代错误 toast

新增 `ErrorCallout` 组件，基于现有的 `Notice`（`v3/ui.tsx`）：

```
⚠ 智能体还没有已发布的判据表，放行门无法评估。
  放行门要按已发布的判据逐条判定；部署那一刻会锁定当时已发布的判据表版本。
  [去发布判据表 →]   [这一步是什么？]
```

- 三行结构：**哪里错了**（`apiErrors.<code>`）→ **为什么**（可选的 `apiErrors.<code>_why`）→ **下一步**（`next` 按钮，以及可选的“这一步是什么？”，打开对应术语卡片，见 §5）。
- 位置：用户操作引起的错误，显示在触发它的按钮或表单旁边，直到用户再次操作或关闭才消失；后台轮询失败显示在对应面板顶部。
- `toast` 只保留给成功提示和与当前页面无关的后台事件；这类 toast 如果带 `next`，显示一个动作按钮，停留时间延长到用户关闭为止。
- 把 677 处 `toast(errorMessage(err))` 改成 `ErrorCallout` 是机械性改动，按页面分批做（§8）。新代码由 CI 检查约束（§7）。

### 3.5 第一批错误码

先覆盖工作坊主线（准备 → 定义 → 构建 → 评估 → 发布 → 观测 → 反馈）上最常遇到的错误码，每个都要补中英文案、原因说明和 `next`。

| 错误码 | 下一步 |
|---|---|
| `release.no_criteria` | `criteria.publish`：去发布判据表 |
| `release.no_golden` / `release.empty_golden` | `golden.open`：打开该判据表关联的黄金集，未编制时直接定位“初次编制切分” |
| `release.not_evaluated` / `release.gate_not_passed` | `gate.open`：打开放行门的候选版本卡片 |
| `release.self_sign` / `criteria.self_sign` / `waiver.self_approve` | 无链接；说明需要另一个人签署，并列出持有该权限的人 |
| `criteria.agent_required` | `agent.create`；有智能体但没在运行时，改为 `agent.open` |
| `criteria.draft_exists` | `criteria.open_draft`：打开已有草稿 |
| `criteria.sign_unpublished` | `criteria.publish` |
| `golden.duplicate_name`（见 `agent-dlc-design` §4.2.1） | `golden.open`：打开已有的同名黄金集 |
| `golden.holdout_sealed` / `golden.holdout_closed` | 说明封存的原因；`admission.open`：新样本走样本准入 |
| `golden.duplicate_scenario` / `golden.bad_case_tier` | 在编制弹窗里把错误定位到对应样本；`doc.golden_format` |
| `admission.no_golden_set` | `golden.create` |
| `waiver.redline` | 说明红线不能豁免；`criteria.open` 定位到这条判据 |
| `workspace.assume_role_failed` / `aws.access_denied` | `workspace.fix_role`：打开工作区角色设置，附上缺的 IAM 动作（`detail.operation`） |
| `workspace.not_ready` | `workspace.open`：查看初始化进度 |
| `auth.permission_required` | 按 §3.3 |

## 4. 禁用的按钮说明原因

- `Btn` 增加 `disabledReason?: { text: string; next?: NextStep }`。有值时：按钮保持可聚焦（`aria-disabled` 代替 `disabled`），悬停或聚焦时显示原因；有 `next` 时显示一个链接。
- 原因同样从数据算出，写成纯函数并做单测，例如：

  ```ts
  // standards/guards.ts
  export function canCreateCriteria(agents): Reason | null {
    if (!agents.some((a) => a.status === "active"))
      return { text: "v3.reason.needActiveAgent", next: { action: "agent.create" } };
    return null;
  }
  ```

- 只是请求进行中的禁用（提交时锁按钮）不需要原因，用 `busy` 状态区分，CI 检查时不计入。

## 5. 术语卡片

### 5.1 从一句话到一张卡片

```
判据
一条可以核对的质量标准，例如“不得承诺政策外的退款”。
用在：定义（起草） → 评估（发布、签署） → 放行门（逐条判定） → 观测（漂移监测）
谁负责：developer 起草，biz-owner 签署
相关：判据表 · 红线 / 门限 / 观察 · 黄金集
[去判据页 →]
```

数据结构从 `glossary.<term>` 一个字符串，改成：

```ts
interface GlossaryEntry {
  short: string;                     // 悬停时显示的一句话（现有文案沿用）
  stages?: { stage: Stage; what: string; action?: string; params?: string[] }[];
  owner?: string;                    // 谁负责
  related?: GlossaryTerm[];
  action?: string;                   // 卡片底部的主链接，走 §3.2 的动作表
}
type Stage = "prepare" | "define" | "build" | "evaluate" | "release" | "observe" | "feedback";
```

- 悬停 / 聚焦时显示 `short` 和 `stages`（一行，用 → 连接）；点击打开术语表里的完整卡片。
- `stages` 里的每一步都是链接，链接带上页头当前选中的智能体（`params: ["agent_id"]`），没有选中的就只跳到页面。
- `stage` 的叫法与工作坊和首页 `LaunchSequence` 一致，让用户看到同一套流程。
- 文案键：`glossary.<term>.short`、`glossary.<term>.stage.<stage>`、`glossary.<term>.owner`。现有的 `glossary.<term>` 迁移到 `.short`，V2 的 `HintIcon`、`HintLabel` 同步改读 `.short`。

### 5.2 第一批术语（Agent-DLC）

`GLOSSARY_TERMS` 新增以下词（中文沿用 `agent-dlc-design` §9.2 和工作坊里的说法）：

| 术语键 | 中文 | 主要出现的步骤 |
|---|---|---|
| `criteria` | 判据 | 定义、评估、放行门、观测、反馈 |
| `criteriaSet` | 判据表 | 定义、评估、发布 |
| `criteriaTier` | 红线 / 门限 / 观察 | 定义、放行门 |
| `goldenSet` | 黄金集 | 定义、评估、放行门、反馈 |
| `goldenSplit` | 开发集 / 回归集 / 保留集 | 评估、放行门 |
| `caseTier` | 用例档位 | 定义、评估 |
| `admission` | 样本准入 | 反馈 |
| `judgeCalibration` | 裁判校准 | 评估 |
| `gate` | 放行门 | 评估、发布 |
| `passK` | pass^k | 评估 |
| `waiver` | 豁免 | 发布 |
| `releaseRecord` | 决策记录 | 发布、观测 |
| `driftWatch` | 漂移监测 | 观测 |
| `repairLadder` | 修复阶梯 | 观测、反馈 |
| `scorecard` | 记分卡 | 评估、观测 |

现有的 19 个通用术语补上 `stages`（可选，P2 做）。

### 5.3 在哪里标注

- **显式标注**：用 `<Term>` 包住页面标题、子页签名（如判据页的 `Filters` 选项）、表头、表单标签里出现的术语。不扫描 DOM 自动加——自动匹配会误伤正文和用户数据。
- **每页只标第一次出现**：页面级的 `TermScope` 上下文记住这页已经标过的术语，后面再出现只显示纯文本，避免满屏虚下划线。
- **可关闭**：帮助菜单里的“术语提示”开关，按用户保存（和 `useLaunchHidden` 同一套存储），关闭后不显示虚下划线，⌘K 和术语表照常可用。
- **本页术语**：页头帮助按钮增加“本页术语”，只列出 `TermScope` 里登记过的词。

## 6. 每页的前置条件清单

把 `LaunchSequence` 的做法推广到功能页：每个页面或子页签可以声明一组前置条件，没满足时，空状态（`Empty`）里显示清单，每一条带跳转链接。

```ts
// standards/requirements.ts — 纯函数，单测
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

第一批：判据页的 **判据表**、**黄金集**、**放行门**、**裁判校准**，以及 **评估任务 → 新建任务**。显示规则：全部满足时不显示；有未满足的，下一条待办点亮，其余变淡，和 `LaunchSequence` 的视觉一致。`needs` 不是当前用户时，这一条显示“需要 biz-owner 操作”。

## 7. 文案与 CI 检查

前端测试新增 `frontend/src/v3/guidance.test.ts`，后端测试新增 `backend/tests/test_error_guidance.py`：

1. **错误码文案**：从后端源码提取全部 `AppError` / `NotFoundError` 错误码，每个都要有 en 和 zh-CN 的 `apiErrors.<code>`。现有缺口记在基线文件 `frontend/src/locales/missing-api-errors.json` 里，**只许删不许加**：新增错误码缺文案时测试失败，补上文案而基线没删时也失败（提醒清理）。
2. **动作表**：后端代码里出现的每个 `next.action` 都在 `NEXT_ACTIONS` 里；每个动作都有 en + zh-CN 文案；`to()` 生成的路径前缀属于 `nav.ts` 里的路由。
3. **术语**：`GLOSSARY_TERMS` 里每个词都有 `.short` 和 `name`（en + zh-CN）；`stages` 里引用的动作存在。
4. **禁用原因**：ESLint 规则，V3 里 `<Btn disabled=…>` 没有 `disabledReason` 也不是 `busy` 时报警告。现有位置同样进基线，只减不增。

## 8. 分期

| 期 | 内容 | 退出标准 |
|---|---|---|
| P1 | 信封 `next` / `needs`；`NEXT_ACTIONS`；`ErrorCallout`；§3.5 的错误码；§5.2 的 15 个术语卡片，标注在判据页和评估任务页；CI 检查 1–3 | 按工作坊从头走一遍，判据页、评估任务页、放行门上遇到的错误都有中文和下一步 |
| P2 | `disabledReason` 和 ESLint 规则；§6 的前置条件清单；通用术语补 `stages`；所有 V3 页面的错误 toast 换成 `ErrorCallout` | V3 页面没有不带原因的禁用按钮（基线为 0）；没有错误 toast |
| P3 | 清空错误码文案基线；V2 页面按需跟进 | `missing-api-errors.json` 为空 |

## 9. 测试

- 后端：`AppError(next=…)` 的信封输出；`/v1` 和 `/share` 不输出 `next` / `needs`；§3.5 每个错误码的 `next` 参数齐全（表驱动）。
- 前端：`ErrorCallout` 渲染三行结构和动作按钮；未知动作被忽略；非管理员看不到授予权限的链接；`TermScope` 每页只标一次；关闭术语提示后不再显示下划线；各 `requirements` 纯函数的表驱动用例。
- E2E：在空工作区里按工作坊主线制造 `release.no_criteria`、`release.empty_golden`、`golden.duplicate_name`，点下一步按钮能落到正确页面和子页签，且页头选中的智能体正确。

## 10. 不在范围内

- 自动替用户修复（比如点一下直接发布判据表）：下一步只负责带路，操作仍由用户在目标页完成，保留签署和审计的语义。
- 全站逐步引导式的交互教程：现有的 `Tour` 不变。
- 改动错误码本身或 HTTP 状态码。
