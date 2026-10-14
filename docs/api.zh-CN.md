# 公开 API(/v1) / Public API

English: [api.md](api.md)

每个已部署的 Agent 都可通过平台的 `/v1` 接口调用——与 Chat 交互页面使用的是同一
条调用链。交互式文档:**`/api/docs`**。

鉴权:`X-Api-Key` 请求头。在控制台创建密钥(Chat → API KEYS),或:

```bash
curl -s -X POST localhost:8000/api/apikeys -H 'Content-Type: application/json' \
  -d '{"name": "integration"}'
# → {"id": "…", "prefix": "lp_live_ab12…", "key": "lp_live_<完整密钥,仅此一次展示>"}
```

密钥以 **哈希(sha256)** 存储——完整密钥仅在创建时展示一次。

**密钥生命周期。** 每个密钥记录创建它的控制台账号(`created_by` = 其 `users.id`,另有
`created_by_name`;在此之前创建的密钥两者均为 `null`,控制台显示为"未知")以及
`last_used_at`(`/v1` 调用成功时写入，每分钟最多一次)。密钥只在其创建者仍有资格持有时有效：
一旦该账号被 **删除、停用或过期**,或(成员账号)**失去** 密钥所属工作空间的 **授权**,
该密钥返回 `401 auth.invalid_api_key`(与任何不可用密钥相同;`/v1` 从不说明原因)并被置为停用。
管理员停用 / 删除账号以及移除授权会立即停用创建者的密钥;过期没有写入事件，因此在密钥下一次被使用时生效。
密钥只会被停用、从不删除——仍列在 `GET /api/apikeys` 中(`created_by`、`created_by_name`、
`last_used_at`、`creator_offboarded`:`deleted` | `disabled` | `expired` |
`no_workspace_grant` | `null`)。在 `shared` 工作空间中任何成员都可停用 / 启用任意密钥；在 `isolated`
可见性下，成员只能列出和启停自己创建的密钥（其他密钥 → `404 apikey.not_found`；管理员可管理全部）。对创建者已失效的
密钥调用 `POST /api/apikeys/{id}/enable` 会被拒绝，返回 `409 apikey.creator_offboarded`
(`detail.reason`)。由配置内置管理员或未开启鉴权时的本地操作者(没有 `users` 行)创建的密钥，
以及旧密钥，永远不会被判定为创建者失效。密钥跟随其**创建者当前**的工作空间可见性：在 `isolated` 下，
成员创建的密钥只能列出和调用该成员能看到的 Agent（其他 Agent → `404 agent.not_found`），每次调用时重新判定；
由管理员、配置内置管理员创建的密钥以及记录创建者之前的旧密钥不受限制。

**密钥限制（可选）。** `POST /api/apikeys` 与 `PATCH /api/apikeys/{id}` 接受三项可选限制；不传即不受限（旧密钥的行为），`PATCH` 中显式传 `null` 表示清除该限制：

| 字段 | 对 `/v1` 的作用 |
|---|---|
| `agent_ids`（密钥所属 workspace 中的 Agent，最多 50 个） | 只能列出和调用这些 Agent；其他 Agent 返回 `404 agent.not_found` |
| `expires_at`（必须是未来时间） | 到期后返回 `401 auth.expired_api_key` |
| `rate_per_minute`（1–100000） | 超限返回 `429 auth.rate_limited`，并带 `Retry-After`（按后端进程计数） |

控制台目前只能按名称新建密钥并启用、停用；这三项限制通过 API 设置。

## 同步调用 / Sync invoke

```bash
curl -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "What is 2+2?", "session_id": null}'
# → {"agent":"…","text":"4","session_id":"…","latency_ms":1234}
```

## 流式调用(SSE) / Streaming invoke

```bash
curl -N -s -X POST localhost:8000/v1/agents/<AGENT_ID>/invoke-stream \
  -H "X-Api-Key: $LP_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "Tell me a two-sentence story."}'
# event: meta   → {"session_id": "…", "mode": "stream"}
# event: delta  → {"text": "Once"} … (增量分片)
# event: usage  → {"input_tokens": 812, "output_tokens": 64, "total_tokens": 876, "model_calls": 1}（运行时上报用量时发送一次）
# event: done   → {"latency_ms": 2100}
```

在下一次调用时传回返回的 `session_id` 即可延续对话(session 上下文与
AgentCore Memory 随之而来)。

流中还可能出现 `tool`(`{name, id}`,一次工具调用开始),以及托管 Harness 的
`policy_denied`——Cedar 策略拒绝了一次 Gateway 工具调用:
`{tool, tool_use_id, reason, policy_id, gateway_id}`(经登录用户的 Gateway 别名调用时,
`tool` 为目录名 `<target>___<tool>`,`gateway_id` 为工作区网关;否则为 Harness 工具名与
`null`;仅当原因中指明判定策略时才有 `policy_id`)。智能体自己的回答仍会继续输出;
客户端可忽略这两类事件。

## Python

```python
import requests

BASE, KEY, AGENT = "http://localhost:8000", "lp_live_…", "<AGENT_ID>"

# 同步
r = requests.post(
    f"{BASE}/v1/agents/{AGENT}/invoke",
    headers={"X-Api-Key": KEY},
    json={"prompt": "How many vacation days does EMP-1024 have left?"},
    timeout=120,
)
print(r.json()["text"])

# 流式(SSE)
with requests.post(
    f"{BASE}/v1/agents/{AGENT}/invoke-stream",
    headers={"X-Api-Key": KEY},
    json={"prompt": "Summarize our HR policy in one line."},
    stream=True, timeout=300,
) as stream:
    for line in stream.iter_lines(decode_unicode=True):
        if line.startswith("data:"):
            print(line[5:].strip())
```

错误使用平台统一信封 `{code, message, detail}`——例如
`auth.missing_api_key`(401)、`auth.invalid_api_key`(401——未知、已停用或创建者已失效的密钥)、`agent.not_active`(409)、`agent.not_found`(404)。

平台没有映射成自有服务错误码(`kb.not_found`、`memory.unavailable` 等)的 AWS 侧失败,
同样以信封返回,而不是裸的 `500 Internal Server Error` 或 botocore 的
`An error occurred (…) when calling the … operation:` 原文。`app/core/errors.py` 中的全局
`ClientError` 处理器按 AWS 错误码映射:

| AWS 错误码 | HTTP | `code` |
|---|---|---|
| `ResourceNotFoundException` | 404 | `aws.not_found` |
| `ValidationException` | 400 | `aws.validation` |
| `AccessDeniedException`、`UnauthorizedException` | 403 | `aws.access_denied` |
| `ThrottlingException`、`TooManyRequestsException`、`ServiceQuotaExceededException` | 429 | `aws.throttled` |
| `ConflictException`、`ResourceInUseException`、`RetryableConflictException` | 409 | `aws.conflict` |

`message` 是去掉 botocore 前缀后的 AWS 消息;`detail` 为
`{"aws_error_code": "<AWS 错误码>", "operation": "<boto 操作名>"}`。其他 AWS 错误码
(如 `InternalServerException`)仍是未处理的 500,后端日志保留完整堆栈。跨账号角色扮演失败
保持原有答复:502 `workspace.assume_role_failed`。`/v1` 共用同一处理器,状态码与 `code` 相同,
但 `message` 是按错误码固定的通用句子(`AWS resource not found`、`AWS rejected the request as
invalid`、`AWS access denied`、`AWS is throttling this request`、`AWS resource conflict`),
`detail` 只含 `aws_error_code`——AWS 原文会暴露本部署的角色 ARN、实例 id 与操作名,这些只留在
API-key 信任边界的控制台一侧。

## 控制台系统 Agent API——托管预置 / Console System Agents API

系统托管预置（见架构文档“系统托管预置”）只能在这里安装、修复与移除。读取不触达 AWS；
安装是显式、计费的操作员动作，走标准部署管道。

| 方法 | 路径 | 角色 | 结果 |
|---|---|---|---|
| `GET` | `/api/system-agents` | 成员 | `{workspace_id, presets[{key, name, label, description, method, skill_version, installed_skill_version, update_available, status, requirements[{code, message}], name_collision, agent_id, agent_status, error, job_id, deployment_id, deployment_status, model_id, model_source, knowledge_bases[], allowed_tools[], memory, settings{model_id, model_source, max_tokens, reasoning_effort, system_prompt, max_iterations, timeout_seconds, knowledge_bases[]} | {}, defaults{同样成员}, editable_fields[], operation, can_install, can_repair, can_configure, can_uninstall, updated_at}]}`——`status ∈ configuration_required | not_installed | deploying | uninstalling | active | failed`；拆除任务持有该行时 `operation` 为 `{kind: uninstall, job_id, job_status, attempt, error, retryable}`，否则为 `null`；条件 code 为 `bootstrap_not_ready | missing_artifacts_bucket | missing_execution_role | per_agent_roles_disabled | missing_oauth_provider`；`memory` 为 `disabled`；仅读台账 |
| `POST` | `/api/system-agents/{key}/install` | 管理员 | 必需的 JSON 请求体，为局部编辑：`{model_id?, model_source?, max_tokens?（1–131072，单次模型调用 = bedrockModelConfig.maxTokens）, reasoning_effort?（low|medium|high；仅 model_source=bedrock 上的 OpenAI 模型）, system_prompt?, max_iterations?（1–100）, timeout_seconds?（10–3600）, knowledge_bases?[{kb_id, name?, description?}], reset?[可编辑字段…], clear?["max_tokens"|"reasoning_effort"], force?}`（`{}` = 安装时预置默认值，修复时已存选择；省略 = 保持，`reset` = 构建默认值，`clear` = 不发送；未知成员 → `422`）→ 有任务在途时 `202 {agent, job_id, deployment_id, created, changed, preset}`（对部署中预置的无请求体修复返回其既有任务且 `changed: false`；部署中的显式编辑 → `409 system_agent.deploy_in_progress {job_id}`；竞争安装返回胜出方的任务，但若请求了不同设置则返回 `409 system_agent.deploy_in_progress`；局部编辑在声明事务内针对当前行解析，声明以版本为条件，行变化后重新解析，三次仍失败返回 `409 system_agent.conflict`），运行中的预置已匹配时 `200` 并带上一任务的 ID；不支持的组合（如给 Claude 模型设置推理强度）返回 `422 system_agent.invalid_options`。知识库在 provision 阶段于目标 Workspace 中核验 |
| `POST` | `/api/system-agents/{key}/skill-registration` | 管理员 | 无请求体（资源由服务端选定：已存 spec 固定的版本，与本构建比对并从 S3 回读）→ `200 {preset_key, record{system, record_id, name, type: AGENT_SKILLS, status, version, descriptors, …}, created, changed, submitted, note, skill{name, version, digest, path, files[]}, preset}`——把**运行中**预置已发布的 Skill 版本注册为独立的 Registry 记录，指向不可变的 `system-skills/<name>/<version>-<digest12>/` 前缀（不写 S3，不重新发布 Harness，Agent 的 A2A 记录不动）。`created` ⇒ 新记录已提交审核（此处绝不批准）；`changed` 而非 `created` ⇒ 描述符前滚到更新版本（DRAFT，重新审核，`recordVersion` 递增）；两者皆否 ⇒ 版本相同、空操作（保留批准）。幂等且竞争安全（持久 `clientToken`、按预置加锁）；部署管道的 register 阶段在安装/修复时执行同样的注册 |
| `DELETE` | `/api/system-agents/{key}` | 管理员 | `202 {agent, job_id, operation: uninstall, attempt, started, preset}`——在一次提交中声明该行（`uninstalling`，乐观条件更新）并排入拆除任务；同时到达的请求共享一个任务（落败方 `started: false`）；独占（按 Agent 的建议锁，单主机）且带围栏的 worker 核实每个知识库目标、Harness 与专用角色均已消失之前（任务上有含精确资源 ID 的逐步 `progress`，仅在已核实时继承到下一次尝试）该行保留身份；拆除失败则进入第 N+1 次尝试；部署进行中返回 `409 agent.deploy_in_progress` |

`GET /api/system-agents` 另外报告 `skill_registration`（`{record_id, pending_record_id, status: creating |
accepted | registered, release_version, release_digest, path, updated_at} | null`，仅读台账；`accepted`
表示我们的创建返回了 `pending_record_id` 但回读尚未核验，`record_id` 在核验前保持 `null`；当前 Registry
中没有映射时为 `null`）与 `can_register_skill`。
Registry 记录（`GET /api/registry/records[/{id}]`、搜索、action/update/reimport 的响应）携带服务端
推导的 `system` 成员——`{managed: true, preset_key, label, skill_version, release_digest, path,
protected_actions[], admin_actions[]} | null`——当且仅当 Workspace 台账在其**当前 Registry** 中把该记录（已核验或已接受的 id）映射到系统预置的 Skill
时设置；绝不从描述符或标签读取。对这类记录，`PUT`、`POST …/reimport` 与 `DELETE` 对所有调用方返回
`403 registry.system_skill_protected`，`POST …/action` 对成员返回同样错误（管理员可提交/批准/驳回/停用），
以保留名称进行普通 Skill 注册/导入返回 `409 registry.name_reserved`。Skill 注册错误码：
`system_skill.preset_not_active`（409）、`system_skill.release_mismatch`（409，本构建的技能包不是
已安装版本）、`system_skill.bundle_unverified`（409，S3 已发布版本与快照不一致；未写入）、
`system_skill.foreign_record`（409，平台无法证明由自己创建的同名 Skill 记录——包括服务不予兑现的
丢失创建重放；不绑定）、`system_skill.stale_release`（409，调用方版本旧于已安装/已核验/意图/远端版本，
或同版本号不同摘要）、
`system_skill.record_deprecated`（409，终态——在 AWS 中删除后重新注册）、
`system_skill.readback_mismatch`（409）、`registry.unavailable`（503）。

错误码：`system_agent.unknown`（404）、`system_agent.workspace_not_ready`（409，
`detail.requirements[{code, message}]`）、`system_agent.name_collision`（409，普通 Agent
占用保留名称——绝不接管）、`system_agent.not_installed`（卸载不存在的预置时 404）、
`system_agent.uninstalling`（拆除任务持有该行或上次尝试失败时，安装/修复返回 409，
`detail.job_id/job_status/error`）、`agent.deploy_in_progress`（部署中卸载时 409）。普通 Agent 路由上，预置在任何 AWS 调用之前返回
`agent.system_managed`（403，`detail.action ∈ redeploy | delete | convert | experiment |
canary | promote | …`，`detail.maintenance_route`）；实验与运行时金丝雀的 action 路由对引用预置的
记录返回同样的错误；`DELETE /api/knowledge-bases/{kb_id}` 在知识库挂载于预置时返回
`kb.attached_to_system_agent`（409，`detail.agents`），无论是否 `force`；用保留名称
`POST /api/agents` 返回 `agent.name_reserved`（409）。每个 Agent 投影都带
`system: {managed, key, label, skill_version, protected_actions} | null`。
`AgentSpec.allowed_tools`（仅 Harness）接受 1–64 字符、匹配 `*|@?name(/tool)?` 的条目。

## 控制台架构助手 API——经审阅的 Harness 提案 / Console Architect Assistant API

架构助手（见 architecture.zh-CN.md →“架构助手（SE-039）”）是成员与受保护预置
`aws-agent-solution-architect` 的对话，终点是一个新托管 Harness 的惰性提案。讨论路由为
`成员`（与对话演练场对等）；批准使用 `perm:agents.deploy`（与 `POST /api/agents` 对等），并在其
写事务内从数据库重新解析调用者的账号、权限与 Workspace 授权。所有路由按 Workspace 限定**且按
principal 绑定**（`user:<id>` / `config-admin` / `local-operator`；用户名仅用于显示）：其他
principal——其他成员、管理员、或同名重新注册的账号——的会话返回
`404 assistant.conversation_not_found`。除特别说明外读取仅访问台账。

会话详情还包含 `context_digest: {text, through_turn, updated_at, generating}`，即已确认事项摘要。
滑出重放窗口的轮次会连同上一版摘要一起，汇总成一份简短的 Markdown 列表：已陈述、已建议、已确认的事项，
以及未决问题。摘要放在轮次前言中、当前提案之前，并标注为可能不完整。一轮结束后，如果下一轮的窗口将省略
摘要尚未覆盖的轮次，就在后台启动一次刷新。刷新是对廉价默认模型（`assistant_digest_model_id`，否则为
`agent_default_model_id`）的一次 Bedrock Converse 调用，读取超时 60 秒，不重试。失败时保留上一版摘要，
任何轮次都不等待它。没有摘要时 `text`、`through_turn`（覆盖到的最后一轮）与 `updated_at` 为 `null`。
本进程内刷新或重建进行中时，`generating` 为 true。摘要只是会话状态：它从不进入提案、提案哈希或批准。

| 方法 | 路径 | 角色 | 结果 |
|---|---|---|---|
| `GET` | `/api/assistant/architect` | 成员 | `{workspace_id, account_id, region, available, reasons[], preset{key, label, status, agent_id, requirements[], can_install}, can_deploy, deploy_requirements[{code, message}], capabilities{shared_memory, kb_gateway}, is_admin, owner, principal}`——`available` ⇔ 预置为 `active`；`capabilities` 说明提案可绑定哪些前置资源（此处绝不创建）；仅读台账 |
| `GET` | `/api/assistant/architect/conversations` | 成员 | `{conversations[{id, title, owner, mine, read_only, shared, shared_by, shared_at, turns, turn_in_progress, status, proposal_status, proposal_revision, created_at, updated_at}]}`——调用者自己的会话，加上所有者或管理员在本 Workspace 共享的会话（`mine: false`），最新在前（≤ 50）；**管理员**列出本 Workspace 所有成员的会话（≤ 200），其他成员未共享的会话带 `read_only: true`；`owner` 为创建人的显示用户名；详情中每条 `messages[]` 带 `author`（`user` 消息的发送人） |
| `POST` | `/api/assistant/architect/conversations` | 成员 | 请求体 `{title?}` → `201` 会话详情（见下）；快照 Workspace **目录**（Registry attachables、每条 Gateway 记录的实时 Gateway ARN + 出站认证身份、每个技能的 S3 内容摘要、ACTIVE 托管知识库、Workspace 前置资源——唯一的 AWS 读取）；预置未运行时 `409 assistant.unavailable`（`detail.preset_status`） |
| `GET` | `/api/assistant/architect/conversations/{id}` | 成员（所有者；共享期间所有成员）；管理员（任意会话，未共享时 `read_only`） | `{…摘要, catalog{fetched_at, tools[{key, kind, name, description, attachable, reason, gateway_arn?, auth_type?, outbound_auth?, url?, record_id}], skills[{key, name, description, path, record_id, content_digest, object_count}], knowledge_bases[{kb_id, name, description}], warnings[], resources{memory_arn, kb_gateway_id, kb_gateway_arn, oauth_provider_arn, execution_role_arn}, target{workspace_id, account_id, region}}, messages[{id, turn, role: user|assistant|tool|error, text, name, at}], proposals[…]}` |
| `PUT` | `/api/assistant/architect/conversations/{id}/sharing` | 成员（所有者）或管理员 | 请求体 `{shared: bool}` → 摘要。向本 Workspace 所有成员开放（或收回）该会话，记录 `shared_by` / `shared_at`；不改变 `updated_at`。只能共享调用者本身可访问的会话（自己的，或已共享的；管理员可访问所有会话，因此可共享任意会话——只读会话由此转为协作），非管理员访问其他成员的私有会话返回 404；对已共享会话，既非所有者也非管理员的协作者返回 403 `assistant.share_forbidden`。共享期间，下列会话路由（发消息、目录、资源准备、提案编辑/驳回/批准、评估计划与评估资产）对所有成员开放，各自原有的权限要求不变（在每次写入内复查，取消共享立即生效）；仅 `footprint` 与 `DELETE`（清除）限所有者（协作者得到 404）。管理员对未共享会话的读取（`GET` 详情、`evaluation-plan`、`evaluation-plan/operations/{id}`）不会放宽任何写入：共享之前，所有写入路由对其返回 404 |
| `POST` | `/api/assistant/architect/conversations/{id}/catalog` | 成员 | 重新读取目录 → `{catalog}` |
| `POST` | `/api/assistant/architect/conversations/{id}/turns` | 成员 | 请求体 `{prompt}`（≤ 100k 字符 / 300k 字节，且须与前言一起放入请求预算）→ SSE `meta{conversation_id, turn, session_id, agent, omitted_turns} → (tool|delta)* → proposal? → done` 或 `error{code?, message}`（保留在记录中；客户端关闭流时留下部分回答 + 一条 `interrupted` 错误行，且无提案）。对预置发起一次携带有界重放记录（按轮次配对，≤ 12 轮 / 含前言 160k 字符）的 `InvokeHarness`；`launchpad-proposal` 块（≤ 64 000 字节）成为新修订（`draft` 或 `invalid`）；**无其他写入**。流打开前：`409 assistant.unavailable`、`409 assistant.turn_in_progress`（`detail.active_turn`——每会话单轮在途）、`409 assistant.conversation_full`（200 轮）、`413 assistant.prompt_too_large` |
| `POST` | `/api/assistant/architect/conversations/{id}/digest/regenerate` | `perm:project.use`（所有者） | 无请求体 → `{context_digest}`（形状见上）。所有者的“有误，重新生成”：同步地从重放窗口之外的全部轮次重建摘要，忽略已存储的摘要（一次 Converse 调用，输入上限 60 000 字符）。只写台账，并以已存储的摘要为写入条件，因此并发刷新不会存下第二份结果。共享会话的协作者可以读取摘要，但这里返回 `403 assistant.digest_owner_only`；其他人返回 `404 assistant.conversation_not_found`。`409 assistant.digest_nothing_to_summarize`（所有轮次仍在重放窗口内）、`409 assistant.digest_busy`（该会话已有刷新或重建在进行）、`409 assistant.conversation_clearing`、`502 assistant.digest_failed`（模型调用失败；保留上一版摘要） |
| `PUT` | `/api/assistant/architect/conversations/{id}/proposal` | 成员 | 请求体 `{content}`（提案白名单；未知外层成员 → 422）→ `{proposal}`——一个**新**修订（`source: member`，唯一单调编号），绝非就地修改；无效内容按 `invalid` 保存并附 `validation_errors`，从不被更正；序列化超过 64 000 字节返回 `413 assistant.proposal_too_large`（不存储任何内容；规范化后的存储内容按同一上限复查）；达到 50 个修订返回 `409 assistant.conversation_full`。所有助手写请求同时在入口受限：实际接收超过 512 000 字节返回 `413 assistant.request_too_large` |
| `POST` | `/api/assistant/architect/conversations/{id}/proposal/reject` | 成员 | 请求体 `{revision}` → `{proposal}`，`status: rejected`（不可执行）；条件转换——非当前修订返回 `409 assistant.proposal_stale`，期间已执行的修订返回 `409 assistant.proposal_already_approved`（`detail.approval`） |
| `POST` | `/api/assistant/architect/conversations/{id}/proposal/approve` | `perm:agents.deploy` | 请求体 `{revision, content_hash}` → 本次调用声明该修订、声明 Agent 名称（与 `POST /api/agents` 共享）并在一次提交中创建普通 Agent + Deployment + `deploy_agent` 任务且把其 ID 写到提案上时返回 `202 {proposal, agent, job_id, deployment_id, started: true}`；重复、并发或历史（已批准，即使有更新修订）请求返回已记录结果 `200 … started: false`——并重新唤醒仍 `queued` 且无活跃 worker 的任务。拒绝均发生在任何写入之前：`401 auth.required` / `403 auth.permission_required` / `403 workspace.forbidden`（实时目录读取之后、在声明处从数据库重新解析）、`409 assistant.proposal_stale`（修订不存在 / 哈希不同 / 批准期间变化）、`409 assistant.proposal_not_approvable`（无效、已取消、已被替代）、`409 assistant.workspace_not_ready`、`409 assistant.proposal_invalid`（实时目录已无所引用资源或前置条件——如知识库 Gateway）、`409 agent.name_reserved`、`409 agent.name_exists`（原子——两个竞争创建者之一）、`409 assistant.bindings_changed`（`detail.changed[]`——某个 key 解析到了与审阅时不同的 URL、Gateway 认证身份、技能内容、记忆或知识库 Gateway）、`502 assistant.catalog_unavailable`（实时目录无法读取且无胜出方）；每个“已批准”响应都先重新校验调用者 |

