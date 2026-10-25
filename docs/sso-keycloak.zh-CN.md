# 用 Keycloak 单点登录控制台（OIDC 或 SAML 2.0）

[English](sso-keycloak.md)

Keycloak 可以通过任一通道让用户登录控制台。**OpenID Connect 更简单**，推荐使用；SAML 2.0 供统一使用 SAML 的 realm 选用。两条通道共用同样的账号规则（`jit` / `existing_only`、邮箱域名允许列表）与组映射。Authentik 的配置方式与下文 OIDC 部分相同（一个带 `groups` scope 的 OAuth2/OpenID 提供方）。

准备：控制台的管理员账户；一个你有管理权限的 Keycloak realm；控制台的 `public_base_url`（`config/launchpad.yaml`）设为用户实际访问的 URL——下文所有回调地址都由它生成。示例使用 `https://keycloak.example.com`、realm `corp`，控制台地址为 `https://launchpad.example.com`。

## A. OpenID Connect

### A.1 在控制台创建提供方

**配置 → 单点登录 → 新建身份提供方 → Keycloak**。填写按钮名称（“公司 SSO”），**颁发者 URL** 填 `https://keycloak.example.com/realms/corp`。页面会显示 **重定向 URI**：`https://launchpad.example.com/api/auth/oidc/<id>/callback`。

### A.2 在 Keycloak 创建客户端

1. **Clients → Create client**：类型 **OpenID Connect**，Client ID `launchpad`。
2. **Capability config**：**Client authentication** 开启（机密客户端）——关闭也可以（公共客户端），因为控制台始终使用 PKCE；**Standard flow** 开启，其余关闭。
3. **Login settings**：
   - **Valid redirect URIs**：上述重定向 URI
   - **Valid post logout redirect URIs**：`https://launchpad.example.com/`
   - **Web origins**：`https://launchpad.example.com`
4. **Advanced → Proof Key for Code Exchange Code Challenge Method**：`S256`。
5. 机密客户端请复制 **Credentials → Client secret**。

### A.3 发送组

Keycloak 默认不在 token 中放组。**Clients → launchpad → Client scopes → launchpad-dedicated → Configure a new mapper → Group Membership**：

- **Token Claim Name**：`groups`
- **Full group path**：开或关均可——映射写 `platform` 或 `/platform` 都能匹配组 `/platform`；不同父组下有同名组时请开启
- **Add to ID token**：开启

### A.4 在控制台完成配置

回到提供方页面：**客户端 ID** 填 `launchpad`，**客户端密钥** 填复制的值（加密保存，之后不再显示；`arn:aws:secretsmanager:…` 值会作为引用保存）。Keycloak 模板已按 `preferred_username`、`email`、`name`、`groups` 读取声明。**测试连接** 会读取 discovery 文档与 JWKS；然后添加 **组映射** 并 **启用** 提供方。

登出控制台时也会登出 Keycloak（RP 发起登出到 realm 的 `end_session_endpoint`，再回到 `/`）。

## B. SAML 2.0

### B.1 在控制台创建提供方

**配置 → 单点登录 → 新建身份提供方 → SAML 2.0**。页面列出控制台交给身份提供方的值：**SP 实体 ID**（`https://launchpad.example.com/api/auth/saml/<id>/metadata`）、**ACS URL**（`…/acs`）、**SLO URL**（`…/sls`）以及 **SP 元数据** 下载。

### B.2 在 Keycloak 创建客户端

1. **Clients → Import client**，上传控制台的 SP 元数据文件——Keycloak 据此填好客户端。（手工方式：**Create client**，类型 **SAML**，**Client ID** = SP 实体 ID，**Valid redirect URIs** 与 **Master SAML Processing URL** = ACS URL。）
2. 客户端 **Settings → SAML capabilities**：**Name ID format** 选 `username`（或 `persistent`，得到改名后仍不变的不透明 ID），**Force name ID format** 开启。
3. **Signature and Encryption**：**Sign documents** 开启。Keycloak 的 **Keys → Client signature required** 默认开启——此时请在控制台开启 **签名请求**（导入的元数据带有 Keycloak 用来验签的 SP 证书）。**Encrypt assertions** 可选；若开启，控制台也请开启 **要求加密断言**。
4. **Advanced → Logout Service Redirect Binding URL**：填控制台的 SLO URL，这样在 Keycloak 登出时控制台会话也会结束。

### B.3 发送邮箱、姓名与组

**Client scopes → <client>-dedicated → Add mapper → By configuration**：

| Mapper 类型 | 名称 / 属性 | SAML Attribute Name |
|---|---|---|
| User Property | `email` | `email` |
| User Property | `username` | `username` |
| User Attribute | `firstName`（全名可用 JavaScript mapper） | `name` |
| Group list | **Single Group Attribute** 开启，**Full group path** 同 OIDC | `groups` |

`email` 必填——缺少时拒绝登录。

### B.4 加载元数据并启用

在 **身份提供方交给控制台** 下，**IdP 元数据 URL** 填 `https://keycloak.example.com/realms/corp/protocol/saml/descriptor` → **加载**。控制台读出 IdP 实体 ID、SSO URL、SLO URL 与签名证书。**测试连接**、添加 **组映射**，然后 **启用**。

**允许 IdP 发起登录** 默认开启。在客户端上设置 **IDP-Initiated SSO URL name** 后，Keycloak 可从 `https://keycloak.example.com/realms/corp/protocol/saml/clients/<该名称>` 发起登录；关闭该选项则只接受从控制台发起的登录。

每个 SAML 响应都会校验签名、issuer、受众、目的地、有效时间窗（允许 2 分钟偏差）、`InResponseTo`（最近 10 分钟内 SP 发起的请求，只能用一次）与重放；被拒绝时可能显示的原因见 [sso-identity-center.zh-CN.md](sso-identity-center.zh-CN.md) 第 5 节。

## C. SCIM 预配（可选）

Keycloak 没有内置的 SCIM *客户端*，需要社区扩展。任何推送 SCIM 2.0 的 IdP（Entra ID、Okta、Authentik 或 Keycloak 扩展）配置方式相同：

1. 在提供方页面 **SCIM 预配 → 创建令牌**。立即复制——token 只显示一次（控制台只保存其哈希）。**轮换令牌** 替换它；**关闭** 立即使其失效。
2. 在 IdP 中：SCIM 基础 URL `https://launchpad.example.com/api/scim/v2`（页面上有显示），认证方式 **Bearer token**，填上面的 token。
3. **主体**：与该 IdP 登录时的 NameID / `sub` 相同的 SCIM 属性——大多数为 `userName`（Keycloak OIDC 的 `sub` 是用户 ID，若扩展把该 ID 作为 `externalId` 发送，请选 `externalId`）。

此后，在 IdP 中停用的用户会在控制台中被停用，其已打开的会话在下个请求即结束；组成员变化会立即重新应用映射，而不必等到下次登录。组映射按 SCIM 组的 `displayName` 或 `externalId` 匹配。

## 隐藏本地登录

提供方可用后，把 **登录页** 开关（配置 → 单点登录）设为 **只用单点登录**（隐藏本地登录）：登录页只显示 IdP 按钮，密码登录与自助注册都会被拒绝。内置管理员始终可以通过 `https://launchpad.example.com/?local=1` 登录。
