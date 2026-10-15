# 用 IAM Identity Center 单点登录控制台（SAML 2.0）

[English](sso-identity-center.md)

IAM Identity Center 为任意 Web 应用提供“客户托管”的 SAML 2.0 应用，因此控制台通过 SAML 让 Identity Center 用户登录：既可以从控制台登录页发起（SP 发起），也可以从 AWS 访问门户的应用图标进入（IdP 发起）。账户在首次登录时创建，角色和项目跟随 Identity Center 组。

准备：控制台的管理员账户；一个 Identity Center 实例以及在其中添加应用的权限；控制台可通过 `public_base_url`（`config/launchpad.yaml`）访问——SP 实体 ID 与 ACS URL 由它生成，请先设置好。

## 1. 在控制台创建提供方

1. 以管理员登录，打开 **配置 → 单点登录**，选择 **新建身份提供方 → IAM Identity Center**。名称就是登录按钮上显示的文字（例如“公司 SSO”）。新建的提供方处于停用状态，并自带一把 SP 签名密钥。
2. 提供方页面的 **控制台交给身份提供方** 列出三项，保持页面打开：
   - **SP 实体 ID**——`https://<console>/api/auth/saml/<id>/metadata`
   - **ACS URL**——`https://<console>/api/auth/saml/<id>/acs`
   - **SP 元数据**——包含以上信息的 XML 文件下载

## 2. 在 Identity Center 添加应用

1. 在 IAM Identity Center 控制台：**应用程序 → 添加应用程序 → 我有要设置的应用程序 → SAML 2.0**。
2. **显示名称**：“Launchpad console”。在 **IAM Identity Center 元数据** 下复制 **IAM Identity Center SAML 元数据文件** 的 URL（或下载该文件）。
3. **应用程序属性**（可选）：**应用程序启动 URL** 填 `https://<console>/api/auth/saml/<id>/start`，门户图标就会走一次普通的 SP 发起登录；留空则图标直接向 ACS 投递 IdP 发起的响应。**中继状态** 留空（它是进入控制台后的落地路径，空即 `/`）。
4. **应用程序元数据**：上传控制台的 **SP 元数据** 文件，或选择 **手动输入元数据值**，填写 **应用程序 ACS URL** = ACS URL、**应用程序 SAML 受众** = SP 实体 ID。
5. 提交后打开该应用的 **操作 → 编辑属性映射**：

   | 应用程序中的用户属性 | 映射到此字符串值 | 格式 |
   |---|---|---|
   | `Subject` | `${user:subject}` | `persistent` |
   | `email` | `${user:email}` | `unspecified` |
   | `name` | `${user:name}` | `unspecified` |
   | `groups` | `${user:groups}` | `unspecified` |

   `Subject` 是该账户在控制台中的主键。`${user:subject}` 是稳定的 Identity Center 用户 ID；邮箱可能变化，作为主键较弱。`email` 必填（缺少时拒绝登录）；控制台用户名取邮箱 `@` 之前的部分，除非另外映射 `username` 属性。
6. **分配用户和组**：只有分配到该应用的人才能登录。

## 3. 加载 Identity Center 元数据

回到提供方页面，在 **身份提供方交给控制台** 下粘贴元数据 URL 并点 **加载**（或 **上传元数据文件**）。控制台读出 IdP 实体 ID、SSO URL 与签名证书，并显示证书剩余有效期。然后点 **测试连接**：重新读取元数据，检查证书与 SP 密钥。

同一页面上的选项：

- **允许 IdP 发起登录**——默认开启，门户图标可用。关闭后只接受从控制台发起的登录。
- **签名请求**——Identity Center 不要求签名的 AuthnRequest，保持关闭。
- **要求加密断言**——Identity Center 不为客户托管应用加密断言，请保持关闭，否则每次登录都会被拒绝。
- **SLO URL**——Identity Center 的元数据通常不含；此时登出只结束控制台会话。

## 4. 映射组，然后启用

`${user:groups}` 发送的是 Identity Center 的 **组 ID**（如 `906754d8-…` 这样的 GUID），不是组名。在 **组映射** 中填写 Identity Center 组页面上的 ID（**组 → <组> → 组 ID**），并选择它授予的内容：全局角色、项目角色或项目组角色。映射在每次登录时应用；只改动由映射产生的成员资格，管理员手工设置的角色优先（用户页会标注差异）。**账号** 一节设置 `jit`（首次登录自动建号，默认）或 `existing_only`、邮箱域名允许列表与默认角色。

把提供方 **启用**。登录页即出现“使用 公司 SSO 登录”，每个被分配的用户的 AWS 访问门户里也会出现应用图标。

## 5. 登录校验与排错

每个响应都用 `python3-saml` 校验：对 Identity Center 证书验签（未签名的响应一律拒绝）、issuer、受众（SP 实体 ID）、目的地（ACS URL）、有效时间窗（允许 2 分钟时钟偏差）、SP 发起登录的 `InResponseTo`（必须是本控制台最近 10 分钟内发出的请求，且只能用一次）、防重放（同一断言 ID 只接受一次）。被拒绝时回到登录页并显示原因；提供方的 **最近登录** 与审计日志（`auth.sso.login`）记录同样的代码。

| 原因 | 常见原因 |
|---|---|
| `auth.saml.bad_audience` | **应用程序 SAML 受众** 与 SP 实体 ID 不完全一致 |
| `auth.saml.bad_destination` | **应用程序 ACS URL** 不一致，或配置后修改了 `public_base_url` |
| `auth.saml.bad_signature` / `unsigned` | Identity Center 证书已轮换——重新 **加载** 元数据 |
| `auth.saml.expired` | 控制台主机时钟偏差超过 2 分钟 |
| `auth.saml.unsolicited` | 关闭了 IdP 发起登录却从门户图标进入 |
| `auth.saml.in_response_to` | 登录页停留超过 10 分钟，或响应被重放 |
| `auth.sso.email_missing` | 缺少 `email` 属性映射 |
| `auth.sso.not_provisioned` | 策略为 `existing_only`，且该用户尚无关联账户 |

## 预配（SCIM）与登出

Identity Center 的预配是 *入站* 的（从外部 IdP 同步到 Identity Center），不会向客户托管的 SAML 应用推送用户。因此使用 Identity Center 时，账户在首次登录时创建，组变化在用户下次登录时生效。要立即切断访问，请在控制台 **用户** 页停用该账户——其已打开的会话在下个请求即结束。会推送 SCIM 的身份提供方（Entra ID、Okta、Authentik）在同一页面的 **SCIM 预配** 下配置；见 [api.zh-CN.md](api.zh-CN.md)。

把 **登录页** 开关（配置 → 单点登录）设为 **只用单点登录**（隐藏本地登录），登录页只显示 IdP 按钮；内置管理员始终可以通过 `https://<console>/?local=1` 登录。

## 轮换密钥

- Identity Center 证书：Identity Center 允许在旧证书过期前添加第二张证书。两张都发布后重新 **加载** 元数据，控制台接受其中任意一张。
- SP 密钥：**轮换密钥** 生成新的 SP 密钥与证书；除非开启了签名请求，Identity Center 不校验 SP 签名，无需在那边改动。
