# Console single sign-on with IAM Identity Center (SAML 2.0)

[中文](sso-identity-center.zh-CN.md)

IAM Identity Center offers a *customer managed* SAML 2.0 application for any web app,
so the console signs Identity Center users in over SAML: from the console's login
page (SP-initiated) or from the AWS access portal's app tile (IdP-initiated). Accounts
are created on first sign-in; their role and projects follow Identity Center groups.

You need: an administrator account on the console, an Identity Center instance and
permission to add applications to it, and a console reachable at the URL in
`public_base_url` (`config/launchpad.yaml`) — the SP entity ID and ACS URL are built
from it, so set it before you start.

## 1. Create the provider in the console

1. Sign in as an administrator, open **Configure → Single sign-on** and choose
   **New provider → IAM Identity Center**. Give it the name people see on the login
   button ("Company SSO"). The provider is created disabled, with its own SP signing
   key.
2. On the provider's page, **The console gives the IdP** lists three values. Keep the
   page open:
   - **SP entity ID** — `https://<console>/api/auth/saml/<id>/metadata`
   - **ACS URL** — `https://<console>/api/auth/saml/<id>/acs`
   - **SP metadata** — a download of the same values as an XML file

## 2. Add the application in Identity Center

1. In the IAM Identity Center console: **Applications → Add application → I have an
   application I want to set up → SAML 2.0**.
2. **Display name**: "Launchpad console". Under **IAM Identity Center metadata**, copy
   the **IAM Identity Center SAML metadata file** URL (or download the file).
3. **Application properties** (optional): **Application start URL**
   `https://<console>/api/auth/saml/<id>/start` makes the portal tile start a normal
   SP-initiated sign-in; leave it empty and the tile posts an IdP-initiated response
   to the ACS instead. Leave **Relay state** empty (it is the landing path in the
   console, `/` when empty).
4. **Application metadata**: either upload the console's **SP metadata** file, or
   choose **Manually type your metadata values** and enter **Application ACS URL** = the
   ACS URL and **Application SAML audience** = the SP entity ID.
5. Submit, then open the application's **Actions → Edit attribute mappings**:

   | User attribute in the application | Maps to this string value | Format |
   |---|---|---|
   | `Subject` | `${user:subject}` | `persistent` |
   | `email` | `${user:email}` | `unspecified` |
   | `name` | `${user:name}` | `unspecified` |
   | `groups` | `${user:groups}` | `unspecified` |

   `Subject` becomes the account's key in the console. `${user:subject}` is the
   stable Identity Center user id; an email address can change, so it is the weaker
   choice. `email` is required (sign-in is refused without it); the console username
   is the email's local part unless you also map a `username` attribute.
6. **Assign users and groups** to the application: only assigned people can sign in.

## 3. Load the Identity Center metadata

Back on the provider's page, under **The IdP gives the console**, paste the metadata
URL and choose **Load** (or **Upload a metadata file**). The console reads the IdP
entity ID, the SSO URL and the signing certificate and shows how long the certificate
stays valid. Then **Test connection**: it re-reads the metadata and checks the certificate and
the SP key.

Options on the same page:

- **Allow IdP-initiated sign-in** — on by default, so the portal tile works. Turn it
  off to accept only sign-ins that start at the console.
- **Sign requests** — Identity Center does not require signed AuthnRequests; leave it
  off.
- **Require encrypted assertions** — Identity Center does not encrypt assertions for
  customer managed applications, so leave this off or every sign-in is refused.
- **SLO URL** — Identity Center's metadata usually carries none; signing out then ends
  the console session only.

## 4. Map groups, then enable

`${user:groups}` sends Identity Center **group ids** (GUIDs such as
`906754d8-…`), not names. In **Group mappings**, enter the id shown on the group's
page in Identity Center (**Groups → <group> → Group ID**) and choose what it grants: a
global role, a project role, or a project-group role. Mappings are applied at every
sign-in; only memberships a mapping created are changed, and a role an administrator
set by hand wins (the user page flags the difference). The **Accounts** section sets
`jit` (create accounts on first sign-in, the default) or `existing_only`, an
email-domain allowlist and the default role.

Turn the provider **on**. The login page now shows "Continue with Company SSO", and
the app tile appears in each assigned user's AWS access portal.

## 5. Sign-in checks and troubleshooting

Every response is verified with `python3-saml`: the signature against the Identity
Center certificate (unsigned responses are refused), issuer, audience (the SP entity
ID), destination (the ACS URL), the validity window with 2 minutes of clock skew, the
`InResponseTo` of an SP-initiated sign-in (a request this console sent in the last 10
minutes, used once), and replay (an assertion id is accepted once). A refusal lands on
the login page with a reason; the provider's **Recent sign-ins** and the journal
(`auth.sso.login`) show the same code.

| Reason | Usual cause |
|---|---|
| `auth.saml.bad_audience` | **Application SAML audience** is not the SP entity ID exactly |
| `auth.saml.bad_destination` | **Application ACS URL** differs, or `public_base_url` changed after setup |
| `auth.saml.bad_signature` / `unsigned` | The certificate rotated in Identity Center — **Load** the metadata again |
| `auth.saml.expired` | The console host's clock is more than 2 minutes off |
| `auth.saml.unsolicited` | The portal tile was used while IdP-initiated sign-in is off |
| `auth.saml.in_response_to` | The sign-in page was left open over 10 minutes, or the response was replayed |
| `auth.sso.email_missing` | The `email` attribute mapping is missing |
| `auth.sso.not_provisioned` | `existing_only` and no account is linked to this user yet |

## Provisioning (SCIM) and sign-out

Identity Center provisions *inbound* (from an external IdP into Identity Center); it
does not push users to customer managed SAML applications. So with Identity Center,
accounts are created at first sign-in and group changes take effect at the user's next
sign-in. To cut access at once, disable the account on the console's **Users** page —
its open sessions end on their next request. Identity providers that do push SCIM
(Entra ID, Okta, Authentik) are configured on the same page under **SCIM provisioning**;
see [api.md](api.md#console-single-sign-on-api-saml-scim-sign-out).

The **Sign-in page** switch (Configure → Single sign-on) set to **Single sign-on
only** shows only the IdP buttons; the built-in administrator can always sign in at `https://<console>/?local=1`.

## Rotating keys

- Identity Center certificate: Identity Center lets you add a second certificate
  before the first expires. **Load** the metadata again once both are published; the
  console accepts either.
- SP key: **Rotate key** generates a new SP key and certificate; Identity Center does
  not check SP signatures unless you sign requests, so no change is needed there.
