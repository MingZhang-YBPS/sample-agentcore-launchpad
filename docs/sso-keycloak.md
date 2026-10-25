# Console single sign-on with Keycloak (OIDC or SAML 2.0)

[中文](sso-keycloak.zh-CN.md)

Keycloak can sign people in to the console over either channel. **OpenID Connect is
the simpler one** and the recommended choice; SAML 2.0 is there for realms that
standardize on it. Both share the same account rules (`jit` / `existing_only`, the
email-domain allowlist) and the same group mappings. Authentik is configured the same
way as the OIDC part below (an OAuth2/OpenID provider with a `groups` scope).

You need: an administrator account on the console, a Keycloak realm you administer,
and the console's `public_base_url` (`config/launchpad.yaml`) set to the URL people
use — every redirect URL below is built from it. The examples use
`https://keycloak.example.com`, realm `corp`, and a console at
`https://launchpad.example.com`.

## A. OpenID Connect

### A.1 Create the provider in the console

**Configure → Single sign-on → New provider → Keycloak**. Name it ("Company SSO") and
set **Issuer URL** to `https://keycloak.example.com/realms/corp`. The page shows the
**Redirect URI**: `https://launchpad.example.com/api/auth/oidc/<id>/callback`.

### A.2 Create the client in Keycloak

1. **Clients → Create client**: type **OpenID Connect**, Client ID `launchpad`.
2. **Capability config**: **Client authentication** on (a confidential client) —
   or off for a public client, which works too since the console always uses PKCE;
   **Standard flow** on, everything else off.
3. **Login settings**:
   - **Valid redirect URIs**: that Redirect URI
   - **Valid post logout redirect URIs**: `https://launchpad.example.com/`
   - **Web origins**: `https://launchpad.example.com`
4. **Advanced → Proof Key for Code Exchange Code Challenge Method**: `S256`.
5. For a confidential client copy **Credentials → Client secret**.

### A.3 Send the groups

Keycloak puts no groups in the token by default. **Clients → launchpad → Client scopes
→ launchpad-dedicated → Configure a new mapper → Group Membership**:

- **Token Claim Name**: `groups`
- **Full group path**: either — a mapping written `platform` or `/platform` matches
  the group `/platform`; turn it on when two groups share a name under different parents
- **Add to ID token**: on

### A.4 Finish in the console

Back on the provider: **Client ID** `launchpad`, **Client secret** the copied value
(stored encrypted, never shown again; an `arn:aws:secretsmanager:…` value is stored
as a reference instead). The Keycloak template already reads `preferred_username`,
`email`, `name` and `groups`. **Test connection** reads the discovery document and
the JWKS; then add **Group mappings** and turn the provider **on**.

Signing out of the console also signs out of Keycloak (RP-initiated logout to the
realm's `end_session_endpoint`, back to `/`).

## B. SAML 2.0

### B.1 Create the provider in the console

**Configure → Single sign-on → New provider → SAML 2.0**. The page lists what the
console gives the IdP: **SP entity ID**
(`https://launchpad.example.com/api/auth/saml/<id>/metadata`), **ACS URL**
(`…/acs`), **SLO URL** (`…/sls`) and the **SP metadata** download.

### B.2 Create the client in Keycloak

1. **Clients → Import client** and upload the console's SP metadata file — Keycloak
   fills the client from it. (By hand: **Create client**, type **SAML**, **Client ID**
   = the SP entity ID, **Valid redirect URIs** and **Master SAML Processing URL** =
   the ACS URL.)
2. Client **Settings → SAML capabilities**: **Name ID format** `username` (or
   `persistent` for an opaque id that survives a rename), **Force name ID format** on.
3. **Signature and Encryption**: **Sign documents** on. Keycloak's **Keys → Client
   signature required** is on by default — then turn **Sign requests** on in the
   console (the imported metadata carries the SP certificate Keycloak checks them
   with). **Encrypt assertions** is optional; if you turn it on, turn on **Require
   encrypted assertions** in the console as well.
4. **Advanced → Logout Service Redirect Binding URL**: the console's SLO URL, so
   signing out in Keycloak ends the console session too.

### B.3 Send email, name and groups

**Client scopes → <client>-dedicated → Add mapper → By configuration**:

| Mapper type | Name / property | SAML Attribute Name |
|---|---|---|
| User Property | `email` | `email` |
| User Property | `username` | `username` |
| User Attribute | `firstName` (or a JavaScript mapper for the full name) | `name` |
| Group list | **Single Group Attribute** on, **Full group path** as for OIDC | `groups` |

`email` is required — sign-in is refused without it.

### B.4 Load the metadata and enable

Under **The IdP gives the console**, **IdP metadata URL**
`https://keycloak.example.com/realms/corp/protocol/saml/descriptor` → **Load**. The
console reads the IdP entity ID, SSO URL, SLO URL and signing certificate. **Test
connection**, add **Group mappings**, then turn it **on**.

**Allow IdP-initiated sign-in** is on by default. Keycloak starts one from
`https://keycloak.example.com/realms/corp/protocol/saml/clients/<IDP-Initiated SSO URL
name>` once you set that name on the client; turn the option off to accept only
sign-ins that begin at the console.

Every SAML response is checked for signature, issuer, audience, destination, the
validity window (2 minutes of skew), `InResponseTo` (an SP-initiated request from the
last 10 minutes, used once) and replay; see
[sso-identity-center.md](sso-identity-center.md#5-sign-in-checks-and-troubleshooting)
for the reasons a refusal can show.

## C. SCIM provisioning (optional)

Keycloak has no built-in SCIM *client*; community extensions add one. Any IdP that
pushes SCIM 2.0 (Entra ID, Okta, Authentik, or a Keycloak extension) is set up the
same way:

1. On the provider's page, **SCIM provisioning → Create token**. Copy the token now —
   it is shown once (the console keeps only its hash). **Rotate token** replaces it;
   **Turn off** stops it at once.
2. In the IdP: SCIM base URL `https://launchpad.example.com/api/scim/v2` (shown on the
   page), authentication **Bearer token**, the token above.
3. **Subject**: the SCIM attribute that equals the NameID / `sub` this IdP signs
   people in with — `userName` for most (Keycloak OIDC's `sub` is the user's id, so
   pick `externalId` there when the extension sends the id as `externalId`).

From then on a user deactivated in the IdP is disabled in the console and their open
sessions end on the next request; group membership changes re-apply the mappings at
once instead of at the next sign-in. Group mappings match a SCIM group's
`displayName` or `externalId`.

## Hiding local login

Once a provider works, set the **Sign-in page** switch (Configure → Single sign-on) to
**Single sign-on only**: the login page shows only the IdP buttons, and password
sign-in and self-registration are refused. The built-in administrator can always sign
in at `https://launchpad.example.com/?local=1`.