提案为 `{id, conversation_id, revision, source: model|member, status: draft|invalid|approved|
rejected|superseded, content, content_hash, bindings, validation_errors[], created_by,
created_at, approval, rejected_by, rejected_at}`。`content` 是白名单对象 `{version: 1, name,
model_id, model_source, system_prompt, tools[key], skills[key], knowledge_bases[kb_id],
memory: disabled|workspace, max_iterations, timeout_seconds, summary,
requirements_baseline[], assumptions[], manual_tasks[], golden_tests[{id, input,
expected_response, expected_tools[], forbidden_behavior, pass_criteria, evaluator,
source}], evaluator_recommendations[]}`；`bindings` 是解析后的 `{name, method, model_id,
model_source, tools[ToolRef], skills[s3 路径], knowledge_bases[KnowledgeBaseRef],
memory{short_term, long_term, memory_id}, max_iterations, timeout_seconds,
resources{gateways{<gateway_id>: {gateway_arn, gateway_name, record_id, auth_type,
outbound_auth}}, remote_mcp{<name>: {url, record_id}}, skills{<key>: {record_id, path,
source_prefix, content_digest, object_count, total_bytes}}, kb_gateway{gateway_id, gateway_arn,
oauth_provider_arn, url, authorizer_type, authorizer} | null, memory{mode, arn}, execution_role_arn}}`（无效时为 null；`outbound_auth` 是身份——提供方
ARN、授权类型、scope——绝非凭据值）；`content_hash` = 规范化 `{content, bindings}` 的 sha256。
部署任务的 payload 携带 `{content, bindings}` 并在任务入口重新检查（漂移 → 任务在任何阶段之前
失败）。可观测路由（`/api/observability/sessions*`、`/traces*`、`…/evaluate`）隐藏其他 principal
的助手会话，其详情返回 `404 observability.session_not_found` / `observability.trace_not_found`。`approval` 为 `{approved_by, approved_at, agent_id,
agent_name, agent_status, agent_error, deployment_id, job_id, job_status} | null`；任务与
Agent 是普通行，可通过 `GET /api/jobs/{id}` 与 `GET /api/agents/{id}` 读取。通用调用入口
（`POST /api/chat/{id}`、`POST /api/agents/{id}/invoke`、`/v1 …/invoke[-stream]`）对系统托管
Agent 上助手轮次的 `session_id` 返回 `404 chat.session_not_found`。

### 评估资产计划（SE-047）

仅对话所有者可见（其他主体 / Workspace → `404 assistant.conversation_not_found`，管理员亦然）。准备 / 编辑为
`member`；创建与清理为 `admin` **且**为所有者。读取仅访问账本。

| 方法 | 路径 | 角色 | 结果 |
|---|---|---|---|
| `GET` | `/api/assistant/architect/conversations/{id}/evaluation-plan` | member | `{plans[…], operations[…], disclosure}` |
| `POST` | `…/evaluation-plan/prepare` | member | `{revision}` → `201 {plan, …}` 平台草稿（旧版黄金测试的场景带 `review_required`，草稿在全部确认或阻止前为 `invalid`）；无副作用 |
| `PUT` | `…/evaluation-plan` | member | `{content}` → 新修订（`draft` 或带 `validation_errors` 的 `invalid`）；超过 160 000 字节 → `413` |
| `POST` | `…/evaluation-plan/materialize` | `perm:agents.deploy`（所有者） | `{plan_revision, plan_hash, acknowledge_disclosure: true}` → `202 {operation, started: true}`（原子声明：仍为 draft、同哈希、最新版本；启动 worker）或 `200 … started: false`（重复 / 并发请求，同一操作）。`422 assistant.disclosure_required`、`409 assistant.evaluation_plan_stale`（版本 / 哈希不符或期间被编辑 / 取代 / 声明）、`409 assistant.evaluation_plan_not_approvable`、`409 assistant.evaluation_plan_invalid`、`409 assistant.workspace_not_ready`、`409 assistant.execution_role_untrusted`（请求授权但执行角色无平台标签）；调用者与 Workspace 身份在声明事务内重新解析并钉在操作上 |
| `GET` | `…/evaluation-plan/operations/{operation_id}` | member | `{operation}`——仅账本，不调用 AWS |
| `POST` | `…/operations/{operation_id}/retry` | `perm:agents.deploy`（所有者） | 恢复持久化意图（同令牌 / 同请求）；5 次后 `409 assistant.evaluation_assets_exhausted` |
| `POST` | `…/operations/{operation_id}/lambda-revision-review` | `perm:agents.deploy`（所有者） | body `{plan_hash, expected_created_revision_id, expected_current_revision_id, cloudtrail_event_id, reason}`（`extra="forbid"`，reason 非空）→ `{operation, review, started}` —— **SE-049 经审阅的单一冲突恢复**：`CreateFunction` 返回的 RevisionId 在函数仍处于 `Pending` → `Active` 初始化期间发生变化，worker 拒绝发布（`resources[lambda_function].review.kind = initial_revision_changed`）。服务端自行读取指定的 CloudTrail 事件（按 `EventId` 调用 `LookupEvents`，从不信任客户端 JSON），要求**恰好一条**来自 `lambda.amazonaws.com`、位于钉住账号/区域的成功 `CreateFunction20150331` 记录，其请求字段等于记录的请求，响应带有记录的 FunctionArn、期望的初始 RevisionId、审阅过的 CodeSha256、`state = Pending` / `stateReasonCode = Creating` 与 `lastModified`；随后当前 `$LATEST` 必须等于该响应加上**仅有**的生命周期变化 —— `Active` / `LastUpdateStatus Successful`、恰为 `expected_current_revision_id`、同一 `LastModified`、所有已批准字段与全部可选安全相关成员（环境变量、层、VPC、KMS、文件系统、死信、签名、架构、追踪、日志、临时存储、SnapStart、镜像配置、运行时版本）一致 —— 且 `$LATEST` 是唯一版本、无别名、无资源策略、无预留并发，角色 / 日志组仍保持记录的身份。成功时在资源上追加**只增不改**的审阅条目（`reviews[]`：审阅人、原因、事件 ID / 时间 / 请求 ID、核验字段、新旧快照、精确的计划绑定；原始 CreateFunction 证据永不覆盖），基线 `revision_id` / `settled_revision_id` 移到审阅值，Lambda 冲突及其被阻塞的依赖（`lambda_permission`、`role_grant`、代码评估器）重新排队，由普通 worker 继续 —— 其 `PublishVersion` 仍带两项前置条件，之后的变更在那里失败。**本路由不做任何云端写入。**完全相同的重复请求幂等（`started: false`，返回已记录的审阅，不再读 CloudTrail）；审阅后的不同请求为 `409 assistant.lambda_revision_review_stale`。拒绝（不记录、不写入）：`422 assistant.lambda_revision_review_reason_required`、`409 assistant.evaluation_plan_stale`（哈希）、`409 assistant.lambda_revision_review_not_applicable`（操作非 partial/failed、函数不是仅因发布前 RevisionId 漂移而阻塞的自有已接受创建 —— 丢失的创建、已发布版本、发布意图或已重钉基线 —— 或存在其他无关的未决结果）、`409 assistant.lambda_revision_review_stale`（初始 RevisionId / 变化不匹配、已有不同审阅）、`409 assistant.lambda_revision_review_unverified`（`detail.fields` 列出差异成员；也包括 0 / 2+ / 畸形事件、未终止的事件历史、`InProgress` / `Failed` 更新状态、多余版本 / 别名 / 策略 / 并发、角色或日志组身份变化）、`409 assistant.evaluation_assets_running`、`409 assistant.evaluation_assets_exhausted`、`409 assistant.evaluation_assets_stopped`。四方比较沿已安装的 Lambda 模型无损进行：**记录的请求** ↔ 事件 `requestParameters`（逐成员；多出的环境变量等成员不属已批准）、**不可变的接受响应**（`create_response`，若存在）↔ 事件 `responseElements`、该响应 ↔ **当前 `$LATEST`**（所有非生命周期成员的存在、缺失与相等一并比较 —— 多出的 `DurableConfig` / `TenancyConfig` / `CapacityProviderConfig` / `MasterArn` 或平台未知的成员都是差异），且响应的每个成员必须由请求固定或属于文档记载的服务默认值（`PackageType Zip`、`x86_64`、`PassThrough` 追踪、512 MB 临时存储、SnapStart 关闭、写入 `/aws/lambda/<name>` 的文本日志、本区域运行时版本 ARN）。仅结构成员名做大小写归一；数据映射的键 / 值（环境变量、标签）与空字符串都是内容；存在性以显式的"缺失"哨兵比较（成员以 `null` 或错误类型出现即为差异，绝不等同于缺失），各方在比较前都按已安装的 Lambda 模型做类型校验（畸形值被拒绝而非归一），唯一的"缺失即为空"等价仅限文档记载信封的良构空形态（`environment: {}` / `{"Variables": {}}`、`Layers` / `FileSystemConfigs` `[]`、全空且类型正确的 `VpcConfig`），`CodeSize` 保留在接受响应中并在响应、事件与当前函数之间比较。依赖与记录快照重新比对（RoleId / ARN / 信任策略 / 内联策略 / 标签；日志组 creationTime / ARN / 保留期 / 标签），资源策略只有 `NotFound` 才证明不存在 —— 空文档或不可读文档均被拒绝。审阅的条件 UPDATE 把审阅所依赖的每个值都绑定为同一语句的谓词 —— 操作的状态 / 租约令牌 / 尝试次数 / 计划 id / 计划修订 / 计划哈希 / 所有者 / 审批人 / 精确的 pinned JSON / 精确的意图 JSON，已批准的计划行（id / 修订 / 状态 / 哈希列**以及**经校验内容的精确 JSON，其规范哈希已核对），对话所有者（== 操作记录的所有者 == 调用者），Workspace 账号 / 区域 / 角色 / 外部 id **以及**其精确的 `resources` JSON（含执行角色），审批人**与**审阅人的活跃未过期管理员行 ——并在主机锁内从数据库重新解析调用者，因此另一会话在该语句之前提交的任何变更都使其成为空操作（`409 assistant.evaluation_assets_stopped`）。核验状态作为 `reviewed_baseline` 持久化在资源上，恢复的 worker 在首次变更前立即重新校验（完整配置 + 标签、依赖、`$LATEST` 为唯一版本、无别名、策略不存在、无预留并发）—— 外部发布的同代码版本、他人的别名 / 策略 / 并发或任何配置漂移都是 `conflict`，永不采纳或覆盖，且不可再次审阅。与审阅无关地，普通 worker 在首次 `PublishVersion` 被拒后不会采纳同摘要版本（只有自身派发丢失响应的情况才对账），也永不覆盖不是自己设置的预留并发。限制：CloudTrail 是供人工审阅的正向证据，不能证明没有其他写入（事件历史最终一致，只列出已记录的内容）；控制台需要 Workspace 角色具备 `cloudtrail:LookupEvents`；外部管理员的检查→写入窗口仍为一次调用宽 |
| `DELETE` | `…/operations/{operation_id}/assets` | `perm:agents.deploy`（所有者） | 按依赖顺序仅删除身份仍匹配的自有云端资产，每次生效后持久化；本地 Dataset 保留；worker 活跃时 `409 assistant.evaluation_assets_running`，批准者或 Workspace 身份变化时 `409 assistant.evaluation_assets_stopped` |
| `GET` | `…/conversations/{conversation_id}/footprint` | 成员（所有者） | 「清除」将移除的内容：`{agents[], canaries[], experiments[], operations[], datasets[], blockers[], required_permissions[], can_clear, purge, turns, proposals}`（`canaries` / `experiments` 为在本会话 Agent 上运行的金丝雀与实验，`pending` 表示清理尚未执行或有残留；`blockers[].kind` 可为 `turn` / `preparation` / `operation` / `job` / `canary` / `experiment`；`purge` 为最近一次清除任务；涉及云端资产时 `required_permissions` 含 `agents.deploy`，涉及 Agent 时含 `agents.delete`；`can_clear` 表示调用者全部具备）；仅读账本 |
| `DELETE` | `…/conversations/{conversation_id}` | 成员（所有者），并须具备 `required_permissions` 中的每项权限 | 只有对话记录的会话直接删除（`200 {deleted: true, …}`）；其余情况为持久化清除任务：`202 {deleted: false, job_id, status, …}`，通过 `GET /api/jobs/{job_id}` 轮询（`payload.step`、`payload.result`，失败时 `payload.blocker`）。按依赖顺序，每步经 AWS 回读确认后才进行下一步：Agent 上的金丝雀（停止并 `act_cleanup`，等待 A/B 测试、在线评估、网关与两个命名端点均不存在）→ 实验 → 对每次可清理的评估资产操作执行带围栏的清理 → 本地 Dataset（手动同步到 AWS 的副本保留）→ 每个 Agent（删除资源、等待不存在、其执行角色、其自身的 A2A Registry 记录、账本）→ 金丝雀候选包 → 账本行。删除任何内容之前的拒绝：`409 assistant.conversation_busy`（轮次、导入、操作、部署任务或金丝雀 / 实验动作在进行）、`403 auth.permission_required`。任务进行时重复 DELETE 返回同一任务；期间会话写入返回 `409 assistant.conversation_clearing`，其 Agent 的金丝雀 / 实验 / 重新发布 / 转换 / 删除路由返回 `409 assistant.agent_clearing`。无法完成的任务以 `failed` 结束，`payload.blocker.code` 为 `assistant.purge_canary_incomplete`、`purge_experiment_incomplete`、`conversation_assets_remain`、`purge_still_deleting`、`purge_delete_failed`、`purge_agent_dependency`、`purge_agent_conflict`、`purge_agent_busy`、`purge_role_remains`、`purge_dependency_busy`、`purge_unowned`、`purge_step_failed` 之一（或拒绝本身的代码）；会话保留，下一次 DELETE 从已确认的检查点继续 |

操作为 `{id, plan_id, plan_revision, plan_hash, proposal_revision, approved_by, account_id, region,
pinned{…}, status: queued|running|succeeded|partial|failed|cleaning|cleaned, attempts, max_attempts,
dataset_id, error, resources[{kind, key, plan_key?, name, status: pending|accepted|ready|failed|conflict|
blocked|skipped|retained|deleted|delete_failed, error, digest?, reference_dependent?, owned?, recovered?,
result, cleanup?, link?}], running}`。`POST /api/eval/runs` 对缺少参考的托管代码评估器返回
`422 run.judge_needs_ground_truth`（模拟人设条目的轮次在运行时才生成，报告为 `<scenario>/simulated turns
lacks expected_response`）；所选自定义评估器读取失败时返回 `422 run.evaluator_unverifiable`（需求未知不等于已验证，
不会启动任何下游步骤），不存在时返回 `422 run.evaluator_not_found`；在线评估拒绝此类评估器。普通 `DELETE /api/eval/evaluators/{id}` 对操作拥有
的评估器返回 `409 evaluator.managed_by_operation`。`GET /api/assistant/architect` 新增
`can_materialize_evaluation_assets`（= 调用者具备 `agents.deploy`）。

## 控制台 MCP 工具审批 API / Console MCP tool review API

托管 MCP 服务器（`spec.protocol == "mcp"`）的工具清单经过审批之前不会对外提供（设计 projects §15.6）。部署流水线每次创建或重新同步服务器的网关目标时，都会经网关列出工具，并快照为 `[{name, description, input_schema}]`（按名称排序，去掉 `<target>___` 前缀）。快照的 SHA-256 与最近一次**已批准**的清单比较：一致则闸门打开；首次部署、改动了工具的重新发布或漂移则打开一条审批，并再次删除目标，因此 Agent 既不能调用也看不到新工具。目标早于工具审批存在的服务器，会在下一次部署时把当时的实时清单记为已批准的 `baseline`（由 `system:baseline` 记录），之后只有变更才需要审批。

这些路由与其他 Agent 路由一样属于 Workspace + 项目范围。读取需要 `perm:project.use`，审批需要项目级权限键 `perm:mcp.approve`，漂移检查需要 `perm:project.write`。服务器的部署者（最近一次部署的操作者，否则为创建者）永远不能审批自己的审批单，管理员也不例外：`409 mcp_review.self_decide`。其他项目的服务器、或查看者无权看到的服务器返回 `404 agent.not_found`；不是 MCP 服务器的 Agent 返回 `409 agent.not_mcp_server`。

审批单为 `{id, agent_id, version, target_name, tools_hash, tool_count, base_review_id, diff{added[], removed[], description_changed[], schema_changed[]}, hints[{tool, kind, detail}], status: reviewing|approved|rejected|superseded, source: deploy|drift|baseline, requested_by, decided_by, decided_at, note, publish_status: publishing|published|failed|null, publish_error, created_at}`。需要注意的字段：

- `diff` 相对于最近一次已批准的清单，即 Agent 实际看到的清单。
- `hints` 标出审批人应仔细阅读的内容，`kind` 为 `dangerous_verb`、`write_name`、`missing_description`、`injection_phrase`、`url`、`cross_tool_reference` 或 `long_description`。提示本身从不拒绝任何东西。
- 更新的清单会取代（superseded）更早的待审批单，因此同一时间最多一条处于 `reviewing`。

| 方法 | 路径 | 权限 | 结果 |
|---|---|---|---|
| `GET` | `/api/mcp-servers/{agent_id}/reviews` | `perm:project.use` | `{agent_id, state: none\|reviewing\|approved\|rejected, open_review_id, approved_review_id, approved_hash, reviews[review]}`，最新在前，不含工具清单。有清单待审时 `state` 为 `reviewing`，否则为最近一次决定；首个清单出现前为 `none` |
| `GET` | `/api/mcp-servers/{agent_id}/reviews/{review_id}` | `perm:project.use` | 审批单，另加 `tools[]` 与 `base_tools[]`（用于对照的已批准清单，首次审批为 `null`），便于并排阅读。其他服务器的审批单返回 `404 mcp_review.not_found` |
| `POST` | `/api/mcp-servers/{agent_id}/reviews/{review_id}/approve` | `perm:mcp.approve` | 请求体 `{note?（≤ 2000）}` → 审批单详情。批准后在后台重新创建被扣下的网关目标，并再次经过闸门：服务器列出的工具必须仍与批准的完全一致。结果见 `publish_status`。审批单已有结论时返回 `409 mcp_review.not_open` |
| `POST` | `/api/mcp-servers/{agent_id}/reviews/{review_id}/reject` | `perm:mcp.approve` | 请求体 `{note?}` → 审批单详情。目标保持扣下，直到新的部署获得批准 |
| `POST` | `/api/mcp-servers/{agent_id}/reviews/check` | `perm:project.write` | 重新同步实时目标，并把其工具与已批准清单比较，返回 `{checked, drift, review_id?, reason?}` 并合并上面的闸门状态。服务器可以不经重新部署就改变工具；一旦不同，就打开一条 `drift` 审批并扣下目标。无法读取清单时同样扣下（失败即关闭）。没有存活目标时为 `checked: false, reason: "no_target"` |

打开审批单记入审计事件 `mcp.review.deploy` / `.drift`，决定记入 `mcp.review.approve` / `.reject`。V3 Agent 页的 MCP 服务器面板展示闸门状态、待审批单及其差异与提示，以及批准、驳回操作。

**MCP 服务器的注册记录。** 对 `descriptorType` 为 `MCP` 的记录执行 `POST /api/registry/records/{record_id}/action` 的 `approve` 或 `publish`，还需要 `perm:mcp.approve`（`403 auth.permission_required`）。若该记录对应本 Workspace 的一个托管 MCP 服务器（同一 `target_name`），则该服务器的工具审批必须先为 `approved`，否则返回 `409 mcp.review_pending`（`detail.agent_id`）。其他记录类型与操作的生命周期不变。

## 控制台 Registry API——实时名片 / Console Registry API: live agent card

`GET /api/registry/records/{record_id}/live-agent-card` 是 Registry 抽屉「AGENT 名片」区块中「实时名片」按钮背后的读取：
该记录背后的运行时*此刻*实际提供的 A2A 名片，与记录在部署时存储的名片并列展示。这是一次按需的数据面调用（`GetAgentCard`），
只在操作者点击时发起——打开抽屉不会触发——且不落任何状态。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/registry/records/{record_id}/live-agent-card` | `{agent_id, runtime_arn, status_code, card, diff}`——`card` 是运行时提供的 JSON 文档（`GetAgentCard.agentCard`，即 A2A 客户端在 `/.well-known/agent-card.json` 读到的内容），`status_code` 为 `GetAgentCard.statusCode`；`diff` = `{identical, fields[{field, record, live}], skills_only_in_live[], skills_only_in_record[]}`，将 `name`/`url`/`version`/`protocolVersion` 与技能 id 集合同记录的 `descriptors.a2a.agentCard.inlineContent` 比较（描述、capabilities 与平台 `metadata` 块不参与比较）。路由在服务端完成 记录 → 账本 Agent（`Agent.registry_record_id`，同 workspace，未删除）→ `Agent.arn` 的解析；浏览器从不提供 ARN。不发送 `runtimeSessionId`；AWS 为提供名片而打开的会话随即以 `StopRuntimeSession` 结束，失败时仅记录日志，名片仍会返回 |

错误码（均在调用 AWS 之前由账本判定）：`registry.record_not_deployed`（404，没有 Launchpad Agent 拥有该记录）、
`registry.record_not_a2a`（409，Agent 的 `spec.protocol` 不是 `a2a`）、`registry.agent_not_ready`（409，Agent 不处于
`active` 或尚无运行时 ARN）。数据面 `ClientError` 映射为标准 4xx 信封（`aws.not_found`、`aws.access_denied`、
`aws.throttled` 等）；没有映射的运行时侧失败（`RuntimeClientError`）为 `registry.live_card_failed`（502），并带
`detail.aws_error_code`——绝不会是裸 500。无需 IAM 变更：控制台角色已具备 `bedrock-agentcore:*`。

## 控制台 Registry API——消费者视图 / Console Registry API: consumer view

Registry 页面从两个侧面展示同一个注册中心。**发布者列表**（`GET /api/registry/records`，控制面 `ListRegistryRecords`）是运维者管理的全集：所有状态的全部记录。**消费者视图**（`?view=discoverable`）则是拥有数据面访问权限的消费者或 Agent 实际能看到的记录——即 GA 发现 API `ListDiscoverableRegistryRecords`。出现在前者而不在后者中的记录，就是尚未经批准对外暴露的记录；两份列表都拿到后，控制台会给它们打上「不可发现」标签。只读，不落任何数据。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/registry/records/discoverable?type=` | `{records[{record_id, name, display_name, description, type, descriptor_types[], status, status_reason, version, created_at, updated_at}], count}`——数据面 `ListDiscoverableRegistryRecords(registryId=<workspace 注册中心>, maxResults=100)`，按 `nextToken` 翻页到底；可选的 `type` 以 `filters=[{name: "recordType", values: [<GA 类型>]}]` 收窄，接受平台名（`A2A`/`MCP`/`AGENT_SKILLS`）或 GA 名（`agent`/`mcp`/`skill`）；行内的 `type` 始终是平台名。摘要从不包含 `descriptors`——需要载荷时读取 `GET /api/registry/records/{record_id}`。`count` 为行数 |

错误码：`registry.bad_type`（422，未知的 `type`）、`registry.unavailable`（503，该 workspace 没有注册中心）。AWS `ClientError` 映射为标准 4xx 信封（`aws.access_denied`、`aws.throttled` 等），绝不返回裸 500。路由策略为 MEMBER，与其他 Registry 读接口一致。

## 控制台治理 API / Console Governance API

