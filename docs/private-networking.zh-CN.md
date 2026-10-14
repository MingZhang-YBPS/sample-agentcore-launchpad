# 私有网络：PrivateLink 入站、私有部署控制台、VPC 拆除

English: [private-networking.md](private-networking.md)

Agent 通过**网络配置档**运行在客户 VPC 中（配置 → 网络配置档，`/v3/network`；设计 projects §12）。本文说明配置档之外的部分：
私网调用方如何访问 Agent、Launchpad 控制台如何不带公网 IP 部署、平台给出的遥测与成本提示，以及拆除 VPC 的顺序。

## 1. Agent 的私有入站（AgentCore PrivateLink）

内部应用通过接口端点 `com.amazonaws.<region>.bedrock-agentcore`（数据面）调用 Agent；控制面有自己的端点
`com.amazonaws.<region>.bedrock-agentcore-control`。

配置档预检（`POST /api/network-profiles/{id}/check`）把它作为信息项 `inbound.endpoint` 报告，**从不阻断**配置档：

- 配置档 VPC 中是否存在数据面 / 控制面端点、私有 DNS 设置与子网；
- 端点策略 `Allow` 语句列出的主体，以及是否允许任意主体（`*`，默认的完全访问策略也是如此）。

**端点策略只能限定 IAM 主体。** 用 OAuth 认证的调用方（启用入站 JWT 授权的 Agent）在端点策略中表现为 `Principal: "*"`。
对这类 Agent，部署时的预检会附上这条说明；真正限制调用方的是 Agent 的入站 JWT 授权（`routers/identity.py` 的 inbound-auth 设置）。

## 2. 私有部署的 Launchpad 控制台（`ConsoleExposure`）

workshop 模板（`static/infrastructure/launchpad-multiuser.yaml`，位于 workshop 仓库，不在本仓库）目前要求公有子网：CloudFront
的源是实例的 `PublicDnsName`，实例分配公网 IPv4。模板需要新增参数：

```yaml
ConsoleExposure:
  Type: String
  AllowedValues: [public-instance, cloudfront-vpc-origin, internal-alb]
  Default: public-instance
CreateEndpoints:          # 仅 internal-alb：一并创建下列接口端点
  Type: String
  AllowedValues: ["true", "false"]
  Default: "false"
```

| 取值 | 模板需要做的事 | 说明 |
|---|---|---|
| `public-instance`（默认，现状） | 公有子网、公网 IPv4、CloudFront 源为 `PublicDnsName`、源密钥头 | 不变；workshop 已验证 |
| `cloudfront-vpc-origin` | 实例放私有子网（`ExistingSubnetId` 不必是公有子网，`AssociatePublicIpAddress: false`）；CloudFront **VPC 源**（`AWS::CloudFront::VpcOrigin`）指向实例或内部 ALB；实例安全组只在控制台端口放行 CloudFront VPC 源的托管前缀列表 / 安全组 | 保留 CloudFront 的 HTTPS 域名且无公网 IP。需确认目标区域支持 VPC 源及支持的源类型（EC2 / ALB / NLB）。实例仍需出站：NAT、下列全部端点，或预制 AMI（bootstrap 会从互联网安装软件包） |
| `internal-alb` | 私有子网中的内部 ALB、ACM 证书、Route 53 私有托管区记录；不使用 CloudFront | 完全私有：只能经 VPN / Direct Connect 访问。workshop 参与者无法访问，`/v1` 只对内网调用方可用 |

推荐：模板默认保持 `public-instance`；企业部署文档推荐 `internal-alb`；`cloudfront-vpc-origin` 是“要公网 HTTPS 域名但不要公网 IP”的折中。

控制台主机在私有子网时，hub 调用的每个 AWS API 都必须经 NAT 或接口端点访问：`sts`、`iam`、`s3`（网关）、`ecr.api`、`ecr.dkr`、
`logs`、`codebuild`、`cognito-idp`、`secretsmanager`、`bedrock`、`bedrock-runtime`、`bedrock-agentcore`、
`bedrock-agentcore-control`、`xray`、`monitoring`（CloudWatch）、`servicequotas`、`ec2`。`CreateEndpoints=true` 会创建它们
（开启私有 DNS，位于控制台子网，安全组放行来自实例的 443）。`iam` 是全局服务，大多数区域没有区域端点，请保留到它的 NAT 路径，
或确认目标区域存在该端点。

模板测试：`cfn-lint`，以及每种 `ConsoleExposure` 取值的部署冒烟（`cloudfront-vpc-origin` 需在支持 VPC 源的区域）。

## 3. VPC Agent 的遥测与成本

- **遥测。** VPC 中的 Agent 若无法访问 `logs`（或 `xray`）端点，日志与 span 会静默丢失。预检对 `logs` 阻断（每个 Agent 都需要），
  对 `xray` 警告。若配置档最近一次预检中这两项未通过，Agent 的“网络”卡片会提示“该配置档无法访问 logs / xray”；后端函数
  `services/network_profiles.telemetry_hint(db, workspace_id, spec)` 给出同样的提示，供观测页在“最近 30 分钟有调用但无 span”时显示。
- **成本。** 预检的信息项 `cost` 统计 VPC 中的接口端点数 × 配置档的可用区数，以及配置档子网路由经过的 NAT 网关；配置档页链接
  PrivateLink 与 VPC（NAT）价目表。平台不自行估价（价格随区域变化），也不创建端点或 NAT 网关——它们是客户的网络资源。

## 4. 拆除：先删除 Agent，再删除 VPC

AgentCore 的网络服务关联角色（`AWSServiceRoleForBedrockAgentCoreNetwork`）在配置档子网中持有 ENI，并在 runtime / harness 删除后
一段时间才释放；释放前子网与安全组无法删除。

1. 删除使用该配置档的所有 Agent（配置档页的“使用方”列；仍有存活 Agent 使用时配置档不可归档——`network_profile.in_use`）。
2. 每删除一个 VPC Agent，都会启动一个 `agent_eni_release` 作业：先轮询 `ec2:DescribeNetworkInterfaces`（按配置档的安全组与子网过滤）
   几分钟，之后交给定时拆除任务继续检查，最长 6 小时，超时后作业失败并列出仍存在的 ENI。作业运行期间配置档不可归档
   （`network_profile.enis_pending`）。
3. 作业全部成功后，归档配置档，再删除 VPC 中的子网、安全组与端点。

ENI 没有任何文档化的、能指明所属 Agent 的标签或描述格式：多个 Agent 共用一个配置档时，只等待描述中包含被删 runtime id 的 ENI。