以下 `/api` 路由支撑需要鉴权的控制台，不属于公开的 `/v1` Agent 调用契约。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/governance/gateways` | 实时的 MCP Gateway 清单 |
| `GET` | `/api/governance/gateways/{id}` | 目标（每个带 `kind: {protocol, variant}`）、actions 与 `actions_uncovered_targets`、Registry、Engine、IAM 以及可挂接性详情 |
| `POST/DELETE` | `/api/governance/gateways/{id}/manage` | 仅添加/移除 Launchpad 纳管标签 |
| `GET` | `/api/governance/gateways/{id}/registry-preview` | Gateway 级记录差异与遗留记录匹配 |
| `POST` | `/api/governance/gateways/{id}/registry-import` | 创建/复用/更新并提交；绝不批准 |
| `POST` | `/api/governance/gateways/{id}/retire-legacy-records` | Gateway 记录获批后的显式退役 |
| `POST` | `/api/governance/gateways/{id}/engine` | 以所选模式（默认 `ENFORCE`）创建/采用并挂接一个 Engine |
| `GET/POST` | `/api/governance/gateways/{id}/policies` | 列出或创建 `LOG_ONLY` 策略 |
| `PUT` | `/api/governance/gateways/{id}/policies/{policy_id}` | 更新 LOG_ONLY 策略，或创建 ACTIVE 策略候选 |
| `POST` | `/api/governance/gateways/{id}/policies/{policy_id}/promote` | 以证据为门禁的激活/切换 |
| `POST` | `/api/governance/gateways/{id}/policies/{policy_id}/rollback` | 有审计记录的快照/候选回滚 |
| `POST` | `/api/governance/gateways/{id}/mode` | Gateway `LOG_ONLY`/`ENFORCE` 模式切换 |
| `POST` | `/api/governance/gateways/{id}/generations` | 启动自然语言 → Cedar 生成，仅供审阅 |
| `GET` | `/api/governance/gateways/{id}/generations/{generation_id}` | 轮询生成状态并读取草稿资产 |
| `GET` | `/api/governance/gateways/{id}/decisions` | AWS 决策投影，或显式的不可用状态 |
| `GET` | `/api/governance/gateways/{id}/rate-limits` | `{rate_limits: [...]}`：该 Gateway 的全部限流规则（跟完所有 `nextToken` 分页）；对任意 Gateway 可读 |
| `POST` | `/api/governance/gateways/{id}/rate-limits` | 创建 → `201` 返回创建的记录；仅限已纳管 Gateway |
| `PUT` | `/api/governance/gateways/{id}/rate-limits/{rate_limit_id}` | 整体替换 `entries`（可带 `description`）；`dimensionKeys` 不可变，携带则 `422` |
| `DELETE` | `/api/governance/gateways/{id}/rate-limits/{rate_limit_id}` | 删除 → `{deleted: true, id, status}` |
| `POST` | `/api/governance/gateways/{id}/targets/{target_id}/synchronize` | 对单个动态 MCP 服务器目标执行 `SynchronizeGatewayTargets` → `202` 返回目标投影（`status` = `SYNCHRONIZING`，`kind` 与详情一致）；仅限已纳管 Gateway（`409 governance.gateway_not_managed`）；目标不可同步 → `409 governance.target_not_synchronizable`，`detail.reason` ∈ `not_mcp_server`、`static_tool_schema`、`pending_auth`、`synchronizing`、`not_ready`；审计操作名 `target.synchronize` |
| `GET` | `/api/governance/gateways/{id}/audit` | 不可变的本地变更日志 |
| `GET` | `/api/governance/operations/{operation_id}` | 异步 operation 状态 |

`GET /api/governance/gateways/{id}` 详情与同步响应中的每个目标都是同一投影
`{id, name, status, status_reasons, description, kind, listing_mode, last_synchronized_at,
synchronizable, not_synchronizable_reason}`。`kind` 为 `{"protocol": "mcp" | "http" | "inference" |
"unknown", "variant": <联合成员键> | null}`，即 AWS 实际设置的 `TargetConfiguration` 成员（`mcp/lambda`、
`mcp/mcpServer`、`mcp/openApiSchema`、`http/passthrough`、`http/agentcoreRuntime`、`inference/provider` 等）；
空配置为 `unknown`/`null`，未识别的成员映射为 `protocol: <key>` / `variant: null`。详情还带有
`actions_uncovered_targets: [name, …]`，即没有工具 schema、因此绝不会出现在 `actions` 中的 `http` /
`inference` 目标。

策略与 Gateway 变更返回 `202`：

```json
{"operation": {"id": "...", "status": "pending", "operation": "policy_create"}}
```

限流路由（AgentCore **Gateway 限流**，2026 年 8 月 GA）是**同步**的——没有可轮询的 operation。
一条限流规则为 `{id, gateway_id, description, dimension_keys, entries, status, created_at, updated_at}`，
`status` ∈ `CREATING | ACTIVE | UPDATING | DELETING`。创建请求体：

```json
{
  "dimension_keys": ["targetName", "$.context.jwt.sub"],
  "entries": [
    {"dimensions": {"targetName": "office-facts", "$.context.jwt.sub": "*"},
     "requests": [{"rate": 10, "period": "second"}],
     "tokens": [{"rate": 5000, "period": "minute"}]},
    {"dimensions": {"targetName": "*", "$.context.jwt.sub": "*"},
     "requests": [{"rate": 60, "period": "minute"}]}
  ],
  "description": "per-target RPS with a default bucket"
}
```

校验在任何 AWS 调用之前完成，失败返回 `422 governance.rate_limit_invalid`，`detail.reason` ∈
`dimension_keys_count | dimension_key_unknown | dimension_key_duplicate | entries_count | entry_dimensions_mismatch |
entry_dimension_empty | wildcard_not_trailing | entry_no_metric | rate_config_count | rate_out_of_range |
period_not_allowed | description_too_long | dimension_keys_immutable`：1–10 个键，取自 `targetName`、`toolName`、
`qualifiedModelId`、`$.context.jwt.<claim>`、`$.context.iam.principal`、`$.context.iam.sourceIdentity`；1–1000 个条目，
每个条目的 `dimensions` 恰好包含父级键；`*` 只能出现在尾部位置；每个条目至少一个指标；`rate` 0–10 000 000；
`requests` 按 `second`/`minute`，`tokens` 仅 `minute`，`connections` 仅 `second`；描述 ≤ 512 字符。
未纳管 Gateway 上的变更返回 `409 governance.gateway_not_managed`；键集合重复或 Gateway 正忙时 AWS 抛出
`ConflictException` → `409 aws.conflict`。每次变更都以 `rate_limit.create` / `rate_limit.update` / `rate_limit.delete`
记入审计路由（`before` = 变更前记录或 `{}`，`requested` = 载荷，`after` = AWS 响应，状态 `succeeded`/`failed`）。

生成启动返回 `{"operation": …, "generation_id": …, "status": …}`；生成出的资产只是供编辑器使用的草稿，
绝不会激活任何策略。

轮询 operation 路由，直到状态为 `succeeded`、`failed`、`partial` 或 `interrupted`。`interrupted` 表示重启后
无法证明 AWS 侧的效果，该 operation 必须被显式重试——后端绝不自动重放。变更请求携带适用于该 operation 的
实时时间戳与确认信息：

```json
{
  "expected_gateway_updated_at": "2026-07-16T09:00:00+00:00",
  "expected_policy_updated_at": "2026-07-16T09:01:00+00:00",
  "acknowledged_gateway_ids": ["gw-a", "gw-b"],
  "confirmation_name": "finance-gateway",
  "override_reason": null
}
```

常见冲突码有 `governance.gateway_not_managed`、`governance.concurrent_change`、
`governance.shared_engine_changed`、`governance.iam_preflight_failed`、`governance.evidence_required`、
`governance.policy_engine_deleted` 与 `governance.registry_record_not_approved`。

当 Gateway 仍引用一个已被带外删除的 Policy Engine 时，读操作不会失败，而是以 `policy_engine.missing = true`
与 `status = "DELETED"` 报告该引用；策略变更返回 `409 governance.policy_engine_deleted`；`POST .../engine`
把该引用视为未挂接：创建一个新的 Engine，以所选模式挂接，并把被替换的 ARN 记录在 operation 上。

## 控制台资源创建者 / Console resource creators

每个用户管理资源的列表与详情响应都带两个可为空的字段，记录是谁通过控制台创建了它：

```json
{"created_by": "3f2a…", "created_by_name": "ada"}
```

`created_by` 是创建者的 `users.id`——配置 admin 与关闭认证时的本地操作员没有 `users` 行，记为 NULL；
`created_by_name` 是创建时的用户名，仅用于展示。早于该字段的旧行、在控制台之外创建的资源，以及没有人类主体的
路径（Skill Lab 示例任务集、bootstrap 记忆、实验/灰度持有的在线评估配置、内置与第三方评估器）两者均为 NULL。
它们总由服务端写入：请求体里的 `created_by` / `created_by_name` 会被忽略。

| 资源 | 带这两个字段的路由 |
|---|---|
| 知识库 | `GET` / `POST /api/knowledge-bases`、`GET` / `PATCH /api/knowledge-bases/{kb_id}` |
| 记忆资源 | `GET` / `POST /api/memory/resources`、`GET` / `PUT /{memory_id}`、`POST /{memory_id}/adopt`——被纳管的记忆带纳管者的名字，`created_by` 为 null |
| 数据集 | `GET` / `POST /api/eval/datasets`（以及 `upload`、`from-sessions`、`PUT`）——pipeline 运行新建的数据集记录发起运行的人；架构助手物化的数据集记录计划审批人 |
| 数据处理 pipeline | `GET` / `POST /api/eval/pipelines`、`GET` / `PUT /{id}`、`POST /{id}/run` |
| 评估运行 | `GET` / `POST /api/eval/runs`、`GET /api/eval/runs/{id}`；在线报告行（`GET /api/eval/online/{id}/reports`） |
| 在线评估配置 | `GET` / `POST /api/eval/online`、`GET /api/eval/online/{config_id}` |
| Agent | `GET` / `POST /api/agents`、`GET /api/agents/{id}`、`POST /{id}/convert`（孪生 Agent 由转换者创建）、转换列表、`/api/system-agents` 行、架构助手审批响应（审批人）；发现导入的运行时与预设为 `null`——`owner` 仍只是来源标签 |
| 评估器 | `GET /api/eval/evaluators`、`GET /{id}`、`POST`（创建响应）——架构助手物化的评估器记录计划审批人 |
| 实验 / 灰度 | `GET` / `POST /api/experiments`、`GET /{id}`；`GET` / `POST /api/runtime-canaries`、`GET /{id}` |
| Skill Lab | 任务与任务集的列表 / 详情 / 创建、`POST /jobs/{id}/import-taskset`（由导入者创建该任务集） |

## Workspace 资源可见性 / Workspace resource visibility

管理员按 workspace 决定成员能否看到彼此的资源：

| 方法 | 路径 | 角色 | 请求体 / 响应 |
|---|---|---|---|
| `GET` | `/api/workspaces/{workspace_id}/settings/visibility` | admin | `{"resource_visibility": "shared" \| "isolated"}`——从未设置时为 `shared` |
| `PUT` | `/api/workspaces/{workspace_id}/settings/visibility` | admin | 请求体与响应均为 `{"resource_visibility": "shared" \| "isolated"}`；其他取值 → `422 validation.invalid_request`；成员 → `403 auth.forbidden`；未知 workspace → `404 workspace.not_found` |

两者都作用于路径参数（而非 `X-Workspace`），并且对 bootstrap 尚未完成的 workspace 同样可用。
`GET /api/workspaces` 与管理员 workspace 路由返回的每个 workspace 行都带 `resource_visibility`。
切换在下一次请求生效，不改动任何数据。

在 `isolated` 下，**成员**能看到上表所列种类的一个资源，当且仅当它的 `created_by` 是该成员自己的
`users.id`、为 `null`（创建者未知——旧行、示例、配置 admin、在控制台之外创建的资源），或是一个
**当前**角色为 `admin` 的账号的 `users.id`。管理员看到一切。内置 / 第三方评估器与 bootstrap 记忆
始终可见；云端数据集跟随与之关联的本地数据集（任一可见即可见；没有本地行的为共享），实验 / 灰度持有的
在线评估配置（`exp_<id8>_…` / `can_<id8>_…`）跟随其实验或灰度。

被隐藏的资源在所有地方都不存在，返回与不存在的 id 相同的结果：

| 界面 | 被隐藏时的表现 |
|---|---|
| 列表与选择器 | 不出现——在分页之前过滤，因此 `GET /api/eval/runs` 的 `total` 以及 20 行的实验 / 灰度列表只统计可见行；`GET /api/memory/resources` 会完全去掉被隐藏的托管记忆（不会作为"已发现"列出） |
| 按 id 的路由 | 该种类的 not-found：`kb.not_found`、`memory.not_managed`、`dataset.not_found`（本地与云端）、`pipeline.not_found`、`run.not_found`、`online_eval.not_found`（含 `/results`；被隐藏的报告运行 → `online_eval.report_not_found`）、`aws.not_found`（自定义评估器的 `GET`/`PUT`/`DELETE`）、`experiment.not_found`、`canary.not_found`、`skill_lab.job_not_found`、`skill_lab.taskset_not_found` |
| 创建时的引用 | 与不存在的 id 相同：在被隐藏的数据集上发起运行 → `404 dataset.not_found`，使用被隐藏的评估器 → `422 run.evaluator_not_found`；在线评估配置 / 实验 `gateway` / 灰度中的被隐藏评估器 → `400 {online_eval,experiment,canary}.evaluator_unsupported`；立即评分（`POST /api/observability/sessions/{id}/evaluate`）→ `404 aws.not_found`；pipeline 输出 / from-sessions / 实验或灰度 `traffic` 的数据集 → `404 dataset.not_found`；实验的 `recommend_source_run_id` → `404 experiment.recommend_source_not_found`；灰度的 `source_experiment_id` → `400 canary.source_experiment_invalid`；Skill Lab 任务的任务集 → `404 skill_lab.taskset_not_found` |
| 聚合 | `GET /api/overview` 的 `eval_pass_rate` / `eval_runs` 与 `GET /api/overview/online-quality` 只覆盖可见的运行 / 配置（质量卡片按查看者缓存） |
| 架构助手目录 | 只列出会话所有者能看到的知识库 |

内部引用不受影响：引用了隐藏资源的可见资源（管理员的实验回放某成员的数据集、pipeline 追加到其配置的
数据集）继续工作。

**Agent** 在其 `created_by` 上遵循同一条规则（发现导入的运行时与系统预设的创建者未知，因此共享）：

| 界面 | 被隐藏的 Agent 的表现 |
|---|---|
| 列表与选择器 | 不出现在 `GET /api/agents`（所有控制台选择器）、转换孪生列表，以及（成员创建的密钥调用的）`GET /v1/agents` 中 |
| 按 id 的路由 | `/api/agents/{id}`（以及 `conversions`、`versions`、`identity`、`redeploy`、`inbound-auth`、`convert`、`invoke`、`DELETE`）、`/api/chat/{id}/*` 与 `/v1/agents/{id}/invoke[-stream]` → `404 agent.not_found`；`POST /api/eval/runs`、`POST /api/eval/online`、`GET /api/eval/agents/{id}/log-streams`、`GET /api/experiments/readiness`、`POST /api/experiments` → `400 agent.not_active`；`POST /api/runtime-canaries` → `400 canary.agent_not_active`；`404 registry.a2a_demo_agent`；`live-agent-card` → `404 registry.record_not_deployed`；它的部署作业 → `404 job.not_found` |
| 跨模块变更 | 对可见运行所属的隐藏 Agent 接受推荐，以及除 `verdict` / `cleanup` 之外的每个实验 / 灰度动作 → `404 agent.not_found`；这类运行的 `recommendation-inputs` 读作 `source: "manual"`（不透露隐藏 Agent 的提示词 / 工具） |
| Agent spec 固定项 | 创建 / 转换：固定的每个知识库 / 记忆都必须可见（`404 kb.not_found`、`422 agent.memory_not_managed`）；重新发布 / inbound-auth 只检查相对已存 spec **新增**的固定项 |
| 注册表 | 它的 A2A 记录从 `GET /api/registry/records`、`/search`、`/discoverable` 中去掉；对其 `GET` / `PUT` / `DELETE` / `action` / `reimport` → `404 aws.not_found` |
| 可观测性 | 它的 trace / 会话从列表中去掉；trace / 会话详情、`transcript` 与 `evaluate` → `404 observability.{trace,session}_not_found`；`/dashboard` 仍是 workspace 合计 |
| 记忆控制台 | 它的 actor（`<agent>__<human>`）从 `/api/memory/actors` 与抽取任务中去掉；以该 actor 或其命名空间为键的 `sessions` / `events` / `namespaces` / `records` / `records/search` → `404 memory.actor_not_found`；共享的 `default` actor 会去掉账本归给它的会话 |
| 派生名称 | 从知识库 `attached_agents`（`attached_agent_count` 保留真实数量）与 `kb.has_attached_agents` 409、记忆资源 `agents` 与 `memory.in_use` 409、Connections `referenced_by`（含 `identity.connection_referenced` 409）、在线评估 `matched_agent` 中去掉；发现结果 `managed_agent_id` / `managed_agent_name` 与导入结果 `already_managed` 读作 `null`，并带 `managed: true` |
| 聚合 | `GET /api/overview` 的 `active_sessions` 与 A2A `registry_assets.agents` 只统计可见的 Agent |
| 名称唯一性 | 仍在整个 workspace 内生效；占用者被隐藏时，`409 agent.name_exists` 的 `detail.agent_id` 为 `null` |

知识库强制删除仍会从每个挂载它的 Agent 上移除该知识库。

## 控制台知识库 API / Console Knowledge Bases API

`/api/knowledge-bases/*` 支撑知识库控制台（控制台 04），底层是 Bedrock 的*托管*知识库
——控制面走 `bedrock-agent`，检索走 `bedrock-agent-runtime`。只有
`type == "MANAGED"` 的知识库可寻址：同一账号内的 VECTOR 知识库会返回
`kb.not_found`。本地只存控制台所建知识库的创建者（见下文「资源创建者」），其余都是一次实时 AWS 调用。详见
[architecture.zh-CN.md](architecture.zh-CN.md)「托管知识库」一节。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/knowledge-bases?status=` | 全部 MANAGED 知识库，字段为 `{kb_id, name, description, status, updated_at, data_source_count, attached_agents}`；`status` 是读取后再施加的可选精确匹配过滤（例如 `ACTIVE`） |
| `POST` | `/api/knowledge-bases` | `202`——`CreateKnowledgeBase`（`{name, description?, source: {mode: "upload"\|"existing", bucket?, prefix?}}`）在知识库仍处于 `CREATING` 时就返回详情，并附 `source_pending`；数据源由后端线程在知识库变为 `ACTIVE` 之后（1.5–3 分钟）在请求之外创建，因此客户端轮询 `GET /{kb_id}` |
| `GET` | `/api/knowledge-bases/{kb_id}` | 详情：状态、ARN、时间戳、`failure_reasons`、`attached_agents`，以及每个数据源的桶/前缀、状态与最近 10 个 ingestion 作业 |
| `PATCH` | `/api/knowledge-bases/{kb_id}` | `{description}`（≤1000 字符）→ `UpdateKnowledgeBase`，名称、角色与配置原样读回后回传；响应是刷新后的详情 |
| `DELETE` | `/api/knowledge-bases/{kb_id}?force=` | 依次删除数据源、按知识库的网关 `Retrieve` 目标与按知识库的内联 S3 策略，最后 `DeleteKnowledgeBase`。仍有 Agent 挂载时返回 `409 kb.has_attached_agents`；`force=true` 会先把它从每个挂载它的 Agent spec 里摘掉（并重新同步 harness 类 Agent 的 agentic 目标） |
| `POST` | `/api/knowledge-bases/{kb_id}/files` | `multipart/form-data`，一个或多个名为 `files`（或 `file`）的部件 → artifacts 桶 `kb/{kb_id}/` 下的 `{keys}`。数据源尚不存在时也允许上传；数据源全在别处的知识库返回 `409 kb.no_upload_target` |
| `POST` | `/api/knowledge-bases/{kb_id}/data-sources` | `201`——用同样的 `{mode, bucket?, prefix?}` 请求体创建 `MANAGED_KNOWLEDGE_BASE_CONNECTOR` 数据源，并返回刷新后的详情。按 S3 位置幂等：同一桶/前缀上已有连接器时直接返回它，而不是再建一个。这同时也是「知识库没有数据源」时的手动补建入口 |
| `DELETE` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}` | `DeleteDataSource` → `{deleted, ds_id}`（AWS 侧为异步删除） |
| `POST` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync` | `StartIngestionJob` → 作业投影 `{job_id, status, started_at, updated_at, statistics, failure_reasons}` |
| `GET` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs` | 最近 50 个 ingestion 作业，最新优先，投影同上 |
| `GET` | `/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents?page_size=&token=` | `ListKnowledgeBaseDocuments` 的一页（`page_size` 1–100，默认 50），形如 `{documents, next_token, page_size}`；每个文档带知识库侧的 `status`/`status_reason`/`indexed_at`，以及按对象 key 联结进来的 S3 侧 `size_bytes`/`uploaded_at`（后端无权列举该桶时为空） |
| `POST` | `/api/knowledge-bases/{kb_id}/query` | 检索 Playground——`{text, number_of_results?}`（1–100，默认 8）→ 带 `managedSearchConfiguration` 的 `Retrieve`，响应 `{results}`，元素为 `{text, score, location_uri, metadata}` |
| `POST` | `/api/knowledge-bases/ensure-gateway` | 按名字「不存在才创建」共享的 `launchpad-kb-gw` MCP 网关，并把 `{id, arn, url}` 持久化到工作区。幂等；harness 部署路径调用的是同一个 helper，因此这条路由只用于提前预置网关 |

错误码：`kb.not_found`（404——未知 id，或该知识库不是 MANAGED）、`kb.ds_not_found`
（404）、`kb.has_attached_agents`（409，阻塞的 Agent 名在 `detail.agents` 里）、
`kb.delete_conflict`（409——知识库仍在 `CREATING`）、`kb.no_upload_target`（409）、
`kb.no_files`（400——表单里没有上传部件）、`kb.sync_not_ready`（409——
`StartIngestionJob` 抛出 `ValidationException` 或 `ConflictException`：数据源仍在预置，
或已有同步在跑）、`kb.bucket_required` / `kb.invalid_bucket` / `kb.invalid_prefix` /
`kb.invalid_source`（400——数据源校验）、`kb.query_failed`（502——知识库侧检索失败，
例如索引仍在构建）。其余任何 AWS `ClientError` 都走上文的全局映射（`aws.validation`、
`aws.conflict` 等）。资源映射里没有 `kb_role_arn`（创建）或没有 `artifacts_bucket`
（上传）的工作区尚未完成引导，会返回点明缺失键的 `500`。

## 控制台 Memory API / Console Memory API

`/api/memory/*` 支撑只读的 Memory 控制台（控制台 05），底层是共享的 `launchpad_memory` 单例。
控制台的每条路由都是读操作：没有任何接口会写事件、删记录或触发抽取。
唯一会写的一组接口是下面的 `/api/memory/resources*`——管理记忆*资源*本身，位于独立的路由模块
（`routers/memory_resources.py`）。详见
[architecture.zh-CN.md](architecture.zh-CN.md)「Memory 控制台」一节。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/memory/overview` | 资源配置、长期策略、有界的 actor 计数、同级记忆 |
| `GET` | `/api/memory/actors` | actor 列表，复合 id `<agent_id>__<human>` 已解码并解析出 Agent 名称 |
| `GET` | `/api/memory/sessions?actor_id=` | 单个 actor 的会话；由控制台写入的会话会关联到 ChatSession 台账 |
| `GET` | `/api/memory/events?actor_id=&session_id=` | 短期事件；每条载荷的 `kind` 为 `conversational`（角色 + 全文）、`json`（JSON 值无损序列化到 `text`，含 `null`、`false`、`0` 与 `""`）或 `blob`（只带字节数）；未知类型省略 |
| `GET` | `/api/memory/namespaces?actor_id=` | 已替换 `{actorId}` 的策略命名空间模板；尾部的 `{sessionId}` 段折叠为 actor 级前缀（`prefix: true`），其他位置的占位符则产生 `resolvable: false` |
| `GET` | `/api/memory/records?actor_id=&strategy_id=` 或 `?namespace=` | 解析所得命名空间下的长期记录 |
| `POST` | `/api/memory/records/search` | 语义检索（`{query, actor_id, strategy_id?, namespace?, top_k}`），带相关性分数 |
| `GET` | `/api/memory/extraction-jobs` | 失败（可重试）的抽取作业，可按 `actor_id`/`session_id`/`strategy_id`/`status` 过滤——**控制台未展示**；AWS 的 `status` 枚举只有 `FAILED`，因此健康的资源返回空列表 |

记忆资源管理（`?view=resources`）：

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/memory/resources` | 工作区账号/区域内的全部记忆,默认记忆排首位,每条附带 spec 绑定了它的在线 Agent 与 `managed`(见下文) |
| `POST` | `/api/memory/resources` | `perm:memory.manage`。`CreateMemory`(`{name, description?, event_expiry_days?, strategies?, namespace_keys?}`)→ `201`,返回 `CREATING` 状态的详情投影;新 id 登记为已纳管 |
| `GET` | `/api/memory/resources/{memory_id}` | 详情投影:描述、状态、事件过期、执行角色、策略、命名空间键 |
| `POST` | `/api/memory/resources/{memory_id}/adopt` | **admin**。将账号内已有的记忆登记为本工作区纳管(先经 `GetMemory` 确认存在——未知 id → `404 aws.not_found`);幂等;响应为详情投影 |
| `PUT` | `/api/memory/resources/{memory_id}` | `perm:memory.manage`。仅限 `{description?, event_expiry_days?}` 的 `UpdateMemory`——至少提供一项(否则 422),`description` 1–4096 字符(只能替换、不能清空),`event_expiry_days` 7–365(越界 422)。只发送 `memoryId` 加给出的字段,绝不发送 `namespaceKeys`(API 会整体替换该集合);响应是用 `GetMemory` 读回的详情投影。不会因被 Agent 引用或是平台默认而被阻止;未知 id → `404 aws.not_found` |
| `DELETE` | `/api/memory/resources/{memory_id}` | `perm:memory.manage`。`DeleteMemory`(不可逆);工作区默认记忆返回 `409 memory.platform_protected`,仍被在线 Agent 的 spec 绑定时返回 `409 memory.in_use`(附 Agent 列表);同时删除纳管登记 |

**归属。** 账号内可能存在平台从未创建的记忆,因此只有工作区的 bootstrap 记忆,或 `managed_memories` 账本行登记的记忆(由上面的 `POST` 或管理员纳管写入——从不取自客户端请求)才算*已纳管*。其余 id 在任何 AWS 调用之前,所有按 id 的路由都返回 `404 memory.not_managed`;列表仍以 `managed: false` 展示它们。spec 的 `memory.memory_id` 必须已纳管且为 `ACTIVE`:`POST /api/agents`、重新发布与转换会以 `422 agent.memory_not_managed` / `409 agent.memory_not_active` 拒绝,部署作业在任何阶段之前再次校验。对绑定了未纳管 id 的旧 spec,Chat 记忆侧栏返回 `409 agent.memory_not_managed`。

每条列表路由都接受并返回 `next_token`（AWS 按 100 条分页），并接受 `max_results`（上限 100）——
不会有任何静默截断。`/records` 与 `/records/search` 的命名空间解析顺序：显式的 `namespace` 优先，
否则由 `actor_id`（+ 可选的 `strategy_id`）推导。

错误码：`memory.not_configured`（409，尚未运行 bootstrap——`/overview` 例外，它改为返回
`{"configured": false, …}`，以便页面渲染初始化状态）、`memory.namespace_required`（400，无法推导出
命名空间）、`memory.unavailable`（502，底层 AWS 调用失败）。

## 控制台 Chat API / Console Chat API

`/api/chat/*` 支撑 Chat 交互页面，与 `/v1` 共用同一条调用链（`app.services.invoke`）。
这里的会话就是 AgentCore Runtime 会话：控制台作为 `runtimeSessionId` 发出的 id，正是台账所记录的 id。

| 方法 | 路径 | 结果 |
|---|---|---|
| `POST` | `/api/chat/{agent_id}` | 一轮对话，SSE 形式（`meta` → `delta`/`tool`/`auth_required`/`policy_denied`/`error` → `done`）；`{prompt, session_id?, as_user?, retry_of?}`（`retry_of` 见下文“重试失败的轮次”），不带 id 即开启新会话。`as_user`（仅 JWT 入站智能体）：`true` 发送登录用户的 Cognito JWT，`false` 使用工作区 M2M 令牌，省略时有用户池登录则用用户 JWT，否则用 M2M；未经用户池登录却传 `true` 返回 `409 chat.as_user_unavailable`。`meta.inbound = {mode: jwt, caller: user_jwt\|m2m}`；两种情况下 Memory actor 都是 `scoped_actor`。`auth_required` `{provider, tool, scopes[], url, agent_id}` 是工具发起的 as_user（3LO）授权请求：`url` 为一次性授权 URL（控制台仅在其为 `https:` 时显示链接），`sessionUri` 留在服务端，回答会在其前后继续流式输出。历史中该请求保存为不含 URL 的 `role: "auth"` 行（`text` 为凭证名，`name` 为工具），因此恢复后只能重试；轮询 `GET /api/identity/grants/{connection}/status` 可得知授权何时完成。`policy_denied`（Harness；字段见“流式调用”）保存为 `role: "policy"` 行（`text` 为原因，`name` 为工具），恢复后显示为 V2 Chat 的策略拦截卡片，此时链接指向治理首页而非某个网关。仅控制台的状态事件（chat-streaming S2；公开 `/v1` 流与分享页从不发送）：`meta.started_at` 与 `tool.started_at`（epoch 毫秒）、`phase {phase: starting\|waiting_model\|streaming}`、`thinking {}`（Harness 正在推理；有节流，从不含推理文本）、`tool_end {id, status: success\|error\|denied, duration_ms}`（代码运行时未结束的工具由下一个事件结束）、每段静默期发送的具名 `heartbeat {elapsed_ms}`（`/v1` 仍是 `: keep-alive` 注释），以及运行时报告用量时在末尾发送一次的 `usage {input_tokens, output_tokens, total_tokens, model_calls}` |
| `POST` | `/api/agents/{agent_id}/inbound-auth` | `agent.deploy`——`202 {agent, job_id, deployment_id}`。请求体 `{inbound_auth: {mode: iam\|jwt, jwt?} \| null}`（`null` 取消固定，改为继承工作区默认值）。以替换后的固定值重新发布已存规格：对同一 Runtime 执行 UpdateAgentRuntime，产生新版本。`422 agent.inbound_auth_unsupported`（harness／A2A 不支持 JWT）、`422 agent.inbound_auth_invalid`、`409 agent.deploy_in_progress` |
| `GET` | `/api/identity/inbound-auth/default` | 成员——`{workspace_id, default: {mode: iam\|jwt, jwt?}, configured, cognito, cognito_issuer}`；`configured=false` 表示隐式 IAM；`cognito` 是面向工作区用户池的现成 JWT 配置，引导前为 null；`cognito_issuer` 是该用户池的 issuer（控制台、`/v1` 与评估所出示令牌的 issuer），无用户池时为 null |
| `PUT` | `/api/identity/inbound-auth/default` | `identity.manage`——请求体 `{mode, jwt?: {discovery_url, allowed_clients[], allowed_audience[], allowed_scopes[], custom_claims[], source_connection?}}` → 同 GET 形状。`source_connection` 仅用于展示，不进入授权器配置。持久化前先探测发现文档（`422 identity.discovery_unreachable`／`identity.discovery_invalid`）。已部署的智能体在重新部署前保持原授权器 |
| `GET` | `/api/identity/connections/oidc-sources` | 成员——`{sources[{name, vendor, discovery_url, issuer, derived_from: discovery_url\|issuer}]}`：存储的 `oauthDiscovery` 可推导出 OIDC 发现 URL 的 OAuth2 凭证（入站 JWT 表单的“从凭证选择”）。不含系统凭证与 GitHub，从不返回凭证的 client id |
| `POST` | `/api/identity/connections/oauth2` | `identity.manage`——可选 `obo{grant_type: TOKEN_EXCHANGE\|JWT_AUTHORIZATION_GRANT, actor_token_content?: NONE\|M2M, actor_token_scopes?[]}` 设置 `onBehalfOfTokenExchangeConfig`：仅限 CustomOauth2（`422 identity.obo_vendor_unsupported`），Cognito 或发现文档未声明该授权类型的 IdP 返回 `422 identity.obo_unsupported`，`obo_invalid` 表示字段非法 |
| `POST` | `/api/identity/gateway-targets` | `identity.manage`——`mode: obo` 设置 `grantType: TOKEN_EXCHANGE`，需要 CUSTOM_JWT 网关（`409 identity.obo_needs_jwt_gateway`）与已配置 OBO 的凭证（`422 identity.obo_unsupported`）。凭证的 issuer 与网关 JWT 授权器的 issuer 不同时仍会创建目标，响应的 `warnings` 带有 `identity.obo_issuer_mismatch` `{connection, connection_issuer, gateway_issuer}`：该 IdP 必须信任网关的入站 issuer。完整说明见英文版 |
| `POST` | `/api/identity/oauth/complete` | `identity.grant`——as_user（3LO）绑定环节，由 `/auth/return` 页面调用。请求体 `{session_id}`（AgentCore Identity 附加到返回 URL 上的 `session_id`，一次性的不透明凭据）→ 为该会话记录的用户执行 `CompleteResourceTokenAuth` 后返回 `{completed: true, provider, agent_id, agent_name, tool}`；调用者必须就是该用户。`404 identity.session_unknown`（从未签发、已用过或已被撤销清除）、`409 identity.session_expired`（超过 15 分钟）、`403 identity.session_user_mismatch`、`409 identity.session_token_unavailable`、`409 identity.as_user_requires_user_jwt`、`502 identity.session_completion_failed`。见 [identity.md §7.4](identity.md#74-routes-permission-and-errors) |
| `GET` | `/api/identity/grants` | 成员——调用者**自己**的 as_user 授权，绝不返回其他成员的：`{grants[{connection, agent_id, agent_name, tool, scopes[], status: pending\|authorized\|revoked, force_reauth, created_at, updated_at, authorized_at, revoked_at}]}`，按时间倒序（我的授权） |
| `GET` | `/api/identity/grants/{connection}/status?agent_id=` | 成员——`{connection, agent_id, status: none\|pending\|authorized\|revoked, force_reauth, authorized_at}`；Chat 授权卡片轮询此接口直到授权完成（记录到请求前为 `none`）。只有 `authorized` 且 `force_reauth: false` 时授权才可用 |
| `DELETE` | `/api/identity/grants/{connection}` | `identity.grant`——撤销（强制重新授权）：`{revoked: true, provider, agents}`；之后经此凭证的调用会带上 `forceAuthentication=true` 并再次请求授权，只有新的授权完成后撤销才解除 |
| `GET` | `/api/identity/consent-portal` | 成员——`{gateway_id, portal: {id, name, status, status_reason, portal_url, execution_role_arn, connection, scopes[], audience, created_at, …} \| null}`，每次都从 AWS 读回工作区网关的 Consent Portal（无网关时 `gateway_id: null`） |
| `POST` | `/api/identity/consent-portal` | **管理员**——`202 {gateway_id, portal}`。请求体 `{name, description?, connection, scopes[]（默认 [openid]）, audience?, execution_role_arn}`：为 as_user 网关目标创建门户（`connection` 为入站 IdP，与网关 JWT 授权器同一 issuer；执行角色由运维提供）。不可授予成员：`identity.manage` 不覆盖此接口。`409 identity.gateway_missing`／`identity.consent_portal_exists`，`422 identity.invalid_portal_name`／`identity.invalid_role_arn`／`identity.role_account_mismatch`／`identity.portal_scopes`；见 [identity.md §7.6](identity.md#76-consent-portal-as_user-gateway-targets) |
| `DELETE` | `/api/identity/consent-portal` | **管理员**——`{deleted: true}`；`404 identity.consent_portal_not_found` |
| `GET` | `/api/chat/{agent_id}/turns/{turn_id}/events?after=` | 恢复跟随一个仍在运行的控制台轮次（见下文“断线不丢的轮次”）：先返回 SSE id `after` 之后的事件（`0` = 整轮），再返回实时事件，轮次结束后关闭。若其中部分事件已离开缓冲区，先发一个 `gap {after, first_seq}`。轮次已不在本进程中，或属于其他 Agent、Workspace 或项目时返回 `404 chat.turn_not_found`；此时 `GET …/history` 中是台账已保存的内容 |
| `POST` | `/api/chat/{agent_id}/turns/{turn_id}/stop` | 停止按钮：结束一个运行中的轮次。上游被关闭，已生成的部分回答与一条 `interrupted: stopped by the user` 行会被保存。工作线程记录停止后（最多约 5 秒）返回 `{turn_id, stopped}`，因此该会话的下一轮会被立即接受。轮次已结束时 `stopped: false` |
| `GET` | `/api/chat/{agent_id}/sessions` | 该 agent 可回放的会话：`{session_id, actor_id, turns, last_at, ended_at, preview}`——`ended_at` 在控制台显式结束 runtime 会话后写入，仍存活或只是空闲时为 `null` |
| `GET` | `/api/chat/{agent_id}/history?session_id=` | 某会话已渲染的对话条目，按回放顺序 |
| `POST` | `/api/chat/{agent_id}/sessions/{session_id}/stop` | **结束会话**——数据面 `StopRuntimeSession(agentRuntimeArn, runtimeSessionId)` → `{session_id, ended: true, already_ended, ended_at}`。AWS 回 `ResourceNotFoundException`（会话早已结束或因空闲过期）时 `already_ended: true`，视为成功而非错误。台账行保留（历史仍可回放）并打上 `ended_at`；之后若在同一 id 下再发一轮，会开启新的 runtime 会话并清掉该标记。只有 runtime 支撑的 agent 才可结束（`zip_runtime`、`studio`、`container`、已发现的 runtime）；托管 Harness——无论自建还是导入——没有结束会话的操作，返回 409 `chat.session_stop_unsupported`，`detail.reason_code` 为 `harness`。其他 agent 或其他 workspace 的会话返回 404 `chat.session_not_found`。撑过 botocore 重试仍然出现的 `RetryableConflictException` 映射为 409 `aws.conflict` |

**断线不丢的轮次。** 控制台的一轮对话运行在后台工作线程上，而不在 HTTP 响应里（`app/services/chat_turns.py`，chat-streaming §5.2）。工作线程读取上游、持久化对话条目，并把每个事件按 `seq` 编号发布到该轮的内存缓冲区。`POST /api/chat/{agent_id}` 的响应只是这个缓冲区的第一个订阅者；浏览器离开后，轮次继续运行并继续保存。

- `meta.turn_id` 标识该轮。除心跳外的每一帧都以 SSE `id:` 行携带其 `seq`，`…/turns/{turn_id}/events?after=` 就从这里接着发送。控制台在连接断开后用它重连；停止按钮除关闭流外，还会调用 `…/turns/{turn_id}/stop`。
- `GET …/history` 增加 `running_turn {turn_id, user_message_id} | null`：重新打开的会话显示到该用户行为止，并从第一个事件起跟随运行中的轮次。「结束会话」会先停止该会话中运行的轮次。
- 每个会话同一时间只运行一轮：会话的上一轮仍在运行时再发新的一轮，会在流打开前返回 `409 chat.turn_in_progress`（`detail.turn_id`、`detail.session_id`）。
- 一轮最多运行 Agent 的 `timeout_seconds` + 60 秒（规格未设置时为 3600 + 60 秒）。超过后上游被关闭，流以 `error` `chat.turn_timeout` 结束（`detail.limit_s`，`retry.category: timeout`，不提供重试按钮）；被停止按钮结束的轮次以 `chat.turn_stopped` 结束。
- 缓冲区最多保留 2000 个事件或 600 秒内的事件；已结束的轮次在 60 秒内仍可恢复，之后只剩台账。
- 单进程（v1）：轮次登记表就是服务进程的内存。多工作进程部署必须把恢复请求路由到运行该轮的进程；重启会丢失运行中的轮次，其部分回答在台账中保留到最后一次写入的条目。

`/v1` 与分享页不变：它们的流仍随连接结束。

结束是显式动作：控制台的「新会话」只在本地忘掉 id，留下的 runtime 会话会自行空闲过期。
重新发布之后应当按「结束会话」——AgentCore 会把存活的会话钉在首次服务它的版本上，
验证新版本需要一个全新的会话。

**重试失败的轮次。** 控制台流携带重试所需的台账 id，`/v1` 与分享页不发送这些字段。

| 事件 | 新增成员 |
|---|---|
| `meta` | `user_message_id`：本轮用户行的台账 id，即重试时要指定的 id |
| `error` | `message_id`：已持久化的错误行的台账 id。`transient: bool`：评估运行器的瞬时错误判定是否识别该失败（限流、5xx、连接断开）。`retry: {category: timeout\|connection\|busy\|config\|other, manual, auto, timeout_s?}`：控制台如何提供重试 |

`category` 取以下值：

- `timeout`：`harness.execution_timeout`，Agent 用尽了全部时间预算。Agent 设置了 `timeout_seconds` 时，`timeout_s` 为该值。
- `connection`：到 runtime 的传输断开或超时。
- `busy`：其他瞬时失败。
- `config`：其他具名错误，例如权限、参数、限额或策略。
- `other`：其余情况。

`manual`（显示重试按钮）在 `connection`、`busy`、`other` 时为 true；`timeout` 与
`config` 重放也无法修复，因此从不为 true。`auto`（不询问、自动重试一次）仅在
`connection` 或 `busy` 且本轮尚未流出 `delta`、`tool`、`auth_required` 或
`policy_denied` 时为 true，这样重放不会重复用户已看到的内容或工具副作用。

重试方法：以同一 `session_id` 发送，并把 `retry_of` 设为失败轮次的任一台账 id，即其
`user_message_id` 或错误事件的 `message_id`。该轮必须是会话的最新一轮，且失败或没有任何回答。
新的用户行记录 `retry_of`。失败的轮次保留在台账中，但 `GET …/history` 不再回放它，因此恢复的
会话中该消息只出现一次。若某个中断的流所在轮次已被重试取代，则不再写入 `interrupted` 行。
以下拒绝发生在流打开之前：

- `404 chat.message_not_found`：该 id 不存在、属于其他会话、Agent、Workspace 或项目，或缺少 `session_id`。
- `409 chat.retry_not_allowed`（`detail.retry_of`）：该轮不是最新一轮，或已经正常回答而没有错误。

## 控制台 Agent API——BYOC 上传 / Console Agents API: BYOC uploads

`byoc` 创建方式部署成员自己编写的代码。两种 zip 构件类型（`code_zip`、
`container_source`）先在此暂存归档；返回的 `upload_id` 填入创建请求体的
`spec.byoc`。

| Method | Path | Result |
|---|---|---|
| `POST` | `/api/agents/uploads?python_version=PYTHON_3_13` | `perm:agents.deploy`——`multipart/form-data`，单个名为 `file` 的部件，仅限 `.zip`，≤250 MiB（解压后 ≤750 MiB、条目 ≤2 万；zip-slip/绝对路径/符号链接会被拒绝）。存入制品桶 `byoc/{workspace_id}/{upload_id}/source.zip` + `manifest.json` → `201` `{upload_id, sha256, size_bytes, original_filename, uploaded_by, uploaded_at, entries_count, uncompressed_bytes, detected: {entrypoint_candidates[], has_requirements, has_dockerfile, agentcore_sdk_detected, adot_detected, adot_source, requirements: {status: ok\|failed\|skipped, package_count, error}}}`——`requirements` 是对 zip 内 requirements.txt 针对部署目标（linux/aarch64 + 可选 `python_version`，默认 PYTHON_3_13）的上传期干跑解析；`failed` 表示部署的 package 阶段会以同样方式失败，`skipped`（无 requirements.txt、解析超时约 90 秒、`uv` 不可用）不代表任何结论 |
| `GET` | `/api/agents/uploads/{upload_id}` | member——已存储的清单（同一形状）；其他工作区的 upload_id 返回 404 |

错误码：`byoc.invalid_upload`（400，缺少部件/非 zip/空文件）、
`byoc.invalid_python_version`（422）、
`byoc.upload_too_large` / `byoc.upload_request_too_large`（413）、
`byoc.zip_invalid`、`byoc.zip_empty`、`byoc.zip_entry_unsafe`、
`byoc.zip_too_many_entries`、`byoc.zip_uncompressed_too_large`（422）、
`byoc.upload_not_found`（404）。

zip 内的 `requirements.txt` 按 pip 文件格式解析（反斜杠续行、行内注释、环境标记
均被支持）；`--hash=` 选项会被丢弃——平台会针对自己的部署目标重新锁定并生成新的
hash。以下内容会被明确报错拒绝：`-r`/`-c` 引用、`-e`/可编辑安装、本地路径、直接
URL/VCS 条目、索引选项（`--index-url`/`--extra-index-url`/`--find-links`——平台
只从自己的索引安装），以及超过 500 条的清单。

`POST /api/agents` 使用 `method: "byoc"` 时携带 `spec.byoc`：
`{artifact_kind: code_zip|container_source|container_image, upload_id?,
image_uri?, entrypoint?（code_zip，默认 main.py）, python_version?
（PYTHON_3_10…PYTHON_3_13，默认 PYTHON_3_13）, install_requirements?（默认
true）, invoke_contract?（launchpad_prompt|raw）, allowed_models?（1–20 个不重复的
Bedrock 基础模型或推理配置文件 ID）}`——zip 类型必须提供
`upload_id`，`container_image` 必须提供本工作区账户+区域内的私有 ECR
`image_uri`。该方式的 `system_prompt` 可选（作为描述使用）；v1 拒绝
tools/toolkits/skills/knowledge_bases 与 `a2a` 协议。部署时服务端会把
`spec.byoc.provenance` 写入 spec（来自上传清单）。

**允许的模型。** 按 Agent 的执行角色把 `bedrock:InvokeModel` 精确限定到
`byoc.allowed_models` 这些模型，用户代码调用其他模型会在运行时收到
`AccessDeniedException`。第 `[0]` 个条目是**主模型**，必须等于 `spec.model_id`：
只发送 `allowed_models` 时服务端把 `model_id` 设为第一个条目；两者都发送时
`model_id` 必须在列表中（会被移到最前）。没有 `allowed_models` 的 spec——包括该
字段出现之前写入的所有行——按 `[spec.model_id]` 处理。重新发布会重写角色策略，
因此编辑后的列表在下次部署生效。部署器把主模型 ID 以环境变量 `MODEL_ID`、完整
列表以环境变量 `ALLOWED_MODEL_IDS`（逗号分隔，主模型在前）传入运行时——两个变量
只要 `spec.env` 已自行设置就以用户值优先。

## 控制台 Agent API——版本与端点 / Console Agents API

`GET /api/agents/{agent_id}/versions` 是 Agent 详情「版本与端点」面板背后的只读 AWS 视图。它对该 Agent
所属资源族的两个列表操作跟随每一页 `nextToken`,并返回白名单投影——不含环境变量、制品位置、执行角色或
鉴权配置。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/agents/{agent_id}/versions` | `{kind: runtime\|harness, resource_id, versions[{version, status, description, last_updated_at}], endpoints[{name, live_version, target_version, status, description, created_at, last_updated_at, failure_reason}], latest_version, ledger_version, canary_endpoints[]}`——`versions` 最新在前;`endpoints` 先 `DEFAULT` 再按名称;`latest_version` 是 AWS 报告的最高版本,`ledger_version` 是最近一次 Launchpad 部署记录的版本(`Agent.version`),带外更新或金丝雀候选版本铸造后二者可能不同;`canary_endpoints` 列出仍然存在的 `stable`/`treatment` 端点名。资源族:`zip_runtime`/`studio`/`container` 以及 `spec.discovery.resource_type` 缺省或为 `runtime` 的导入行 → `ListAgentRuntimeVersions` + `ListAgentRuntimeEndpoints`;`harness` 以及 `resource_type == "harness"` 的导入行 → `ListHarnessVersions` + `ListHarnessEndpoints`(harness 版本没有描述字段)。不改变任何状态 |
| `GET` | `/api/agents/{agent_id}/mcp` | member——仅限 MCP 服务器（`spec.protocol == "mcp"`），否则 `409 agent.not_mcp_server`：`{target_name, target_id, client_id, gateway_id, endpoint, target_status, status_reasons[], tools[{name, description}], tools_error}`——实时读回网关目标（AWS 上已不存在时为 `MISSING`），以及智能体经网关看到的工具（`<target>___<tool>`）；不返回任何密钥。另带工具审批闸门 `review {state: none\|reviewing\|approved\|rejected, open_review_id, approved_review_id, approved_hash, live_hash, drift}`（见下文“控制台 MCP 工具审批 API”）：`live_hash` 是实时列出的工具清单的哈希（目标不是 `READY` 时为 null），与 `approved_hash` 不同时 `drift` 为 true。此读取不记录任何东西，记录由 `POST /api/mcp-servers/{agent_id}/reviews/check` 完成 |
| `GET` | `/api/agents/{agent_id}/conversions` | 成员——`{source: {id, name, method, status}, conversions: [agent…]}`：由该 Agent 转换出的 **Runtime 孪生**（`POST …/convert` 会在新的 `-rt` Agent 上标记 `spec.source_harness.agent_id`），最新在前，每行是普通的 Agent 投影加上它最近一次的 `deployment`。纯账本读取（不调用 AWS）；已删除的孪生不列出；来源不存在或已删除 → 404 `agent.not_found`。助手的 NEXT STEPS 用它在孪生 `active` 后切换目标，并在刷新页面后重新找到孪生 |

错误码:`agent.not_found`(404,未知 id 或其他 workspace 的 Agent)、`agent.no_resource`(409,该行没有可查询的
AWS 资源——部署仍在进行、首次部署失败、已删除,或既非 Runtime 也非 Harness 的形态;`message` 即面板展示的
人类可读原因)。AWS `ClientError` 映射为标准 4xx 信封。

## 控制台 Agent API——遥测状态 / Console Agents API: telemetry status

`GET /api/agents/{agent_id}/telemetry` 支撑 Agent 详情页的“工具与能力”卡片：该 Agent 的生产端点是否写入了
span，以及最近一次在多久之前。未带 ADOT 启动器部署的 BYOC 代码 zip 不产生任何 span，因此不会出现在可观测页面，
也无法评估；这个读取让该问题变得可见。它复用评估探测（评估解析器推导出的服务名，再对 `aws/spans` 做
`FilterLogEvents` 扫描），不向 AWS 或台账写入任何内容。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/agents/{agent_id}/telemetry?refresh=` | `perm:project.use`——`{agent_id, endpoint, service_name, status: receiving\|none\|unknown\|unsupported\|not_deployed, last_span_within: 1h\|24h\|7d\|14d\|null, lookback_days, reason, byoc_no_adot, checked_at, cached}`。`endpoint` 是生产流量使用的端点（受门控的 Agent 为 `live`，否则为 `DEFAULT`）；`service_name` 为 `<runtimeName>.<endpoint>`（无法解析时为 null）。时间窗口从最窄开始探测，`last_span_within` 是第一个含有 span 的窗口；`lookback_days` 是最宽窗口（14，即评估预检的回看期）。`byoc_no_adot` 在 `byoc` `code_zip` 且 `spec.byoc.provenance.adot_launcher` 未设置时为 true。按 Agent、资源 id、版本与端点缓存 5 分钟（`unknown` 结果只缓存 30 秒）；`refresh=true` 绕过缓存重新探测。`cached` 表示本次结果是否来自缓存 |

`status` 与 `reason`：

| `status` | 含义 | `reason` |
|---|---|---|
| `receiving` | 在 `last_span_within` 内收到过 span | `null` |
| `none` | 最近 `lookback_days` 天内没有 span | `byoc_no_adot` 时为 `byoc_code_zip_no_adot`；从未在该端点调用过的 Harness 为 `no_log_group`；否则为 `null` |
| `unknown` | 探测无法判断（繁忙日志组触及分页上限、CloudWatch 拒绝读取、服务名无法解析） | `probe_failed`，或解析器的错误码 |
| `unsupported` | 该创建方式没有可探测的 span 遥测（导入的 Harness，或不在评估支持集合 `zip_runtime`/`studio`/`container`/`harness`/`byoc` 内的方式） | `method_unsupported` |
| `not_deployed` | 该行还没有 AWS 资源 | `not_deployed` |

错误码：`agent.not_found`（404，未知 id，或属于其他 Workspace、其他项目、对调用者隐藏的 Agent）。探测失败从不让请求失败，
而是返回 `status: unknown`。

## 控制台评估数据集 API / Console Evaluation Datasets API

`/api/eval/datasets` 保存本地 scenario 数据集(SQLite,可编辑的事实来源)及其各自对应的一个 AWS Dataset。AWS 数据集由一份**草稿(DRAFT)**加若干不可变的编号**版本**组成:「同步 AWS」首次创建数据集,之后原地替换草稿中的示例;「发布版本」把草稿快照为版本。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/eval/datasets` | `{datasets[]}`——本地行,含 `items`、`kind`、`has_ground_truth` 与 `cloud` blob |
| `POST` | `/api/eval/datasets` | 由条目创建(devguide scenario、模拟 persona 或旧式 prompt;kind 自动推断)→ 201 |
| `PUT` · `DELETE` | `/api/eval/datasets/{dataset_id}` | 编辑(kind 不可变 → 400 `dataset.kind_immutable`)/ 删除本地行;已同步的 AWS 副本保留 |
| `POST` | `/api/eval/datasets/{dataset_id}/sync-to-aws` | 没有存活云端副本时:`CreateDataset`(内联示例)并轮询到 `ACTIVE`。有副本时:**原地编辑其草稿**——`ListDatasetExamples` → `DeleteDatasetExamples`(草稿为空时跳过)→ 用归一化后的 scenario 调 `AddDatasetExamples`,每步经 `UPDATING` 轮询到 `ACTIVE`;数据集 id 与已发布版本保留,草稿变为 `MODIFIED`。AWS 已不认识的副本(`GetDataset` 返回 `ResourceNotFoundException`)或标为 `deleted` 的副本会重新创建。返回该行;`CREATE_FAILED` / `UPDATE_FAILED` / 超时 → 502 `dataset.sync_failed`,携带 AWS `failureReason` 并记录到 blob |
| `POST` | `/api/eval/datasets/{dataset_id}/publish-version` | 对该行的云端副本调 `CreateDatasetVersion`,经 `UPDATING` 轮询到 `ACTIVE` → 返回该行,新版本位于 `cloud.versions` 首位且 `cloud.draft_status == "UNMODIFIED"`。无存活副本 → 409 `dataset.not_synced`;`UPDATE_FAILED` / 超时 → 502 `dataset.publish_failed`(原因记录到 blob,版本列表保留) |
| `GET` | `/api/eval/datasets/cloud` | workspace 区域内的全部 AWS 数据集:`{datasets[{datasetId, name, status, schemaType, exampleCount, draftStatus, updatedAt}]}` |
| `GET` | `/api/eval/datasets/cloud/{cloud_id}` | 草稿详情:`{datasetId, name, status, schemaType, exampleCount, draft_status, failure_reason, versions[{version, example_count, created_at}], runnable, has_ground_truth}`——版本最新在前 |
| `POST` | `/api/eval/datasets/cloud/{cloud_id}/publish-version` | 仅云端数据集的「发布版本」→ 返回上述刷新后的详情;失败语义与本地路由相同 |
| `DELETE` | `/api/eval/datasets/cloud/{cloud_id}` | `DeleteDataset`——删除草稿与全部版本;指向它的本地行标为 `cloud.status = "deleted"`,下次同步重新创建 |
| `DELETE` | `/api/eval/datasets/cloud/{cloud_id}/versions/{version}` | 带 `datasetVersion` 的 `DeleteDataset`——删除单个已发布版本;草稿与其他版本保留,缓存列表随之刷新 |

本地行上的 `cloud` blob:`{dataset_id, arn, status, synced_at, failure_reason, draft_status (MODIFIED|UNMODIFIED), example_count, versions[{version, example_count, created_at}]}`。它只缓存展示状态——AWS 是事实来源,每次变更都会重新读取 `GetDataset` / `ListDatasetVersions`。

## 控制台数据处理 API（V2）/ Console Data Processing API (V2)

控制台 V2 的数据中心把已观测的会话（Agent 轨迹）转换为本地评估数据集。下面两类路由共用同一个提取器（`app/evaluation/pipelines.py`）：通过可观测服务读取会话对话（与 `/api/observability/sessions` 相同的私有会话可见性规则），把每条用户输入与其后的 Agent 回复配对，每个会话写入一个 **predefined** 场景（`scenario_id = trace-<session>`、`turns[{input, expected_response}]`、`metadata.source = "trace"`）；目标为 **legacy** 数据集时写入首轮的 `{prompt, expected}`。模拟用户数据集会被拒绝（400 `dataset.kind_unsupported`）。合并时跳过已存在的 scenario id、可选跳过相同的首轮输入（`dedupe`），并遵守 200 条上限；这里不调用任何模型。

Pipeline 的数据源也可以读取 **CloudWatch 日志组**（`source.type = "logs"`，`app/evaluation/pipeline_logs.py`）：一次 Logs Insights 查询取时间窗口内最新的记录（至多 10,000 条；`keyword` 可按关键字缩小范围，不区分大小写），再由内嵌在 Pipeline 中的格式转换规则把每条记录转换为按会话分组的 `{role, text}` 轮次——`genai`（OTel GenAI 内容记录，如 `otel-rt-logs`，在服务端预先过滤，读取方式与会话对话相同）、`message`（一条记录是一条消息：`session_field` + `role_field` + `text_field`，角色按 `user_roles` / `assistant_roles` 映射）或 `exchange`（一条记录是一问一答：`input_field` + 可选的 `output_field`；不设 `session_field` 时每条记录各成一个场景，id 为稳定的哈希值）。字段只支持 JSON 点路径（不支持正则）；`session_field` 设为 `@logStream` 表示一个日志流就是一个会话；记录中 JSON 之前的文本前缀会被跳过。转换后的轮次进入与轨迹相同的提取器：`scenario_id = log-<session>`、`metadata{source: "logs", session_id, log_group}`。`processing.keep_replies: false`（两种数据源均可用）只保存输入。

| 方法 | 路径 | 行为 |
|---|---|---|
| `POST` | `/api/eval/datasets/from-sessions` | `{session_ids[1..50], range, dataset_id 与 name 二选一, description?, first_turn_only?, dedupe?}` → 201 `{dataset, added, skipped[{session_id, reason}]}`。原因：`not_found`、`no_transcript`、`no_exchange`，以及（`session_id` 为空时）`duplicate` / `dataset_full`。新建数据集却没有可用内容 → 422 `dataset.nothing_extracted`，`detail.skipped` 给出原因 |
| `GET` · `POST` | `/api/eval/pipelines` | 列出 / 创建保存的处理任务：`{name, description, source{type: traces\|logs, range, max_sessions ≤ 50, agent, status: all\|ok\|error（traces）, log_groups[1..10], keyword?, format{preset: genai\|message\|exchange, session_field, role_field, text_field, user_roles, assistant_roles, input_field, output_field}（logs）}, processing{first_turn_only, dedupe, min_input_chars, keep_replies}, output{dataset_id 与 dataset_name 二选一}}`（输出数据集不存在或为模拟用户 → 404 / 400；没有 `type` 的数据源视为 `traces`） |
| `POST` | `/api/eval/pipelines/preview-logs` | `{source}`（日志数据源）→ 用与运行相同的转换器展示当前会读取和提取的内容：`{events, truncated, parsed, failed{not_json\|not_genai\|no_session\|no_text\|other_role: n}, sessions_found, sessions[≤8 {session_id, stream, log_group, last, records, turns}], samples[≤5 条原始记录 {stream, timestamp, message}], paths[≤60 个记录中出现的字段点路径]}`；轨迹数据源 → 422 `pipeline.not_logs` |
| `GET` · `PUT` · `DELETE` | `/api/eval/pipelines/{pipeline_id}` | 读取 / 整体替换（运行中 → 409 `pipeline.running`）/ 删除——输出数据集保留 |
| `POST` | `/api/eval/pipelines/{pipeline_id}/run` | 返回 `202` 与状态为 `running` 的行（已在运行 → 409 `pipeline.running`），有界的运行在后台进行：列出时间窗口内的会话，按 Agent / 状态过滤，读取最新的至多 `max_sessions` 个，提取并合并（日志数据源：查询、转换，取最新的 `max_sessions` 个会话）。结果写入该行的 `last_run{at, scanned, matched, added, skipped, dataset_id, error}`（日志数据源另有 `log{events, parsed, failed, truncated}`），`status` 为 `succeeded` / `failed`（服务重启中断的运行读回为 `failed`）；`dataset_name` 输出在首次运行时创建，之后该 Pipeline 指向其 id |
| `GET` | `/api/eval/log-services?hours=1..336&log_group=…` | 在 `aws/spans`（以及所给的 `log_group`——span 发往 Agent 自身日志组的情形）的 span 中出现过的服务名称，一次 Logs Insights 查询 → `{services[{service_name, spans, sessions, last_seen, log_group_names, agent}], log_groups, hours}`。`log_group_names` 是建议的批量评估输入：`aws/spans` 加 span 资源属性（`aws.log.group.names`）指向的内容日志组；`agent` 为拥有该服务的平台 Agent，没有则为 `null`；`scopes` 为其 span 的插桩 scope，`evaluable` 表示 AgentCore Evaluation 是否会把其中任一 scope 作为 Agent span 读取（受支持框架的 scope，或通用前缀 `opentelemetry.instrumentation.*` / `openinference.instrumentation.*`，但不含 botocore、starlette 等传输层插桩；`null` 表示未知）——只有自定义 scope 的服务每个会话都会以「No evaluable agent spans found」失败 |
| `GET` | `/api/eval/log-groups?q=` | 名称包含 `q` 的日志组（不区分大小写的 `logGroupNamePattern`），最多 150 个 → `{log_groups[{name, created_at, retention_days, stored_bytes}], truncated}` |
| `GET` | `/api/eval/log-sessions?service_name=&log_group=…&hours=&q=` | `log_source` 任务的「日志」数据源：所给日志组（1–10 个）中 `service_name` 的会话，从其 span 中发现（span 是同时带有 `service.name` 与 `session.id` 的记录），按最近活动倒序，最多 500 个；行结构与 `/agents/{id}/log-streams` 相同（`stream` 为会话最近一条记录所在的日志组 · 日志流，另有 `traces`）。`q` 保留任意记录（span 或内容日志）包含该关键字的会话（不区分大小写的正则匹配，附命中数与摘录）。其他主体的私有助手会话不会列出 |
| `GET` | `/api/eval/agents/{agent_id}/log-streams?hours=1..336&q=` | 评估任务向导的「日志」数据源（`app/evaluation/log_streams.py`）：Agent 运行日志组中在时间窗口内有事件的日志流，按最近事件倒序 → `{log_group, streams[{stream, session_id, kind, first_event, last_event, match, matches, snippet}], truncated, hours, q}`。`kind`：`session`（代码运行时的 `[runtime-logs-<sessionId>]` 日志流）、`otel_session`（某会话在 `otel-rt-logs` 中的切片——Harness 运行时按 microVM 命名日志流，会话只存在于该共享流）或 `shared`（不属于单个会话，不可选）。`q` 保留流名包含该关键字（不区分大小写）或事件中包含该完整词（`FilterLogEvents`，区分大小写；`otel-rt-logs` 中的命中归到事件所属会话）的行，`match` 为 `name` / `content`，并给出命中数与摘录。两类扫描都有上限（500 个日志流、10 页过滤结果），`truncated` 表示上限遮住了部分会话。其他主体的私有助手会话不会列出。选中的 `session_id` 以普通 `POST /api/eval/runs`（`session_ids` + `session_source: "logs"`）发起评估 |

所有路由均为 `MEMBER` 且按工作区隔离（`eval_pipelines.workspace_id`）。

## 控制台评估器 API / Console Evaluators API

`/api/eval/evaluators` 是 `?view=evaluators` 子页背后的自定义评估器 CRUD。AWS 是唯一事实来源（没有 ledger 行）；内置与第三方评估器只读。一个自定义评估器恰好有三种**定义**之一，由载荷里出现的字段决定——`instructions`（LLM 评审，`llmAsAJudge`）、`base_evaluator_id`（派生，`derived`）或 `lambda_arn`（代码评估器，`codeBased.lambdaConfig`）；同时给出两个或一个都没有 → 400 `evaluator.definition_ambiguous`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/eval/evaluators` | `{evaluators[], builtin_count}`——先是本地内置目录（`source: builtin`，轨迹匹配器标 `requires_ground_truth`），再是账户 `ListEvaluators` 的行（`source: third_party \| custom`、`evaluator_type`、`provider`、`status`）。自定义行带 `definition: judge \| derived \| code`（由 `evaluatorType` 推导——列表不含配置）。账号列举失败时仍返回 200，仅含内置评估器，并附 `custom_unavailable: {code, message}`（`aws.*`、`workspace.assume_role_failed` 或 `evaluators.list_failed`；通用消息，绝不回显 AWS 文本）；成功时无此键 |
| `POST` | `/api/eval/evaluators` | 创建 → 201 `{evaluator_id, arn}`。公共字段：`name`（`^[a-zA-Z][a-zA-Z0-9_]{0,47}$`）、`description`。**评审**：`instructions`（10–4000 字符，至少一个 `{placeholder}`，否则 422 `evaluator.missing_placeholder`）、`rating_scale[≥2]`（默认 pass/fail）、`model_id`、`level`（TOOL_CALL \| TRACE \| SESSION，默认 TRACE）。**派生**：`base_evaluator_id`（`Builtin.*` \| `ThirdParty.*`；不存在 → 400 `evaluator.base_not_found`）、`model_id`；级别取自基础评估器。**代码评估器**：`lambda_arn`（`arn:aws[-partition]:lambda:<region>:<account>:function:<name>[:qualifier]`）、`lambda_timeout_s` 1–300（默认 60）、`level`；Lambda 必须与 workspace 同区域，否则 422 `evaluator.lambda_region_mismatch`（`detail: {lambda_region, workspace_region}`）。派生或代码载荷携带 `rating_scale` → 400 `evaluator.rating_scale_not_allowed`。任何定义在 `CreateEvaluator` 上都必须带 `level` |
| `GET` | `/api/eval/evaluators/{evaluator_id}` | `{id, name, level, description, definition, instructions, rating_scale, model_id, base_evaluator_id, lambda_arn, lambda_timeout_s, evaluator_type, provider, status}`——其他定义的字段为空/null（代码评估器的 `instructions: ""`、`rating_scale: []`、`model_id: null`） |
| `PUT` | `/api/eval/evaluators/{evaluator_id}` | 全量配置替换（`UpdateEvaluator` 接收完整配置，因此每个字段都要回传），载荷与创建相同但不含 `name` → 返回刷新后的详情。载荷必须与评估器**当前**定义一致：用评审/派生载荷更新代码评估器，或用代码载荷更新评审/派生评估器 → 400 `evaluator.definition_mismatch`（`detail: {current, payload}`）——评估器绝不会被转换。托管 id → 400 `evaluator.builtin_immutable` |
| `DELETE` | `/api/eval/evaluators/{evaluator_id}` | `DeleteEvaluator` → `{deleted: true}`；托管 id → 400 `evaluator.builtin_immutable`。被 ENABLED 在线配置引用的评估器会被 AWS 锁定 |

**代码评估器（Lambda）契约。** 服务以 `{schemaVersion, evaluatorId, evaluatorName, evaluationLevel, evaluationInput.sessionSpans, evaluationReferenceInputs, evaluationTarget}` 调用函数，函数返回 `{label, value?, explanation?}` 或 `{errorCode, errorMessage}`；单次调用受所配置的超时（≤ 300 秒）与 6 MB 载荷限制。**控制台不管理其 IAM**：平台在批量与在线运行中作为 `evaluationExecutionRoleArn` 传入的评估执行角色需要对该函数拥有 `lambda:InvokeFunction` + `lambda:GetFunction` 权限，且函数的资源策略须允许 `bedrock-agentcore.amazonaws.com` 主体（用 `aws:SourceAccount` / `aws:SourceArn` 收窄）。创建时两者都不做检查——针对角色无法调用的函数发起运行会像任何评估器错误一样按会话失败。

## 控制台评估运行 API / Console Evaluation Runs API

`/api/eval/runs` 通过有界运行队列(`eval_max_concurrent_runs`,上限为账户 5 个活跃批量评估的配额)驱动批量评估 / insights 分析。运行状态:`queued → invoking → waiting → evaluating → completed | failed | stopped`。每一行都带 `stop_requested`(操作员已请求停止,批次仍在 STOPPING)。

`POST /api/eval/runs` 还接受可选的 `name`（1–64）与 `description`（≤ 1000）——控制台 V2 以命名的评估任务列出运行——每一行都带这两个字段（未命名时为 null）以及 `updated_at`。

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/api/eval/runs?limit&offset&mode&agent_id` | 最新在前的分页 `{runs, total, limit, offset}` |
| `GET` | `/api/eval/runs/{run_id}` | 单个运行(分数 / insight 树 / `batch_eval_id` / `scenario_failures`——重试后仍失败而被跳过的模拟用户场景，其余场景照常运行，`[{scenario_id, retries, code, error}]`（所有场景都失败时运行才失败）/ `session_failures`——批次以 COMPLETED_WITH_ERRORS 结束时为 `{total, failed, kind, sessions: [{session_id, index, kind, error_type, message}]}`，`kind` 取 `telemetry_incomplete`（只有缺失或不完整 span 的错误：追踪数据没有完整到达 CloudWatch，重新评估也无法恢复）、`evaluator_error`、`mixed` 或 `unknown`，`index` 为从 1 开始的场景序号；无错误的运行或早于该字段的行为 null，首次读取其结果时补齐 / `error` / `stop_requested`)。 |
| `GET` | `/api/eval/runs/{run_id}/results` | **终态评估器运行的逐会话评审记录** —— `{available, sessions: [{session_id, results: [{evaluator_id, level, score, label, explanation, error_type, error_message}]}], count, truncated}`。账本行只保存每个评估器的平均分;每个分数背后评审模型给出的理由只存在于批次自己的结果日志流中(`GetBatchEvaluation.outputConfig.cloudWatchConfig`,与在线评估视图读取的 `gen_ai.evaluation.result` 记录同族),该路由按需读取、绝不持久化。会话顺序与运行的 `session_ids` 一致。无可读内容时返回 `available=false` + `reason`(`insights_run` \| `no_batch` \| `run_active` \| `stream_missing` \| `unreadable` + `detail`)而非错误。不存在 → 404 `run.not_found` |
| `POST` | `/api/eval/runs` | 启动运行(范围四选一:`dataset_id` \| `cloud_dataset_id` \| `session_ids` \| `lookback_hours`)→ 201。`session_source: "logs"`（仅用于展示，只能搭配 `session_ids` 范围，否则 422 `run.session_source_scope`）把范围记为 `dataset_name` `logs:<n>`，与 `window:<N>h` 相同。**评估对象**：`agent_id` 与 `log_source {service_name, log_group_names[1..10]}` 二选一（否则 422 `run.target_required`）。`log_source` 运行评估已在 CloudWatch 中、背后没有平台 Agent 的遥测（例如未托管在 AgentCore Runtime 上的 Agent）：批量评估的 `cloudWatchLogs` 数据源即 `serviceNames: [service_name]` 加所给的 `logGroupNames`，只支持被动范围（`session_ids` / `lookback_hours`；数据集范围 → 422 `run.log_source_scope`），且每个日志组必须存在（422 `run.log_group_missing`，`detail.missing`）。运行行回显 `log_source`，`agent_id` 为 `""`，`agent_name` 为服务名称。`cloud_dataset_id` 范围可附带 `dataset_version`(已发布版本号,如 `"2"`,绝不是 `DRAFT`;省略即草稿):版本必须存在于 `ListDatasetVersions`(否则 422 `run.dataset_version_unknown`,不创建运行行),`GetDataset` / `ListDatasetExamples` 读取该快照。`dataset_version` 搭配其他范围 → 422 `run.dataset_version_scope`。每个运行行都回显 `dataset_version`(草稿、本地、session 与时间窗口运行为 `null`)。不带 ADOT 启动器部署的 BYOC 代码 zip，若其端点 14 天内没有任何 Span → 400 `eval.no_telemetry`（`detail.service_name`），在创建运行行之前返回。|
| `POST` | `/api/eval/runs/{run_id}/stop` | **停止活跃运行** → 202 返回该运行。批次已在 AWS 上存在(`batch_eval_id` 非空)时调用 `StopBatchEvaluation`:批次经 `STOPPING → STOPPED`,已评判的会话保留结果,轮询器把运行记为 `stopped`,附带这些部分分数 / insight 树以及 `error = "stopped by operator"`。仍在 `queued` 的运行在本地取消(worker 出队时跳过,不调用 AWS),立即返回 `stopped`。正在回放数据集或等待遥测(尚无批次)的运行在提示词之间停止,绝不会调用 `StartBatchEvaluation`。终态运行(`completed` / `failed` / `stopped`)→ 409 `run.not_active`;不存在 → 404 `run.not_found`。刻意不暴露 `DeleteBatchEvaluation`——账本保留的部分结果 AWS 会丢弃 |
| `POST` | `/api/eval/runs/{run_id}/recheck` | `eval.run`——**重新从 AWS 读取失败运行的批次** → 202 返回该运行。适用于已启动批次（`batch_eval_id` 非空）的 `failed` 运行，以及尚无 `session_failures` 的部分失败 `completed` 评估器运行（重新落定时补齐）：调用一次 `GetBatchEvaluation`；批次已终态时按轮询器的方式落定该行（例如带分数的 `completed`，或带批次自身原因的 `failed`），仍在运行时该行回到 `evaluating` 并重新开始轮询。只读——不会重新运行，也不产生费用。其他运行 → 409 `run.not_recheckable`；不存在 → 404 `run.not_found`。每次批次等待都受 `eval_batch_wait_s` 限制（默认 7200 秒，每 30 秒轮询一次）；超时的运行以「batch evaluation still <STATUS> after the N-minute wait」失败，可在 V2 任务详情中重新检查 |
| `DELETE` | `/api/eval/runs/{run_id}` | `eval.run`——**移除没有产出结果的运行**（`failed` / `stopped`）的账本行 → `{deleted, run_id, status, aws_batch_left_in_place}`。只删账本行：已到达 AWS 的批量评估保持不动（AWS 仍是事实来源，该行只是控制台的指针，响应里会指明）。`completed` 运行是评估历史，活跃运行须先停止 → 409 `run.not_deletable`；不存在 → 404 `run.not_found`。评估页运行表和助手 NEXT STEPS 历史在这类行上显示 ✕ |
| `GET` | `/api/eval/runs/{run_id}/recommendation-inputs` | **基于该运行的优化建议将改写的内容** → `{source, system_prompt, tools: [{name, description, origin}], notes, evaluators, default_evaluator, eligible, reason_code}`。`source: "harness"` = 实时读取 Managed Harness（`GetHarness`：系统提示词、每个 `inline_function` 的描述，以及每个 `agentcore_gateway` 的 target 工具 schema，命名为 `<target>___<tool>`，均按 Harness `allowedTools` 过滤）；`"spec"` = Launchpad Agent 配置（提示词 + 可发现的工具）；`"manual"` = 无可读内容（无提示词的 BYOC、CloudWatch 来源的运行、已删除的 Agent），控制台要求用户填写两项输入。`notes` 列出无法读取的内容（`remote_mcp_runtime_only`、`gateway_unreadable`，以及运行比其 Harness 存在更久或 GetHarness 被拒绝时的 `harness_unreadable`——此时输入回退到配置 / 手动输入，而不是报错）。`evaluators` = 所有可作为优化目标的评估器，形如 `[{id, name, level, group: run|builtin|third_party|custom, recommended}]`：该运行自身的评估器在前，其后是开发者指南推荐的两个目标（`Builtin.GoalSuccessRate` / `Builtin.Helpfulness`，`recommended: true`），再是 AWS 内置、第三方托管评估器和本账号 ACTIVE 的自定义评估器（每个自定义评审都用 `GetEvaluator` 读取配置）。`excluded_evaluators` = `[{id, reason}]`，列出不能作为目标的评估器：`lower_is_better`（惩罚型分数）、`ground_truth`（轨迹匹配器、读取 `{expected_response}` 等占位符的评审、读取参考答案的托管代码评估器）、`categorical`（非数值评分）、`unavailable`（该运行用过但已不存在）。`POST` 使用相同规则（422 `recommendation.evaluator_<reason>`）。没有会话 id 的运行（时间窗口运行）`tools_eligible` 为 false |
| `GET` | `/api/eval/runs/{run_id}/recommendations` | 从该运行启动的优化建议，最新在前 → `{recommendations: [{id, kind, recommendation_id, status, input_source, system_prompt, evaluator, tools, skipped_tools, result, error, …}]}`。每次读取时对未终态的行调用一次 `GetRecommendation` 刷新（无后台轮询）；结束但没有产出文本的任务显示为 `FAILED` 并附 AWS 自身的错误。工具任务若因部分工具未出现在 trace 中被拒绝，会剔除这些工具后**重试一次**（`skipped_tools`） |
| `POST` | `/api/eval/runs/{run_id}/recommendations` | `eval.run`——**启动优化建议** `{kinds: ["system_prompt" \| "tool_descriptions"], input_source, system_prompt?, evaluator?, tools?: [{name, description}]}` → 201 `{recommendations}`。每种类型一次 `StartRecommendation`，范围均为该运行的会话：系统提示词任务将 `agentTraces.batchEvaluation` 固定为该运行的批次；工具描述任务不接受该来源（实测 `ValidationException`），因此内联传入相同会话的 span（`sessionSpans`，用按需评估的查询读取，最多 20 000 个 span），名称从未出现在其中的工具会预先剔除（`skipped_tools`）。仅限带批次的 `completed` 运行，否则 409 `recommendation.run_not_completed` / `run_no_batch`；无会话 id 的运行上的工具任务 → 409 `run_no_sessions`，遥测已过期 → 409 `no_spans`，没有被调用过的工具 → 422 `tools_not_traced`；提示词为空 / 无工具 / 工具缺描述 → 422。所有拒绝都发生在第一次 Start 之前，不会出现一种类型已启动、另一种未启动的情况；分类评分的自定义评审 → 422 `recommendation.evaluator_categorical`。结果供查看和复制；只有下面的接受路由会应用建议 |
| `PUT` | `/api/eval/runs/{run_id}/recommendations/{rec_id}/edit` | `eval.run`——保存对一条 `COMPLETED`、尚未接受的系统提示词建议的人工修改。请求体 `{system_prompt}`；传 `null`、空白或与原始建议相同的文本即撤销修改。修改存为 `edit {prompt, by, at}`，与 `result` 分开（GetRecommendation 刷新会重写 `result`），列表路由随之返回；控制台的差异对比、复制和接受随后都使用修改版。只写账本，不发布任何内容。已接受 → 409 `recommendation.already_accepted`；工具描述建议 → 400 `recommendation.edit_kind`；未完成 → 409 `recommendation.not_completed` |
| `POST` | `/api/eval/runs/{run_id}/recommendations/{rec_id}/accept` | `agents.deploy`——**接受**一条 `COMPLETED` 的系统提示词建议，发布为新的 Harness 版本 → 202 `{agent, job_id, deployment_id, recommendation}`。请求体 `{system_prompt?}` 为审阅后要发布的文本；省略时使用已保存的 `edit`，否则使用原始建议（与原始建议有差异时记为 `accepted.edited`）。与 `POST /api/agents/{id}/redeploy` 使用相同的校验和 update 模式部署任务，重新发布该运行所属的平台 Harness（UpdateHarness → 新的不可变版本，`DEFAULT` 随之切换）。建议改写的是*线上*提示词，其中已含平台追加的 `## Knowledge bases` 段落，因此会先剔除该段。行上记录 `accepted {by, at, agent_id, previous_version, job_id, deployment_id}`（列表路由随之返回）；只能接受一次 → 409 `recommendation.already_accepted`；工具描述建议 → 400 `recommendation.accept_kind`；未完成或无文本 → 409 `recommendation.not_completed`；非 Harness Agent → 400 `recommendation.accept_not_harness` |
| `GET` | `/api/eval/queue` | `{running, queued, locked, max_concurrency}`——取消的运行立即离开队列,计数只覆盖活跃运行 |

## 控制台 Agent-DLC API —— 判据、黄金集、校准与放行门

设计见 [agent-dlc-design.zh-CN.md](agent-dlc-design.zh-CN.md)，这里是接口面。方法论的主张是
**放行由评估决定，不由会议拍板**，所以这组路由把四件事收归平台：标准（判据表）、证据（黄金集）、
裁判能否代替人（校准），以及放行决定本身（挡在生产流量前的门）。

其中三项权限**只授予具体的人，不按角色给**——`criteria.sign`、`golden.admit`、
`judge.calibrate`——因为它们决定“什么叫好”。`criteria.manage`（成员/操作员）、
`waiver.approve` 与 `release.sign`（操作员）沿用既有的职责分离。发布、签署、初次编制、准入、
豁免审批与全部放行操作都在 `PROD_PROTECTED` 之列：在 prod 级工作区，成员无法触达。

### 判据表

判据集按 `lineage_id` 版本化。发布即冻结该版本，再编辑会开出下一版。已发布的**智能体**判据集
必须由最后编辑人之外的人签署（作者会收到 `403 criteria.self_sign`，管理员可覆盖）。校验是强制的，
不是建议：红线不能交给大模型裁判判定，成本与性能判据必须是指标，每个维度要么有判据、要么写明
`n/a:<dimension>`，并且至少要有一条红线。裁判类判据的**实际档位**在校准记录判定为一致且未过期之前
一律是 `observe`，这样判据表不会声称自己在拦人、而其实拦不住。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/criteria-sets?kind&agent_id` | `{sets: [...]}` —— 每个谱系一行，取最新版本 |
| `POST` | `/api/criteria-sets` | `criteria.manage` —— `{kind: agent\|template, name, agent_id?, scenario?, template_lineage_id?, template_version?, from_evaluation_plan?}` → 201 返回完整载荷。模板的判据行是**复制**进来的；之后模板升级不会在智能体背后改动它 |
| `GET` | `/api/criteria-sets/{lineage_id}?version` | `{set, criteria, summary, findings, calibration_policy, newer_template_version, versions}`。`summary` 含 `tiers`、`judge_share`、`run_cadence`、`declared_gates` / `effective_gates` 与 `compound_gate_rate`（所有门限同时达成的概率：八条 95% 的门限叠加只有 66%） |
| `PUT` | `/api/criteria-sets/{lineage_id}` | `criteria.manage` —— 替换草稿的判据行。`422 criteria.invalid`，`detail.findings` 逐条给出 `{level, code, message, key}`；已发布版本返回 `409` |
| `POST` | `/api/criteria-sets/{lineage_id}/versions` | `criteria.manage` —— 从已发布版本开出下一个草稿 → 201 |
| `POST` | `/api/criteria-sets/{lineage_id}/publish` | `criteria.manage` —— 冻结该版本。只要还有 `error` 级发现就拒绝 |
| `POST` | `/api/criteria-sets/{lineage_id}/sign` | `criteria.sign` —— 业务负责人签署 `{note}`。未签署的标准会让放行门判为 INVALID，所以这一步是承重的 |
| `POST` | `/api/criteria-sets/{lineage_id}/adopt-template?template_version=` | `criteria.manage` —— 把更新的模板版本采纳进**新**的智能体版本 → 201。新版本未签署：尺子变了就要重新签 |
| `DELETE` | `/api/criteria-sets/{lineage_id}` | `criteria.manage` —— 丢弃当前草稿 → `{discarded: <version>}` |
| `GET` | `/api/criteria-sets/{lineage_id}/diff?a=&b=` | `{from, to, changes: [{key, change, fields, before, after}]}` |

### 黄金集

一个父数据集归拢三个切分数据集（`dev` / `regression` / `holdout`），每个切分各自同步成一个
AWS Dataset，各有不可变版本。样本出处记录在 `metadata.dlc`（`case_tier`、`criteria_ids`、
`origin`、`expected_source`、`retired`）。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/golden-sets` | `{golden_sets: [{id, name, criteria_lineage_id, splits}]}` |
| `POST` | `/api/golden-sets` | `criteria.manage` —— `{name, criteria_lineage_id?, description?}` → 201。放行门是**通过**判据谱系找到黄金集的 |
| `GET` | `/api/golden-sets/{dataset_id}` | 该集合加 `coverage`：判据 × 用例档位，样本少于三条的判据标 `thin`（它的通过率是噪声），另有 `unmapped_items`、`agent_observed_items` 以及每个来源的 `bias` 说明 |
| `POST` | `/api/golden-sets/{id}/items` | `criteria.manage` —— 追加到 `dev` 或 `regression` → 201。不接受保留集（`409 golden.holdout_closed`） |
| `POST` | `/api/golden-sets/{id}/seed` | `golden.admit` —— **唯一写入保留集的路径。** `{items: [{split?, scenario_id, turns, metadata}], shares?}` 会把未指定切分的样本按用例档位分层，使三个切分见到同样的构成，然后**封存**保留集：再调一次就是 `409 golden.holdout_sealed`。只有没人对着它调参，保留集才有意义 |
| `POST` | `/api/golden-sets/{id}/move` | `criteria.manage` —— `{scenario_id, to: dev\|regression}`。样本既不会移入也不会移出保留集 |
| `POST` | `/api/golden-sets/{id}/retire` | `criteria.manage` —— `{split, scenario_id, reason}`。退役样本离开放行门的分母，但仍会被重放，这样修好的问题不会悄悄回来 |

`items` 与 `seed` 在写入前逐条校验，任何一条不通过都整体拒绝，什么都不写（`seed` 也就不封存保留集，改好后可以重新提交）：

| 错误码 | 何时返回 |
|---|---|
| `422 golden.empty_input` | `turns` 为空，或任一轮的 `input` 为空 |
| `422 golden.empty_expected` | 任一轮的 `expected_response` 为空（与 `admission.no_expected` 同一条规则） |
| `422 golden.unknown_criteria` | `metadata.dlc.criteria_ids` 里有所服务判据表（该谱系最新已发布版本及更新的草稿）中没有的编号；黄金集没有关联判据谱系时不检查 |

`code` 取第一条出错样本的规则；`detail` 为 `{errors: [{index, scenario_id, code, criteria_ids?}], count, unknown_criteria?}`：
`errors` 列出前 20 条问题（`index` 是该样本在请求 `items` 里的下标，同一条样本可能出现两次），`count` 是问题总数，
`unknown_criteria` 汇总所有未知编号。已经写入的样本不受这些规则影响。

### 标注与裁判校准

标注是**盲的**：任务关闭前不会把裁判的判定给标注人看——先看到它，一致性这个数字就没有意义了。
`decide` 在数据不支持时拒绝判为 `aligned`（κ 下限、最小样本量、且不低于人—人 κ 减 0.05），
所以人只在数据允许的几种读法之间做选择。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/annotation-tasks?agent_id&status` | `{tasks: [...]}` |
| `POST` | `/api/annotation-tasks` | `criteria.manage` —— `{criterion_key, annotators[2..10], adjudicator?, run_id?\|items?, purpose, agent_id?, criteria_lineage_id?, dataset_id?}` → 201。取自已完成评估运行的样本会带上该运行的裁判判定（关闭前隐藏）。该运行对此判据没有判定时返回 `422 annotation.no_items` |
| `GET` | `/api/annotation-tasks/{id}` | 按**当前查看者**的权限返回：标注人只看到自己的标注；持 `judge.calibrate`（或管理员）看到全部 |
| `POST` | `/api/annotation-tasks/{id}/labels` | `{item_ref, label, rationale?, answer?}` → 201。只有该任务的标注人可提交（`403 annotation.not_annotator`）；仲裁阶段只有仲裁人可提交 |
| `POST` | `/api/annotation-tasks/{id}/adjudicate` | 进入仲裁——由仲裁人裁定有分歧的样本 |
| `GET` | `/api/annotation-tasks/{id}/agreement` | `{n, pairs, human_human_kappa, judge_human_kappa, kappa_ci, band, confusion, disagreements, accuracy, policy, suggested_verdict}`。**先看人—人**：如果人都判不一致，这条判据写不下来，换裁判也救不了 |
| `POST` | `/api/annotation-tasks/{id}/decide` | `judge.calibrate` —— `{verdict: aligned\|not_aligned, note}` → 校准记录。数据不支持 `aligned` 时返回 `409 calibration.not_supported` 并带上那几个数字；若判定人参与产生了该任务的标注——标注人、仲裁人，或发放过该任务标注链接的人（同一个人发出的两个链接只算一位标注人，不是两位）——返回 `403 calibration.own_labels`：标注就是证据，写证据的人不同时裁定它。此外 `aligned` 需要真实的人类上限：只有一个人标注的样本不计入，因此一个人无法自行认证裁判 |
| `GET` | `/api/calibration/candidate-runs?agent_id&criterion_key&q&since&source&only_eligible&limit&offset` | 校准任务的运行选择器：某个智能体的批量评估器运行（`mode=evaluators`），可见范围与 `/api/eval/runs` 对当前用户一致，最新完成的在前 → `{total, runs[{id, name, dataset_name, status, split, created_at, finished_at, release_id, items, eligible, reason}]}`。`items` 是从该运行建任务能得到的样本数；`q` 匹配名称或 id；`source=gate` 只保留放行记录评估过的运行（`release_id`），`manual` 为其余运行。保留集（holdout）运行从不出现（标注它等于把保留集要藏起来的内容给人看）。`only_eligible=false`（默认 `true`）同时返回不能选的运行，可选的在前，并带 `reason`：`not_completed` / `no_results` |
| `GET` | `/api/calibration/{criterion_key}?agent_id` | `{records, status, policy}`。记录在工作区 `calibration.period_days` 之后过期——此后该裁判重新只作观察 |

**标注链接**让没有控制台账号的专家也能参与标注。链接**本身就是一个标注人**：生成时会把
`link_<id>` 追加到任务的标注人列表，因此它的票以稳定身份计入 κ，而标签（收件人姓名）只用于展示。
**prod 级工作区拒绝生成**（`409 annotation.links_not_allowed`，并说明替代做法）；在 dev 期间
发出的链接，一旦工作区升为 prod 就不再可用。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/annotation-tasks/{id}/links` | `{links, allowed, reason}` —— prod 工作区下 `allowed: false` |
| `POST` | `/api/annotation-tasks/{id}/links` | `judge.calibrate` —— `{label, expires_in_days?}` → 201，返回 `token` / `path` / `url`。令牌**只显示一次**，落盘只存其 sha256 |
| `DELETE` | `/api/annotation-tasks/{id}/links/{link_id}` | `judge.calibrate` —— 撤销。它已经投出的标注保留：那是真实的票 |
| `GET` | `/share/annotate/{token}` | **PUBLIC** —— 盲标队列：样本与**该标注人自己**的标注。绝不返回裁判判定、他人投票或一致性数字 |
| `POST` | `/share/annotate/{token}/label` | **PUBLIC** —— `{item_ref, label, rationale?, answer?}` → 刷新后的队列 |

任何不可用的链接（未知、格式错误、已撤销、已过期、任务已删、工作区升为 prod）统一返回
`404 share.not_found`：外部人员不应能区分“被撤销的链接”和“从未存在的链接”。页面为
`/r/annotate/<token>`。

### 放行门

`UpdateAgentRuntime` / `UpdateHarness` 会自动把 DEFAULT 端点滚到新版本，所以门控智能体通过
**具名 `live` 端点**对外服务，所有调用路径都带 `qualifier="live"`。当工作区策略为
`release_mode="gated"` 时，一次成功部署会把 `candidate` 指向新版本并开出一条放行记录——
`live` 不动，于是候选版本可以在没有任何用户看到的前提下接受判定。签署会把 `live` 指过去；
回滚再指回来。不删除任何资源。

放行门按固定顺序判定：**红线**（有任何突破 → `BLOCKED`，永不豁免）→ **分母**（判定缺失，
或无法判定占比超过 5% → `INVALID`）→ **各维度门限**（按整数百分比比较，未达 → `BLOCKED`，
除非有生效中的豁免覆盖）→ **观察项**（只记录）。出处问题——判据版本未签署、运行用的是别的版本
或别的端点、没有评估保留集——同样让结论为 `INVALID`。`INVALID` **不是**“不达标”：是证据判不了，
该修的是证据，而不是去豁免。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/agents/{id}/release` | `{release_mode, state, criteria_set, pending, records, waivers}`。`state` 含 `endpoint_mode`、从 AWS 读回的 live/candidate 版本，以及 `gateable` 与 `gateable_reason` |
| `POST` | `/api/agents/{id}/release/migrate` | `release.sign` —— 在当前版本上创建 `live` 并把生产流量切过去。生产服务什么版本是放行决定，所以构建这个智能体的工程师无权执行。`409 release.not_gateable` 会说明原因（A2A、系统预置、尚未部署） |
| `POST` | `/api/agents/{id}/release/evaluate` | `eval.run` —— `{repeats?, confirm_cost?}` → 202。对 `candidate` 排入回归集**和**保留集两次运行，并先算钱：`409 run.cost_over_limit` / `run.cost_confirm_required` 带上预估，成本闸门在花掉第一个会话之前就拒绝 |
| `GET` | `/api/agents/{id}/release/gate` | `{status: evaluating\|decided, report, record}`。报告为 `{verdict, criteria: [...], redline_violations, invalid, gate_failures, waived, provenance, order}`；每行带实测通过率与 Wilson 区间、`threshold_inside_ci`（样本判不了）、分母，以及相对上一次放行的 `trend` |
| `POST` | `/api/agents/{id}/release/sign` | `release.sign` —— 放行候选版本：`live` 被重新指向，流量随之切换。只有 `PASS` 的报告可签（否则 `409`），且签署人不能是发起人（`403`） |
| `POST` | `/api/agents/{id}/release/block` | `release.sign` —— 候选版本不进生产，原因记录在案 |
| `POST` | `/api/agents/{id}/release/rollback` | `release.sign` —— 把 `live` 指回上一个版本 → 返回新状态。不删除任何东西 |
| `GET` | `/api/release-records?agent_id=` · `/api/release-records/{id}` | 决策历史，每条记录带它的放行报告与出处 |

**豁免**是明知未达门限仍放行。它需要理由、具名风险责任人、失效日期（≤ 30 天）和第二个人审批，
红线直接拒绝（`409 waiver.redline`）。放行报告会统计某条判据被豁免过几次——超过一次就是标准本身
的问题，不是例外。

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/agents/{id}/waivers` | `criteria.manage` —— `{criterion_key, actual?, threshold?, reason, risk_owner, compensating_control?, expires_on}` → 201 |
| `POST` | `/api/waivers/{id}/approve` · `/reject` | `waiver.approve` —— 审批人不能是发起人 |
| `DELETE` | `/api/waivers/{id}` | `waiver.approve` —— 撤销生效中的豁免 |

### 样本准入、漂移监测、运行与读回

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/admission?agent_id&status&refresh` | 候选队列，来自点赞点踩、专家纠正、问题箱与 Insights 聚类。`priority` 把被评估器判成**通过**的候选排在最前：它既是缺失的用例，也是一份校准样本 |
| `GET` | `/api/admission/{id}` | 单个候选，附个人信息 `redaction` 预览与 `nearest` 已有黄金集样本（精确与归一化匹配），用于识别重复 |
| `POST` | `/api/admission/{id}/admit` | `golden.admit` —— `{split, expected_response, expected_source, criteria_ids, case_tier}` → 201。期望答案必须由人撰写或确认（拒绝 `agent_observed`：智能体自己的输出不是标准），脱敏不能是 blocked，且永不写入保留集 |
| `POST` | `/api/admission/{id}/reject` · `/duplicate` | `golden.admit` —— 拒绝必须写原因；原因**就是**记录 |
| `GET` · `PUT` | `/api/agents/{id}/watch` | `eval.run` —— 排程重评 `{every, at_hour, tz, repeats, max_cost_usd, enabled}`，并返回 `series`、`alerts`、`drift` 与近期运行。模型可能在智能体底下换掉而一行代码都没改，所以标准要按排程重新施加 |
| `POST` | `/api/agents/{id}/watch/run?split=` | `eval.run` → 202。预估超过成本上限的运行会被**跳过并上报**（`409 watch.over_cost_ceiling`），而不是先花掉 |
| `GET` | `/api/eval/runs/{run_id}/criteria?criterion_key` | 一次运行的逐判据结果：`summary`（n、通过/不通过、通过率、Wilson 上下界、`missing`、`undetermined_rate`、`pass_k`，或指标的 `value` 与其规则）、`denominator` 口径、`endpoint_qualifier`，以及每个会话的判定与裁判给出的理由 |
| `POST` | `/api/eval/runs/{run_id}/criteria/snapshot` | 重新读取一次已完成运行的结果流（读取抖动后的重试）→ 202 |
| `GET` | `/api/eval/runs/compare?runs=a,b,c` | 2 个及以上运行的修复阶梯（最早在前）：逐判据 Δ 与两比例 p 值、`fixed` / `new_failures` / `still_failing`，以及每级的 `layers_changed`——同时改两层带来的收益归不到任何一层。判据版本不同的运行会返回 `comparable: false` 与 `two_numbers`（智能体变好了多少，标准变严了多少） |
| `GET` | `/api/agents/{id}/ladder` | 同上，取该智能体最近若干次已完成的判据运行 |
| `POST` | `/api/eval/runs/estimate` | `{agent_id?, dataset_id?, items?, evaluators, repeats}` → 样本数 × k ×（智能体每会话 + 裁判每样本），并说明估算依据（`history_7d` / `rough` / 未定价）、预计时长，以及工作区策略会要求确认还是直接拒绝 |
| `GET` | `/api/agents/{id}/scorecard` | 按方法论固定顺序给出五个维度的标准与现状，附趋势序列、指标实测值、最近一次放行判定及其结果、生效中的豁免、`calibration_debt` 与黄金集覆盖 |
| `GET` | `/api/audit?action&target&project&actor&limit` | 管理员 —— 每个工作台都会展示的决策记录：谁签署了标准、谁准入了样本、谁批准了豁免、谁放行的 → `{events[{id, actor, actor_user_id, action, target, project_id, workspace_id, at}]}`。`action` 按前缀匹配。跨项目：`project=<id>` 为该项目在其绑定的所有工作区中的事件，`project=workspace` 为本工作区中不属于任何项目的事件；`actor` 为用户名或 `users.id` |

`POST /api/eval/runs` 另外接受 `repeats`（1–10，即 **pass^k**）与 `confirm_cost`。`repeats`
把每条数据集用例在不同会话中重复 k 次，这是衡量一致性的唯一办法——AgentCore 没有按用例重复的参数。
它只适用于“对智能体回放数据集”这一种范围（否则 `422 run.repeats_scope`），成本乘以 k；
而对黄金集切分的运行会自动按该切分已发布的判据打分，因此会落到修复阶梯上。

本模块读取的放行策略字段由 `PUT /api/release-policies/{workspace_id}`（管理员）设置：
`release_mode`（`direct` | `gated`）、`calibration`（`{period_days, kappa_floor}`）以及成本闸门
`eval_cost_confirm_usd` / `eval_cost_max_usd`。该 PUT 会整体替换策略。

## 控制台在线评估 API / Console Online Evaluation API

`/api/eval/online/*` 管理 AgentCore **在线评估配置**:按采样比例持续给真实会话打分。AWS 是唯一事实来源,
ledger 只存标识。列表返回 workspace 账号内全部配置并按 `owner` 归类:`agent`(本控制台为 agent 创建)、
`experiment`(`exp_*`/`can_*` 实验 arm,只读)、`external`(其他来源)。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/eval/online` | `{configs, total}`:全部配置,含 `owner`、双状态、`failure_reason`、evaluators、采样率、超时、`matched_agent`、`duplicate_enabled`、`results_log_group` |
| `POST` | `/api/eval/online` | 为活跃 agent 创建:`{agent_id, mode: scores\|insights(默认 scores), evaluators[1..10](scores 模式), insights[1..3] ⊆ Builtin.Insight.FailureAnalysis\|UserIntent\|ExecutionSummary + clustering_frequencies[0..3] ⊆ DAILY\|WEEKLY\|MONTHLY(insights 模式), sampling_percentage 0.01–100(省略 → scores 10 / insights 100), session_timeout_minutes 1–1440(15), filters[0..5], description?, enable_on_create(true)}` → 201 行(`status` 从 `CREATING` 开始)。混用两类 → 422 `online_eval.mode_conflict`;行带 `mode`(由 `insights` 非空推导) |
| `GET` | `/api/eval/online/{config_id}` | 完整详情(`filters`、`data_source`、`execution_role_arn`) |
| `PATCH` | `/api/eval/online/{config_id}` | 仅 `owner=agent`:`description, sampling_percentage, session_timeout_minutes, filters` 之任意,加上本模式自己的分析字段——`evaluators`(scores)或 `insights` / `clustering_frequencies`(insights;完整列表,`[]` 频率即清除聚类);另一类 → 422 `online_eval.mode_conflict`,模式不可变。后端总是重发完整 `rule`(AWS 整体替换) |
| `POST` | `/api/eval/online/{config_id}/pause` · `/resume` | 切换 `executionStatus`(`agent` 与 `external`) |
| `DELETE` | `/api/eval/online/{config_id}` | 删除 AWS 配置并删掉 ledger 行(`agent` 与 `external`);结果日志组保留并在响应里给出 |
| `GET` | `/api/eval/online/{config_id}/results?range=1h\|6h\|24h\|7d` | Logs Insights 聚合结果日志组:每个 evaluator 的均值 / 计数 / 会话数 / 标签分布、按时间分桶的趋势、最近 ≤50 条带 judge 解释的记录、错误计数 |
| `GET` | `/api/eval/online/{config_id}/reports` | insights **报告** = 以该配置为数据源的批量评估:`{config_id, mode, reports[{batch_id, name, status, run_status, created_at, updated_at, insights, sessions{completed, failed, in_progress, total}, origin: aws_scheduled\|console, run_id, error}], aws_unavailable}` 最新在前(ListBatchEvaluations 失败时 `aws_unavailable: true`,仅有控制台行)——账本里的控制台运行(`EvalRun.dataset_name == "online:<config_id>"`)与 AWS 定期批次合并,后者靠 `GetBatchEvaluation.dataSourceConfig.onlineEvaluationConfigSource.onlineEvaluationConfigArn` 归属(只有摘要里没有 evaluators/insights 的批次才是候选;每个一次 Get,按 batch id 缓存)。任何归属均可读 |
| `POST` | `/api/eval/online/{config_id}/reports` | 立即出报告 `{range: 1h\|6h\|24h\|7d(24h)}` → 202 `{run_id, status, queue_position}`:仅 agent 持有的 insights 配置(否则 403 / 422);经有界运行队列提交 `EvalRun(mode=insights, dataset_name="online:<config_id>")`,批次使用 `onlineEvaluationConfigSource`——只覆盖该配置在窗口内**采样过**的会话,并继承配置的 insights(该数据源下显式传 evaluators/insights 会被 AWS 拒绝) |
| `GET` | `/api/eval/online/{config_id}/reports/{batch_id}` | `{batch_id, name, status, created_at, updated_at, time_range, sessions, insights{failures, userIntents, executionSummaries}, error_details}`(`parse_insights` 树,与运行页的 insights 运行相同);批次不是该配置的数据源时 404 `online_eval.report_not_found` |

错误码:`online_eval.no_telemetry`(400,agent 还没有遥测日志组,先跑一次会话)、`online_eval.evaluator_unsupported`(400)、
`online_eval.read_only`(403)、`online_eval.not_found`(404)、`online_eval.conflict`(409)、
`online_eval.workspace_not_bootstrapped`(400)、`online_eval.invalid_filter` / `online_eval.bad_range`(422)。
结果最早在会话空闲 `session_timeout_minutes` 之后出现;ENABLED 配置引用的自定义 evaluator 会被 AWS 锁定。

在线评分也出现在查看会话的地方:

| Method | Path | Result |
|---|---|---|
| `GET` | `/api/observability/sessions/{session_id}` | 会话详情附带 `online_scores: {configs[{config_id, config_name, owner, agent{id,name}?, records[{time, evaluator_id, level, score, label, explanation, trace_id}]}], total, unavailable, configs_exist}`——该会话在所有配置下的结果记录(agent 持有的块排在最前),用一条前缀 `SOURCE logGroups(namePrefix: ['/aws/bedrock-agentcore/evaluations/results/'])` 查询读取。失败降级:结果查询失败只置 `unavailable: true`,绝不影响追踪与对话记录;`configs_exist` 表示 workspace 是否有 agent 持有的配置(结果与配置都没有时 UI 隐藏该区块) |
| `GET` | `/api/observability/sessions/{session_id}/transcript` | 只取对话内容——返回 `{session_id, transcript}`，`transcript` 结构与会话详情相同，**不**跑 Logs Insights 查询（V2 评估结果抽屉用它）。可选 `agent_id` 用于归属没有账本记录的会话（其他 workspace 的 id 会被忽略）。没有 Memory 事件的评估会话从 runtime 的 otel-rt-logs 内容记录重建对话；Harness 读其 backing runtime 的日志组（`harness_<name>-<id>-DEFAULT`），关闭记忆的 Harness 只有这一份记录 |
| `GET` | `/api/overview/online-quality` | 「在线质量 · 24h」tile:`{range: "24h", mean, scores, sessions, agents, configs, evaluators[{evaluator_id, mean, count, polarity}], cached}`——对每个 (evaluator, agent 持有配置) 组合按计数加权求均值,lower-is-better 的 evaluator 取 `1 − mean`,因此 tile 始终「越高越好」;`evaluators[].mean` 保持原始值;`configs` 统计 workspace 内 agent 持有的配置数(账本),`agents` 统计有评分的 agent 数,因此「已配置但尚无评分」与「没有配置」可区分。按 workspace 缓存 120 秒并单飞,`force=true` 绕过;没有 agent 持有配置的 workspace 直接返回空载荷,不调用 AWS |

## 控制台可观测 API——会话即时评分 / Console Observability API

可观测会话详情(`/observability?session=<id>`)可以用 AgentCore 数据面 `Evaluate` API
**立刻**对会话打分。这是批量运行(异步、持久化、按数据集 / 会话 id / 时间窗口取范围)与
在线评估(抽样、持续)之外的第三种评分模式:

| 模式 | 调用 | 时延 | 结果存放 |
|---|---|---|---|
| 批量运行 | `StartBatchEvaluation`(`POST /api/eval/runs`) | 分钟级,轮询 | AWS 结果日志组 + 台账 `EvalRun` |
| 在线 | `CreateOnlineEvaluationConfig`(`POST /api/eval/online`) | 持续,judge 延迟约 10 分钟 | AWS 结果日志组,按会话读回 |
| **即时** | **`Evaluate`(`POST /api/observability/sessions/{id}/evaluate`)** | **同步,每个 evaluator 一次 judge 推理** | **仅响应体——不持久化** |

| Method | Path | Body / Result |
|---|---|---|
| `POST` | `/api/observability/sessions/{session_id}/evaluate` | Body `{evaluator_ids: string[](1..5,`Builtin.*` / `ThirdParty.*` / 自定义 id), range?: "1h"\|"6h"\|"24h"\|"7d"(默认 24h)}`。先用一条覆盖两种遥测布局的 Logs Insights 查询取回该会话的原始 span 记录(`filter ispresent(scope.name) and attributes.session.id = "<id>" \| fields @message \| sort @timestamp asc \| limit 2000`,非 JSON 行跳过),再按 evaluator 逐个、顺序调用 `evaluate(evaluatorId, evaluationInput={sessionSpans})`(每次 ≤10 条结果)。返回 `{session_id, range, span_count, results[{evaluator_id, evaluator_name, evaluator_arn, value, label, explanation, span_context{sessionId,traceId?,spanId?}, token_usage{input,output,total}, error_code, error_message}]}`。带 `error_code` 的结果是该 evaluator 的**部分失败**(仍返回该行,请求仍为 200)。仅会话级:没有 `evaluationTarget`,也没有 ground truth 参考输入。 |

错误:`observability.session_spans_missing`(409——所选范围内尚无该会话的 span 记录;
`detail.hint` 说明调用完成后 span 需要几分钟才会落地)、`observability.too_many_evaluators`(422)、
标准的 `validation.invalid_request`(422——超过 5 个 id、空列表、范围或 id 形状不合法)、
`aws.validation`(400——AWS 对不支持的 span 返回的 `ValidationException`)、
`observability.query_failed`(502——Logs Insights 失败/超时)。结果**绝不写入台账**;
可随时重跑(每次运行按 evaluator 各计一次 judge 推理)。

## 控制台 Skill Lab API——评估结果 / Console Skill Lab API

Skill Lab 评估详情页（`/skill-lab?view=eval&job=<id>`）从 CLI 的 `out/results.json` 读取一个
作业的逐任务评审行；权威来源是这个文件而不是台账，每次请求都会重新读取。路由本身不变，
其响应新增了一层经过校验的 token 用量投影。同一张表也收录了保存审阅后 taskgen 结果的两条路由。

| 方法 | 路径 | 结果 |
|---|---|---|
| `GET` | `/api/skill-lab/jobs/{job_id}/results` | eval 作业返回 `{summary, rows[]}`（taskgen 作业则返回 `{type: "taskgen", count, tasks, summary}`）。`summary` = `{tasks, passed, invalid, pass_rate, soft_mean, duration_s, judge_prerequisite_missing[], token_usage}`；每行 = `{id, task_type, hard, soft, score_valid, duration_s, judge_status, judge_reason, judge_error, error, judge_prerequisite, response（摘录）, artifacts[{path,size}], usage, judge_usage, token_usage}`。不检查作业状态：CLI 一写出文件（在进程退出之前）就会返回；此前返回 `404 skill_lab.results_pending`（对在评分阶段之前就结束的作业，这也是最终答复）。 |
| `POST` | `/api/skill-lab/jobs/{job_id}/import-taskset` | 把一个**已成功的 taskgen** 作业生成的任务保存为新的 single 模式任务集。请求体 `{name, tasks?}`；`tasks` 是审阅后的选择——`[{index, id?, question?, rubric?, task_type?}]`（最多 `MAX_TASKS_PER_SPLIT` 项），`index` 是该行在作业 `generated_tasks.json` 中的位置（严格整数，不接受 bool/字符串/浮点强转），四个可选字段是唯一接受的作者编辑；未知键（如 `files`、`attachments`）返回 `422 validation.invalid_request`。未出现在 `tasks` 中的行被排除，省略的字段保留生成值，`task_type: ""` 清除该字段；服务端从作业快照重建 `files`/附件。省略 `tasks` 或传 `null` → 原样保存全部生成行（旧行为）。`201 {job, taskset}`。所有错误都发生在任何写入之前：`400 skill_lab.not_a_taskgen_job`、`409 skill_lab.job_not_finished` / `skill_lab.already_imported` / `skill_lab.results_missing`、`422 skill_lab.taskgen_empty_selection`（`tasks` 为空）、`422 skill_lab.taskgen_bad_selection`（索引越界或重复）、`422 skill_lab.taskgen_duplicate_id`（编辑后 id 不唯一）、`422 skill_lab.taskset_invalid`（校验器子进程，例如不安全的 id）、调用方 workspace 之外返回 `404 skill_lab.job_not_found`。 |
| `POST` | `/api/skill-lab/jobs/{job_id}/apply-expansion` | 把一个**已成功的扩展**作业生成的任务追加到其目标任务集/分割。请求体可选：`{tasks?}`，选择的形状与规则同 `import-taskset`；无请求体或无 `tasks` 时追加全部生成行。**编辑后的** id 会对照目标任务集当前的每个分割重新检查（`409 skill_lab.expansion_conflict` 列出冲突的 id），其他分割保持不变，写入为经过校验的全量替换。`200 {job, taskset}`；`400 skill_lab.not_an_expansion_job`，以及与导入相同的 `409`/`422`/`404` 族。两条路由都不会改动作业的 `generated_tasks.json` 与附件快照。 |
| `GET` | `/api/skill-lab/jobs/{job_id}/artifacts?path=` | 作业的 `out/` 目录树，不限状态。目录 → `{kind: "dir", path, dirs[], files[{name, size}]}`；文件 → `{kind: "text", path, size, truncated, content}`（UTF-8，`content` 上限 512 KB，超出部分以 `truncated: true` 标记）或 `{kind: "binary", path, size}`（含 NUL 字节／无法解码）。尚未创建 `out/` 的作业（排队中，或 CLI 尚未写出任何内容的运行中作业）返回**空的根目录列表**而不是错误；不存在或已消失的子路径返回 `404 skill_lab.artifact_not_found`。绝对路径、`~`、反斜杠、NUL，以及（含符号链接）解析到 `out/` 之外的任何路径返回 `400 skill_lab.bad_path`。 |
| `GET` | `/api/skill-lab/jobs/{job_id}/artifacts/raw?path=` | 以下载形式返回文件的精确字节（`Content-Disposition` 带文件名），永不截断；目录或不存在的文件返回 `404 skill_lab.artifact_not_found`，同样受 `400 skill_lab.bad_path` 守卫。两条路由对调用方 workspace 之外的作业均返回 `404 skill_lab.job_not_found`。 |

**`token_usage`（新增）。** 逐行：`{target: <record>, judge: <record>}`，其中 record 为
`{status: "reported"|"missing"|"malformed", input, cache_write, cache_read, output,
unattributed}`——每个计数是整数或 `null`。原始生产者字段 `usage` / `judge_usage` 原样保留在行上；
仅当文件里带有 `NaN` / `Infinity` 字面量（`json.loads` 会接受它们）时，这些值以字符串 `"nan"` /
`"inf"` / `"-inf"` 输出以保证可序列化，文件本身绝不改写。
summary 上：`{scope: "reported", target: <side>, judge: <side>}`，其中 side 为
`{rows, reported_rows, missing_rows, malformed_rows, reports_complete, complete, input,
cache_write, cache_read, output, unattributed, counter_rows{<counter>: n},
counter_complete{<counter>: bool}}`。

语义：`null` 表示*未知*（没有任何一行上报该计数），绝不是零。评审生产者只上报 `input`/`output`，
因此评审侧的 `cache_*` 恒为 `null`。`unattributed` 是 transcript 只以 `total` 形式上报、超出分项
之和的部分（codex 形态——当所有分项都是正总数之下的零占位符时，分项按 `null` 上报）；只上报了
`total: 0` 也算一次上报（`unattributed: 0`，分项为 `null`），不算缺失。格式异常的计数（bool、
负数、NaN/inf、小数、非数值）会被丢弃而不是折算；该行其余有效计数照常计入，且该行计入
`malformed_rows`。score 无效的行（`score_valid: false`）用量照常求和，但不进入 `pass_rate` /
`soft_mean`。完整性分两种：`reports_complete` 是上报覆盖度（`reported_rows == rows` 且没有格式
异常行）；`complete` 是拆分完整性（上报完整，且凡有任何一行上报过的计数都被每一行上报了）。只有
部分行上报的计数是**部分求和**：`counter_rows[k] < rows`、`counter_complete[k] == false`，控制台
在该单元格标出 `k/n`（例如一行 claude 加一行 codex 仅总数，`input` 只来自 2 行中的 1 行，即便两行
都已上报，`complete` 也为 false）。没有任何一行上报的计数是未知，本身不会让拆分变成部分。`scope`
恒为 `reported`：这是对上报了用量的任务的观测统计——不是计费总额，也不做任何费用估算。在用量
采集之前写出的旧结果，每一侧都表现为 `missing_rows == rows` 且计数全为 `null`。

## 控制台账户 API / Console Accounts API

`/api/auth/*` 守住控制台入口，`/api/users/*` 管理其背后的账户。两组接口都不触碰 AWS。详见
[architecture.zh-CN.md](architecture.zh-CN.md)「控制台认证与账户」一节。

| 方法 | 路径 | 鉴权 | 结果 |
|---|---|---|---|
| `GET` | `/api/auth/status` | 开放 | `{auth_required, authenticated, registration_enabled, registration_requires_approval, username, role, email, account_expires_at, permissions, builtin}`——身份字段在认证前为 null（`permissions` 为 `[]`） |
| `POST` | `/api/auth/login` | 开放 | 设置 `launchpad_session` cookie（12 小时，且不超过账户有效期）并回显身份 |
| `POST` | `/api/auth/register` | 开放 | `201`——创建一个 `member` 账户；默认 `status=pending` 且 `expires_at=null`，直到管理员批准，之后有效期为 `auth_registration_valid_days`（默认 7 天） |
| `POST` | `/api/auth/logout` | 会话 | 清除 cookie |
| `POST` | `/api/auth/password` | 会话 | `{current_password, new_password}`——已登录账户修改自己的密码（与注册相同的密码规则）。内置管理员的新密码以哈希覆盖值保存，取代配置中的 `auth_password`。该账户的其他会话全部结束，调用方的 cookie 会重新签发。`auth.invalid_current_password`（400）、`auth.not_enabled`（400，未启用登录） |
| `GET` | `/api/users?q=&status=all\|pending\|active\|expired\|disabled&role=&project=&group=&limit=&offset=` | 管理员 | 分页账户列表，带派生的 `state` / `days_remaining`；另有 `builtin`——内置管理员（id `builtin-admin`，`password_source: config\|console`），在符合搜索与状态筛选时返回，不计入 `items` / `total`。每个条目带 `projects[{id, name, status, is_default, role, sources[{source, role_key}]}]`（直接与经项目组的成员资格；`role` 为直接角色，否则为组角色）与 `groups[{id, name, role, archived}]`。`role` 按全局角色筛选（未知角色返回 `422 users.invalid_role`）；`project` 只保留该项目的成员（直接或经其项目组，不含管理员旁路）；`group` 只保留该项目组的成员。也接受别名 `project_id` / `group_id`。项目或项目组不存在时返回 404。带 `project` 或 `group` 筛选时不返回内置管理员，因为它没有成员资格 |
| `POST` | `/api/users` | 管理员 | `201`——创建一个**立即生效**的账户（无需审批）：`username`、`email`（与注册相同的规则，包括内置管理员的保留名和邮箱域名）、`password`（省略 = 自动生成，并以 `generated_password` 一次性返回）、`role`（默认 `member`，可选 `operator`、`admin`）、`permissions`、`workspaces`（默认 `["default"]`；`admin` 必须为空），以及 `valid_days`（1-3650）、未来的 `expires_at`、`never_expires: true` 三者至多其一（都不传 = `auth_registration_valid_days`）。可选 `projects[{project_id, role_key, overrides?}]`（直接成员资格：内置模板、该项目的自定义角色，或默认项目中的 `inherit`）与 `groups[{group_id, role_key}]`（仅内置模板，不带覆盖）。返回 `GET /api/users` 的条目形状。`users.invalid_workspaces` / `users.admin_no_grants` / `users.invalid_role`（400）、`users.invalid_expiry`（422）、`auth.username_taken` / `auth.email_taken`（409） |
| `GET` | `/api/users/stats` | 管理员 | 汇总数据，包括 `pending` 审批队列、`expiring_soon`（≤3 天）、7 天内的注册/登录计数、14 天注册序列、邮箱域名排行 |
| `PATCH` | `/api/users/{id}` | 管理员 | 以下任意字段：`status`（`pending`\|`active`\|`disabled`；对待审批账户设为 `active` 即批准并启动其有效期）、`role`、`extend_days`、`expires_at`（`null` = 永不过期）、`password`（`null` = 生成并一次性返回）、`permissions`（`{permission_key: bool}`，`null` = 全部授予）、`workspaces`（整体替换该账户的 workspace 授权，`null` 清空）、`projects` / `groups`（整体替换直接的项目与项目组成员资格，条目形状同创建；`null` 清空；默认项目的 `inherit` 行继续跟随 `workspaces`；不带 `overrides` 的条目保留原行的覆盖） |
| `GET` | `/api/users/{id}` | 管理员 | 单个账户，形状同 `GET /api/users` 的条目（授权、项目、项目组），供用户详情页使用。`builtin-admin` 返回其内置投影 |
| `GET` | `/api/users/{id}/access` | 管理员 | 该账户的有效访问：即该账户自己看到的 `GET /api/me/access` 形状，另加 `state`。管理员的访问是经旁路的全部项目；`state` 表示该账户能否登录使用这些访问 |
| `POST` | `/api/users/bulk/projects` | 管理员 | 用户列表的“加入项目”：请求体 `{user_ids[1–200], project_id, role_key, overrides?}` → `{project_id, role_key, added[], updated[], unchanged[], skipped[{user_id, reason: "admin"}]}`。列出的每个账户都获得一条 `role_key` 的直接成员资格；已有成员的角色被替换（发送了 overrides 时覆盖也被替换）。不移除任何人，也不改变 Workspace 授权。管理员被跳过，因为他们凭角色即可访问所有项目。任一 id 不存在则整批拒绝（`404 users.not_found`，`detail.unknown`）。每项变更记入 `project.members.add` / `.update`。列表为空或超出上限返回 `users.invalid_bulk`，此外还有 `PUT /api/projects/{id}/members` 的错误（`project.role_unknown`、`project.sod_conflict` 等） |
| `DELETE` | `/api/users/{id}` | 管理员 | 删除该账户 |
| `POST` | `/api/users/{id}/sessions/revoke` | 管理员 | 强制登出 → `{ok: true, user_id, session_epoch}`。cookie 是无状态的，因此这里递增该账户的 `session_epoch`；它是每个 cookie 所带戳记的一部分，此前签发的所有会话在下一次请求时返回 `401 auth.required`。记入结构事件 `user.sessions.revoke`。对 `builtin-admin` 返回 `users.builtin_immutable`（400）：它的会话在其密码修改时结束 |
| `GET` | `/api/admins` | 管理员 | 管理员页面的列表：`{items[…], total, can_sign_in}`。登录门开启时 `items` 以内置管理员开头（`builtin: true`、`can_sign_in: true`、`can_demote: false`），其后是所有 admin 角色账户（最早创建的在前），形状同 `GET /api/users` 的条目，另加 `builtin: false`、`can_sign_in`（已激活且未过期）、`last_signable`（剩下的唯一一位可登录管理员）与 `can_demote`（对调用者本人和 `last_signable` 为 false）。顶层 `can_sign_in` 是当前可登录的管理员数，含内置管理员 |

对 `builtin-admin`，`PATCH` 只接受 `password`（设置，或 `null` = 自动生成），`DELETE` 会被拒绝（`users.builtin_immutable`，400）。会导致没有任何管理员能够登录的 `PATCH` / `DELETE` 返回 `users.last_admin`（409）；内置管理员计入在内，所以启用登录时它只是兜底。每次账户写操作都记入 `audit_events`（不属于任何工作区的行，只记录修改的字段名，从不记录密码）。

**管理员。** 授予或撤销 admin 角色的 `PATCH {role}` 会在 `user.update` 之外再记入结构事件
`admin.grant` / `admin.revoke`。任何人都不能撤销自己的管理员角色：这样的 `PATCH` 返回
`users.self_demote`（409）；若它同时会移除最后一位可登录的管理员，则先返回 `users.last_admin`（409）。

注册错误码：`auth.registration_disabled`（400，认证门未开启或注册已关闭）、
`auth.invalid_username` / `auth.invalid_email` / `auth.email_domain_blocked` / `auth.weak_password`（400）、
`auth.username_taken` / `auth.email_taken`（409）。

登录错误码：`auth.invalid_credentials`（401），以及在提交的凭据本身正确之后的
`auth.account_pending` / `auth.account_disabled` / `auth.account_expired`（401）。

创建 / `PATCH` 时的成员资格错误：`users.invalid_groups`（400，项目组条目带了 `overrides`）、`group.role_unknown`（400，不是内置模板）、`group.not_found`（404）、`group.archived`（409），以及 `PUT /api/projects/{id}/members` 的项目成员错误。

会话与角色错误：`auth.required`（401——cookie 缺失、被篡改或已过期，也包括账户此后被禁用、过期或删除）、
`auth.forbidden`（403——member 会话访问 `/api/users*`）、`users.not_found`（404）。

## 控制台项目 API / Console Projects API

**项目**把人员、Agent 及其派生行组织在一个或多个 Workspace 中。成员通过项目角色进入项目，该角色授予的权限键
决定其在项目内可做的事。`/api/projects*`、`/api/role-templates` 与 `/api/me/access` 属于全局路由：项目跨越多个
Workspace，因此它们既不读取 `X-Workspace` 也不读取 `X-Project`，所指的项目就是路径中的那个。调用者看不到的项目
id 返回 `404 project.not_found`，从不返回 403，因为项目是否存在本身就可能敏感。每一次改变“谁能访问什么”的写入，
都在同一事务中递增 `access_epoch`，因此每个进程的下一次请求都能看到变化。这些写入还会记入结构事件
（`project.create`、`project.update`、`project.archive`、`project.unarchive`、`project.workspaces.bind` /
`.unbind`、`project.members.add` / `.update` / `.remove`、`project.roles.upsert` / `.delete`、
`project.transfer`）。

**权限标记。** 除 `成员`、`管理员` 与 `perm:<key>` 之外，路由表（`core/route_policy.py`）还使用 `pperm:<key>`。
它是项目范围的权限，依据调用者在路径 `{project_id}` 所指项目中的角色解析，从不依据 `X-Project`：

- 非成员返回 `404 project.not_found`。
- 有效权限键中没有 `<key>` 的成员返回 `403 auth.permission_required`（`detail.permission`、`detail.project_id`）。
- 管理员直接通过以上两项检查。
- 随后，对已归档项目的非读取请求返回 `409 project.archived`，管理员也不例外。

在 Workspace 范围的路由上，`perm:<key>` 同样按请求所在项目的有效权限键检查，而不是账户自身的权限键。
请求所在项目由 `X-Project` 指定；未带该请求头时，取调用者在该 Workspace 中唯一的项目，或该 Workspace
的默认项目。解析失败的情况：

- `400 project.header_required`（`detail.available`）：调用者在该 Workspace 中有多个项目，且都不是默认项目。
- `403 project.none`：调用者不是该 Workspace 中任何项目的成员。
- `409 project.unbound`：管理员发起请求，但该 Workspace 没有项目。
- `404 project.not_found`：所指项目未绑定到该 Workspace，或调用者无法访问。
- `409 project.archived`：对已归档项目的非读取请求。

成员在项目中的**有效权限键**由两部分组成：先取直接角色与项目所属组角色的并集，再叠加直接成员行的
`overrides`（`{key: bool}`）。内置角色模板（代码常量，见 `GET /api/role-templates`）：

| 模板 | 权限键 |
|---|---|
| `project_owner` | `project.members`、`project.audit`、`project.share`、`project.use`、`project.write` 与构建类权限键（`agents.*`、`eval.run`、`promotion.request`、`identity.manage`、`identity.grant`、`memory.manage`、`criteria.manage`）。刻意不含批准与签署类权限键 |
| `developer` | `project.use`、`project.write` 与成员的默认权限键 |
| `biz_owner` | `project.use`、`project.write`、`eval.run`、`criteria.manage`、`criteria.sign`、`golden.admit`、`judge.calibrate`、`identity.grant` |
| `release_ops` | `project.use`、`project.write`、`mcp.approve` 与 operator 的默认权限键（含 `promotion.approve`、`waiver.approve`、`release.sign`） |
| `sme` | `project.use`、`identity.grant` |
| `tool_steward` | `project.use`、`mcp.approve`：审批 MCP 服务器的工具清单，且不持有任何可以自批的权限键 |
| `viewer` | 无（只读） |
| `inherit` | 账户自身的全局权限键，加上 `project.use` / `project.write`；账户持有 `promotion.approve` 时另加 `mcp.approve`；只存在于默认项目 |

项目级权限键为：`project.use`（对话 / 调用、反馈、标注、自己的会话）、`project.write`（项目内的普通写入）、
`project.members`（管理本项目的成员与自定义角色）、`project.audit` / `project.share`（留待第三阶段），以及
`mcp.approve`（审批托管 MCP 服务器的工具清单，部署者本人除外；见“控制台 MCP 工具审批 API”）。`mcp.approve`
是项目级权限键而非 Agent 权限键，因此 `/api/auth/status` 与 `users.permissions` 从不列出它。

**职责合并。** 在同一项目中同时持有一对职责分离权限键两端的成员（`agents.deploy` + `release.sign`、
`criteria.manage` + `criteria.sign`）会在成员视图中被标出（`sod_conflicts`）。项目设置了仅限管理员修改的
`settings.sod_strict` 时，会让某人持有这样一对权限键的写入改为被拒绝：`409 project.sod_conflict`
（`detail.conflicts[{user_id, username, pairs}]`）。这覆盖成员与角色的写入、项目组成员与项目组项目的变更，以及
开启 `sod_strict` 本身。对象级检查（不能批准或签署自己的请求）仍是最后一道防线。

| 方法 | 路径 | 角色 | 结果 |
|---|---|---|---|
| `GET` | `/api/me/access` | 成员 | 调用者的访问图，是项目切换器与控制台 `can()` 的数据来源：`{username, user_id, role, is_admin, can_manage_structure, workspaces[{workspace_id, name, default_project_id, bound_projects, projects[{id, name, status, is_default, is_workspace_default, permissions[], role_sources[{source: direct\|group:<id>, role_key}], bypass, department_id, department_name}]}]}`。管理员看到所有 Workspace 与所有已绑定项目，并持有全部权限键（非成员的项目 `bypass: true`）。`default_project_id` 是不带请求头的请求会使用的项目。`department_id` / `department_name`（项目没有部门时为 null）用于给切换器的选项分组。`bound_projects` 是该 Workspace 绑定的活跃项目数（无论调用者能否访问）：切换器隐藏（只有一个可达项目）但 `bound_projects > 1` 时，V3 顶栏以只读 chip 显示当前项目 |
| `GET` | `/api/role-templates` | 成员 | 静态数据：`{templates[{key, permissions[]}], inherit: "inherit", agent_keys[], project_keys[]}` |
| `GET` | `/api/projects?department_id=&group_id=&status=&workspace_id=&q=` | 成员 | `{projects[project]}`：管理员看到所有项目，其他人只看到自己的项目；默认项目在前，其余按名称排序。`status` 为 `active`、`archived` 或 `all`；`q` 匹配 id 或名称 |
| `POST` | `/api/projects` | 管理员 | 请求体 `{id, name（1–64）, description?（≤ 2000）, department_id?, group_id?, workspaces[1–50], owners?[users.id]（≤ 50，以 `project_owner` 加入）, settings?{resource_visibility: shared\|isolated, sod_strict: bool}}` → `201` 项目详情。`id` 须匹配 `^[a-z0-9][a-z0-9-]{1,31}$`。提交后会尽力为每个新绑定的 Workspace 生成本项目自己的 Skill Lab 示例任务集 |
| `GET` | `/api/projects/{project_id}` | 成员（非成员 404） | 项目详情：`{id, name, description, department_id, group_id, status: active\|archived, is_default, settings, created_by, created_by_name, created_at, updated_at, archived_at, workspaces[binding], member_count, agent_count, my_permissions[]}` |
| `PATCH` | `/api/projects/{project_id}` | `pperm:project.members` | `{name, description, department_id, group_id, settings}` 中任意字段 → 详情。`name`、`department_id`、`group_id` 仅限管理员修改（`403 auth.forbidden`，`detail.fields`）。`settings` 合并进已存设置（`null` 删除该键）：所有者可改 `resource_visibility`，只有管理员可改 `sod_strict`。把项目移到另一个组时会重新评估 API key |
| `POST` | `/api/projects/{project_id}/archive` | 管理员 | → 详情 + `{links_revoked, keys_disabled}`。项目变为只读：其分享链接被撤销，创建者已无法访问该项目的 key 被停用。对已归档项目再次归档不做任何改变（`0` / `0`）。默认项目返回 `409 project.default_locked` |
| `POST` | `/api/projects/{project_id}/unarchive` | 管理员 | → 详情；恢复可写。已撤销的链接与已停用的 key 保持原状 |
| `GET` | `/api/projects/{project_id}/workspaces` | 成员（非成员 404） | `{workspaces[{workspace_id, name, region, tier, is_workspace_default, enabled_by, enabled_at}]}` |
| `PUT` | `/api/projects/{project_id}/workspaces` | 管理员 | 请求体 `{bind[], unbind[], make_default[]}`（各 ≤ 50）→ `{workspaces[binding], bound[]}`，`bound` 列出新绑定的 Workspace，它们会获得 Skill Lab 示例。`make_default` 把本项目设为该 Workspace 的默认项目（不带请求头的请求所指的项目；每个 Workspace 一个）。以下情况拒绝解绑：默认项目、Workspace 默认绑定，以及该 Workspace 仍持有本项目的任何行（`detail.rows` = `{table: count}`） |
| `GET` | `/api/projects/{project_id}/members` | 成员（非成员 404） | `{members[{user_id, username, email, account_role, status, direct{role_key, overrides, added_by, added_at} \| null, group{group_id, role_key} \| null, sources[{source, role_key}], bypass, permissions[], sod_conflicts[[key, key]]}], sod_strict}`：直接成员与组成员及其有效权限键。`sod_conflicts` 列出该成员同时持有两端的职责分离权限键对；`sod_strict` 为项目的设置。下面的 `PUT` 也返回这两个字段。管理员看到完整 `email`，其他人看到掩码形式（`z***@corp.com`）。`bypass` 标记 admin 角色账户，它本来就持有全部权限键 |
| `PUT` | `/api/projects/{project_id}/members` | `pperm:project.members` | 请求体 `{upsert[{user_id, role_key, overrides?{key: bool}}]（≤ 500）, remove[user_id]（≤ 500）}` → `{added[], updated[], removed[], members[…]}`。`role_key` 可以是内置模板、本项目的自定义角色，或 `inherit`（仅默认项目）。只写直接成员行，来自组的访问在组上管理。除管理员外，任何人都不能修改自己的成员资格。被移除或权限收窄的成员在本项目中的 API key 会被停用 |
| `DELETE` | `/api/projects/{project_id}/members/{user_id}` | `pperm:project.members` | `204`，即 `remove` 的单成员形式 |
| `GET` | `/api/projects/{project_id}/candidates?q=` | `pperm:project.members` | `{candidates[{user_id, username, email（掩码）, is_member}]}`：用户名或邮箱匹配 `q` 的已激活非管理员账户，最多 20 个 |
| `GET` | `/api/projects/{project_id}/roles` | 成员（非成员 404） | `role-templates` 的形状，另加 `inherit_available`（仅默认项目）与 `custom[{key, name, permissions[], based_on, members, updated_at}]` |
| `PUT` | `/api/projects/{project_id}/roles/{role_key}` | `pperm:project.members` | 请求体 `{name（1–64）, permissions[]（≤ 64 个已知权限键）, based_on?（内置模板）}` → 角色视图。创建或替换自定义角色；`role_key` 须匹配 `^[a-z][a-z0-9_]{1,31}$` 且不是内置名称。持有该角色的成员会立即重新评估，其 API key 也随之重新评估 |
| `DELETE` | `/api/projects/{project_id}/roles/{role_key}` | `pperm:project.members` | `204`；仍有成员持有该角色时拒绝 |
| `POST` | `/api/projects/{project_id}/transfer` | 管理员 | 请求体 `{kind: "agent"（默认；第一阶段唯一支持的类型）, resource_id, to_project_id}` → `{agent_id, from_project_id, to_project_id, rows{table: count}}`。一次事务内只改台账：该 Agent、所有由它（递归）派生的行及其任务被改标到目标项目；目标项目须为活跃状态，且已绑定到该 Agent 所在的 Workspace。源项目中限定到该 Agent 的 API key 会去掉它，因此不再限定任何 Agent 的 key 会被停用。在目标项目中记入 `project.transfer` |

错误码（未注明者均为 `project.*`）：

- 400：`invalid_id`、`workspaces_required`、`binding_conflict`（同一请求中既绑定又解绑）、`member_conflict`（同一请求中既 upsert 又 remove）、`role_unknown`、`override_unknown`、`role_invalid_key`、`setting_invalid`、`transfer_kind`（`detail.supported`）、`transfer_same`。
- 400 或 403：`setting_forbidden`（`detail.keys`）。所有者修改仅限管理员的设置时为 403，未知设置为 400。
- 403：`self_membership`、`auth.forbidden`、`auth.permission_required`。
- 404：`not_found`、`member_not_found`（无直接成员行）、`role_not_found`、`department.not_found`、`group.not_found`、`workspace.not_found`、`user.not_found`（`upsert` 中的 `user_id` 不存在）、`agent.not_found`（转移时该 Agent 不在源项目中，或已删除）。
- 409：`exists`、`archived`、`default_locked`、`workspace_default`、`workspace_in_use`、`not_bound`、`member_from_group`（`detail.group_id`）、`role_in_use`（`detail.members`）、`sod_conflict`（`detail.conflicts`）、`department.archived`、`group.archived`、`transfer_platform`（系统预置）、`transfer_busy`（该 Agent 的任务仍在排队或运行）、`transfer_linked`（运行中的金丝雀把它与另一个 Agent 配对；`detail.canary_id`）。

### 项目组与部门 / Project groups and departments

**部门**是项目与项目组上的组织标签：它不授予任何访问，V3 项目切换器按它给选项分组。**项目组**跨 Workspace 汇集多个项目，并在组内每个项目中授予其成员一个内置角色模板（一个项目最多属于一个项目组）。成员在某项目中的有效权限键是组角色与直接角色的并集，再叠加直接成员行的 `overrides`。两组接口都是全局路由（`/api/project-groups*`、`/api/departments*` 既不读取 `X-Workspace` 也不读取 `X-Project`）。除成员可读的 `GET /api/departments` 外，所有路由仅限管理员。项目组与部门 id 遵循项目 id 的规则（`^[a-z0-9][a-z0-9-]{1,31}$`，否则 `400 group.invalid_id` / `department.invalid_id`）。改变“谁能访问什么”的项目组写入会递增 `access_epoch`，并停用创建者已失去项目访问的 API key。写入记入结构事件：`department.create` / `.update` / `.archive` / `.unarchive` / `.delete`、`group.create` / `.update` / `.archive` / `.unarchive`、`group.members.add` / `.update` / `.remove`、`group.projects.add` / `.remove`。

| 方法 | 路径 | 角色 | 结果 |
|---|---|---|---|
| `GET` | `/api/departments?status=active\|archived\|all` | 成员 | `{departments[{id, name, description, archived, archived_at, created_by, created_at, updated_at, project_count, group_count}]}`；`status` 默认 `active`（其他值返回 `400 structure.invalid_status`） |
| `POST` | `/api/departments` | 管理员 | 请求体 `{id, name（1–64）, description?（≤ 2000）}` → `201` 部门。`409 department.exists` |
| `PATCH` | `/api/departments/{department_id}` | 管理员 | `{name, description}` 中任意字段 → 部门 |
| `POST` | `/api/departments/{department_id}/archive` / `…/unarchive` | 管理员 | → 部门。已归档的部门不能再分配给项目或项目组（`409 department.archived`） |
| `DELETE` | `/api/departments/{department_id}` | 管理员 | `204`，仅当没有项目和项目组引用它时：否则 `409 department.in_use`（`detail.projects`、`detail.groups`） |
| `GET` | `/api/project-groups?status=&department_id=` | 管理员 | `{groups[{id, name, description, department_id, archived, archived_at, created_by, created_at, updated_at, projects[{id, name, status}], member_count}]}`，按名称排序 |
| `POST` | `/api/project-groups` | 管理员 | 请求体 `{id, name（1–64）, description?, department_id?}` → `201` 项目组详情。`409 group.exists` |
| `GET` | `/api/project-groups/{group_id}` | 管理员 | 项目组详情：列表形状另加 `members[{user_id, username, email, account_role, status, role_key, added_by, added_at, sod_conflicts[{project_id, pairs, strict}]}]`，`sod_conflicts` 列出该成员在组内哪些项目中合并了职责 |
| `PATCH` | `/api/project-groups/{group_id}` | 管理员 | `{name, description, department_id}` 中任意字段 → 详情；均不改变访问 |
| `DELETE` | `/api/project-groups/{group_id}` | 管理员 | 归档该项目组 → 详情 + `{keys_disabled}`。项目组授予的访问立即停止；直接成员保留其访问，项目保留 `group_id`，因此恢复归档后访问原样恢复 |
| `POST` | `/api/project-groups/{group_id}/unarchive` | 管理员 | → 详情。在 `sod_strict` 下，若有成员会因此重新合并职责则拒绝 |
| `PUT` | `/api/project-groups/{group_id}/members` | 管理员 | 请求体 `{upsert[{user_id, role_key}]（≤ 500）, remove[user_id]（≤ 500）}` → `{added[], updated[], removed[]}` + 详情。`role_key` 只能是内置模板：自定义角色只属于单个项目（`400 group.role_invalid`，`detail.templates`）。`400 group.member_conflict`（同一请求中既 upsert 又 remove）、`404 group.member_not_found`、`404 user.not_found`、`409 group.archived` |
| `PUT` | `/api/project-groups/{group_id}/projects` | 管理员 | 请求体 `{add[]（≤ 200）, remove[]（≤ 200）}` → `{added[], removed[]}` + 详情。已在另一个活跃项目组中的项目必须先从那里移除（`409 group.project_in_other_group`，`detail.group_id`）。移出项目会撤销项目组在那里授予的访问。`400 group.project_conflict`、`404 group.project_not_found`（不在本组中）、`404 project.not_found`、`409 group.archived`（向已归档的组添加） |

## 控制台网络配置 API / Console Network Profiles API

**网络配置**是由管理员维护的 Workspace VPC 设置，项目的 Agent 可以运行在其中。Agent 规格以
`network: {profile_id}` 引用它，部署器再把它解析为该配置的子网与安全组。每个项目在每个 Workspace 中有一个
**允许集合**，其中最多一个为默认。不在请求所在项目允许集合中的配置，读取结果与不存在的 id 完全相同
（`404 network_profile.not_found`）。管理员看到该 Workspace 的所有配置。新配置为 `status: unchecked`，直到预检（`POST …/{profile_id}/check`）填写
`check_results` / `checked_at` / `status`。只有预检与探测会调用 AWS。这些路由属于 Workspace 范围（`X-Workspace`）。读取在请求所在项目内进行；写入与按项目的允许集合仅限
管理员且不进入项目范围，因此允许集合路由在路径中写明项目。写入会记入 Workspace 级审计事件：
`network_profile.create`、`.update`、`.archive`、`.project_set`、`.check`、`.probe` 与 `.settings`。

配置为 `{id, workspace_id, name, vpc_id, subnet_ids[], security_group_ids[], purpose: agents|tools|both,
internet: none|nat, reachable[{cidr|host, port, note?}], status, check_results, checked_at, legacy, created_by,
created_at, updated_at, archived_at, allowed, is_default}`。各字段含义如下：

- `id` 为 `np-<12 位十六进制>`；由旧版内联规格归档而来的配置为 `legacy-<hash8>`（`legacy: true`）。
- `allowed` 与 `is_default` 相对于请求所在项目。
- 管理员视图另加 `projects[{project_id, is_default}]`（所有允许使用它的项目）与 `agents`（任意项目中引用它的存活 Agent 数）。

| 方法 | 路径 | 角色 | 结果 |
|---|---|---|---|
| `GET` | `/api/network-profiles?include_archived=` | 成员 | `{profiles[…]}`，按名称排序。成员得到当前项目的允许集合（没有则为空）。管理员得到该 Workspace 的所有存活配置；`include_archived=true` 时也包括已归档的配置 |
| `GET` | `/api/network-profiles/{profile_id}` | 成员 | 单个配置；对成员而言，仅当它在项目的允许集合中时才返回 |
| `POST` | `/api/network-profiles` | 管理员 | 请求体（`extra="forbid"`）`{name（1–64）, vpc_id（vpc-…）, subnet_ids（2–8 个 subnet-…；AgentCore 需要两个可用区）, security_group_ids（1–5 个 sg-…）, purpose?（默认 agents）, internet?（默认 none）, reachable?（≤ 50 个 `{cidr` 或 `host, port 1–65535, note? ≤ 200}`）}` → `201` 配置（`status: unchecked`、`projects: []`、`agents: 0`）。重复的 id 会被去掉；名称在该 Workspace 的存活配置中唯一 |
| `PATCH` | `/api/network-profiles/{profile_id}` | 管理员 | 创建字段的任意子集 → 管理员视图。修改 `vpc_id`、`subnet_ids`、`security_group_ids` 或 `internet` 会让配置回到 `unchecked`，并清空 `check_results` / `checked_at` |
| `DELETE` | `/api/network-profiles/{profile_id}` | 管理员 | `204`。归档该配置（行保留，因为快照可能仍引用它），并把它从所有项目的允许集合中移除。仍有存活 Agent 引用它时拒绝，因为其 runtime 的 ENI 位于这些子网中 |
| `GET` | `/api/network-profiles/projects/{project_id}` | 管理员 | `{project_id, workspace_id, profiles[…], default_profile_id}`：该项目在请求所在 Workspace 中的允许集合 |
| `PUT` | `/api/network-profiles/projects/{project_id}` | 管理员 | 请求体 `{profile_ids[]（≤ 50）, default_profile_id?}` → 同 GET 形状。替换该项目在此 Workspace 中的允许集合及其默认值 |
| `POST` | `/api/network-profiles/{profile_id}/check` | 管理员 | 预检（设计 projects §12.4）→ 管理员视图，带 `check_results[{check, severity: ok\|warn\|block, message, details}]`、`checked_at` 与 `status`（`blocked` > `warn` > `ok`）。通过 Workspace 自己的只读 `ec2:Describe*` 调用读取 VPC：子网存在、属于该 VPC 且位于至少两个受支持的可用区、有空闲 IP；安全组存在且允许 443 出站；VPC DNS；`internet: nat` 时的 NAT 路由；网络 ACL；AgentCore 需要的接口 / 网关端点（只对每个 Agent 都需要的端点阻止，其余为警告）；以及到声明的 `reachable` 目标的路由（只检查存在与下一跳类型，对端标注为“平台未验证”）。上一次探测结果会保留。无法读取 AWS 时返回 `502 network_profile.check_failed`（`detail.aws_code`）；访问被拒时会提示更新授予这些调用的 spoke 模板 |
| `POST` | `/api/network-profiles/{profile_id}/probe` | 管理员 | `202` → 管理员视图，`check_results` 中带一条 `{status: running}` 的 `probe` 记录。后台在该配置的子网与安全组中启动一个临时运行时，对每个 `reachable` 目标尝试 DNS、TCP 与 TLS（§12.7）。之后记录变为 `status: done`（有目标不可达时 `severity: warn`，`details.targets[]`）或 `failed`。运行期间修改网络会丢弃迟到的结果。`400 network_profile.nothing_to_probe`（没有 `reachable` 目标）、`409 network_profile.blocked`（上次预检为阻止）、`409 network_profile.probe_running`（30 分钟内启动的探测仍在运行） |
| `GET` | `/api/network-profiles/settings` | 管理员 | 该 Workspace 的 `{network_enforcement, network_public_allowed}`（默认 `false` / `true`） |
| `PUT` | `/api/network-profiles/settings` | 管理员 | 请求体（`extra="forbid"`）`{network_enforcement?, network_public_allowed?}` → 同 GET 形状；省略或为 `null` 的键保持原值。`network_public_allowed: false` 表示每个 Agent 都必须引用网络配置。只有平台能执行这一点，因为没有 IAM 条件键能表达网络模式；部署路径上的拒绝（`400 network.public_not_allowed`）随网络部署向导（V12-deploy）一起提供，目前该开关只被保存与展示。`network_enforcement` 记录该 Workspace 打算在 IAM 层把 AgentCore 限制在已批准的子网与安全组内；平台不会自己写入该策略（见下一条路由） |
| `GET` | `/api/network-profiles/enforcement` | 管理员 | `{settings, approved_subnet_ids[], approved_security_group_ids[], stack_parameters{ApprovedSubnetIds, ApprovedSecurityGroupIds}, policy, actions[], not_enforceable[]}`。已批准集合是所有未被上次预检阻止的存活配置的子网与安全组。`stack_parameters` 是供 spoke 栈（`infra/spoke/launchpad-workspace-role.yaml`）使用的逗号连接值：`ApprovedSubnetIds` 非空时，栈的 `EnforceNetwork` 条件会给 Workspace 角色加上一条 `launchpad-network-enforcement` Deny。`policy` 是可改作 SCP 使用的等效文档：当 `bedrock-agentcore:subnets` / `bedrock-agentcore:securityGroups` 满足 `ForAnyValue:StringNotEquals` 时，拒绝 `bedrock-agentcore:CreateAgentRuntime`、`UpdateAgentRuntime`、`CreateCodeInterpreter` 与 `CreateBrowser`（已批准集合为空时拒绝所有 VPC 放置）。`not_enforceable` 说明缺口：Harness 操作不带这两个键，也没有表达 `networkMode` 的键，因此 PUBLIC 请求在 IAM 层仍会通过 |

`POST /api/agents` 与 `POST /api/agents/{id}/redeploy` 会规范化 `spec.network`：

- `{profile_id}` 必须在项目的允许集合中（`404 network_profile.not_found`），且未被上次预检阻止（`409 network_profile.blocked`）。
- 旧版客户端的内联 `{subnets, security_groups}` 只接受管理员提交（否则 `400 network.inline_not_allowed`）。管理员的内联网络会匹配到或归档为 `legacy-<hash8>` 配置，并加入项目的允许集合。
- 重新部署时，无论配置处于何种状态，都会继续解析该 Agent 已引用的配置。

错误码：`network_profile.not_found`（404，不存在、已归档、属于其他 Workspace，或不在允许集合中）、
`network_profile.name_exists`（409，`detail.profile_id`）、`network_profile.in_use`（409，`detail.agents`）、
`network_profile.default_not_allowed`（400，默认值不在 `profile_ids` 中）、`network_profile.check_failed`（502）、
`network_profile.nothing_to_probe`（400）、`network_profile.probe_running`（409）、`network.public_not_allowed`（400）、`project.not_found`（404，该项目
未绑定到此 Workspace），以及 `validation.invalid_request`（422，请求体不符合 schema，或 VPC / 子网 /
安全组 id 格式错误）。
