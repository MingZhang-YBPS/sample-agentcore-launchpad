/**
 * The single sign-on page's pure logic: the editable draft of a provider (OIDC or
 * SAML), the patch that saves it, the connection test as a checklist, and mapping-row
 * validation.
 */
import type { Signal } from "../../ui";
import type { SsoClaims, SsoMapping } from "../../../lib/api/sso";
import type {
  AnyPreset,
  AnyTestResult,
  PresetTemplate,
  Provider,
  ProviderWrite,
  ScimBlock,
} from "../../../lib/api/sso-saml";

type SsoProvider = Provider;
type SsoProviderWrite = ProviderWrite;

export interface ProviderDraft {
  name: string;
  issuer: string;
  client_id: string;
  /** "" = keep the stored secret; a value replaces it */
  client_secret: string;
  clear_secret: boolean;
  scopes: string;
  claims: SsoClaims;
  account_policy: "jit" | "existing_only";
  /** one domain per line / comma separated */
  allowed_email_domains: string;
  default_role: SsoProvider["default_role"];
  valid_days: string;
  sort_order: string;
  /** SAML (SSO-2): the IdP side as typed or parsed from its metadata */
  saml_idp_entity_id: string;
  saml_sso_url: string;
  saml_slo_url: string;
  /** PEM blocks or bare base64 bodies, one per certificate */
  saml_idp_certs: string;
  saml_name_id_format: string;
  saml_sign_requests: boolean;
  saml_want_encrypted: boolean;
  saml_allow_idp_initiated: boolean;
  scim_subject: ScimBlock["subject"];
}

export function draftOf(provider: SsoProvider): ProviderDraft {
  return {
    name: provider.name,
    issuer: provider.issuer,
    client_id: provider.client_id,
    client_secret: "",
    clear_secret: false,
    scopes: provider.scopes,
    claims: { ...provider.claims },
    account_policy: provider.account_policy,
    allowed_email_domains: provider.allowed_email_domains.join("\n"),
    default_role: provider.default_role,
    valid_days: String(provider.valid_days),
    sort_order: String(provider.sort_order),
    saml_idp_entity_id: provider.saml?.idp_entity_id ?? "",
    saml_sso_url: provider.saml?.sso_url ?? "",
    saml_slo_url: provider.saml?.slo_url ?? "",
    saml_idp_certs: (provider.saml?.idp_certs ?? []).map((c) => c.body).join("\n\n"),
    saml_name_id_format: provider.saml?.name_id_format ?? "",
    saml_sign_requests: provider.saml?.sign_requests ?? false,
    saml_want_encrypted: provider.saml?.want_encrypted ?? false,
    saml_allow_idp_initiated: provider.saml?.allow_idp_initiated ?? true,
    scim_subject: provider.scim?.subject ?? "userName",
  };
}

/** Certificates from a textarea: PEM blocks, or bare base64 bodies split by blank lines. */
export function splitCerts(text: string): string[] {
  const pem = [...text.matchAll(/-----BEGIN CERTIFICATE-----([\s\S]*?)-----END CERTIFICATE-----/g)].map((m) => m[1]);
  const blocks = pem.length ? pem : text.split(/\n\s*\n/);
  return [...new Set(blocks.map((b) => b.replace(/\s+/g, "")).filter(Boolean))];
}

export function parseDomains(text: string): string[] {
  return [...new Set(text.split(/[\s,;]+/).map((d) => d.trim().toLowerCase().replace(/^@/, "")).filter(Boolean))];
}

/** Only what changed, so an untouched secret is never sent back. */
export function patchOf(provider: SsoProvider, draft: ProviderDraft): SsoProviderWrite {
  const patch: SsoProviderWrite = {};
  if (draft.name.trim() !== provider.name) patch.name = draft.name.trim();
  if (draft.issuer.trim() !== provider.issuer) patch.issuer = draft.issuer.trim();
  if (draft.client_id.trim() !== provider.client_id) patch.client_id = draft.client_id.trim();
  if (draft.clear_secret) patch.client_secret = "";
  else if (draft.client_secret.trim()) patch.client_secret = draft.client_secret.trim();
  if (draft.scopes.trim().split(/\s+/).join(" ") !== provider.scopes) patch.scopes = draft.scopes.trim();
  const claims = Object.fromEntries(
    Object.entries(draft.claims).map(([key, value]) => [key, value.trim()]),
  ) as unknown as SsoClaims;
  if ((Object.keys(claims) as (keyof SsoClaims)[]).some((key) => claims[key] !== provider.claims[key])) {
    patch.claims = claims;
  }
  if (draft.account_policy !== provider.account_policy) patch.account_policy = draft.account_policy;
  const domains = parseDomains(draft.allowed_email_domains);
  if (domains.join("\n") !== provider.allowed_email_domains.join("\n")) patch.allowed_email_domains = domains;
  if (draft.default_role !== provider.default_role) patch.default_role = draft.default_role;
  const days = Number.parseInt(draft.valid_days, 10);
  if (Number.isFinite(days) && days !== provider.valid_days) patch.valid_days = days;
  const order = Number.parseInt(draft.sort_order, 10);
  if (Number.isFinite(order) && order !== provider.sort_order) patch.sort_order = order;
  if (draft.scim_subject !== (provider.scim?.subject ?? "userName")) patch.scim_subject = draft.scim_subject;
  const saml = provider.saml;
  if (provider.kind === "saml" && saml) {
    // the OIDC-only fields are not this provider's: never send them
    delete patch.issuer;
    delete patch.client_id;
    delete patch.client_secret;
    delete patch.scopes;
    const text = (key: "saml_idp_entity_id" | "saml_sso_url" | "saml_slo_url" | "saml_name_id_format", was: string) => {
      if (draft[key].trim() !== was) patch[key] = draft[key].trim();
    };
    text("saml_idp_entity_id", saml.idp_entity_id);
    text("saml_sso_url", saml.sso_url);
    text("saml_slo_url", saml.slo_url);
    text("saml_name_id_format", saml.name_id_format);
    const certs = splitCerts(draft.saml_idp_certs);
    if (certs.join("|") !== saml.idp_certs.map((c) => c.body).join("|")) patch.saml_idp_certs = certs;
    if (draft.saml_sign_requests !== saml.sign_requests) patch.saml_sign_requests = draft.saml_sign_requests;
    if (draft.saml_want_encrypted !== saml.want_encrypted) patch.saml_want_encrypted = draft.saml_want_encrypted;
    if (draft.saml_allow_idp_initiated !== saml.allow_idp_initiated) {
      patch.saml_allow_idp_initiated = draft.saml_allow_idp_initiated;
    }
  }
  return patch;
}

export function isDirty(provider: SsoProvider, draft: ProviderDraft): boolean {
  return Object.keys(patchOf(provider, draft)).length > 0;
}

export type DraftProblem = "issuer" | "client_id" | "scopes" | "valid_days" | "saml_sso_url" | "saml_slo_url";

/** https, or http to this machine only (the backend's rule). */
export function isIdpUrl(value: string): boolean {
  return /^https:\/\/[^\s/]+/.test(value) || /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?(\/|$)/.test(value);
}

/** Field problems the form shows before a save round-trip. */
export function draftProblems(draft: ProviderDraft, kind: Provider["kind"] = "oidc"): DraftProblem[] {
  const problems: DraftProblem[] = [];
  if (kind === "saml") {
    if (draft.saml_sso_url.trim() && !isIdpUrl(draft.saml_sso_url.trim())) problems.push("saml_sso_url");
    if (draft.saml_slo_url.trim() && !isIdpUrl(draft.saml_slo_url.trim())) problems.push("saml_slo_url");
  } else {
    const issuer = draft.issuer.trim();
    if (issuer && !isIdpUrl(issuer)) problems.push("issuer");
    if (draft.client_id.trim().length === 0 && issuer) problems.push("client_id");
    if (!draft.scopes.split(/\s+/).includes("openid")) problems.push("scopes");
  }
  const days = Number(draft.valid_days);
  if (!Number.isInteger(days) || days < 1 || days > 3650) problems.push("valid_days");
  return problems;
}

/** A provider's one-word state on the list: enabled, failing its last test, or off. */
export function providerSignal(provider: SsoProvider): Signal {
  if (provider.last_test && !provider.last_test.ok) return "act";
  if (!provider.enabled) return "off";
  return provider.last_test?.ok ? "ok" : "wait";
}

export interface TestRow {
  key: "discovery" | "jwks" | "pkce" | "secret" | "logout";
  s: Signal;
  detail?: string;
}

/** The connection test as a checklist, each line a lamp. */
export function testRows(result: AnyTestResult): TestRow[] {
  const discovered = Boolean(result.issuer);
  return [
    {
      key: "discovery",
      s: discovered ? "ok" : "act",
      // a failure's message is the panel's notice, not repeated on the line
      detail: discovered ? result.issuer : undefined,
    },
    {
      key: "jwks",
      s: result.jwks_keys ? "ok" : discovered ? "act" : "off",
      detail: result.jwks_keys != null ? String(result.jwks_keys) : undefined,
    },
    { key: "pkce", s: result.pkce_s256 === true ? "ok" : result.pkce_s256 === false ? "act" : discovered ? "wait" : "off" },
    { key: "secret", s: result.secret_configured ? "ok" : discovered ? "info" : "off" },
    { key: "logout", s: result.end_session_endpoint ? "ok" : discovered ? "info" : "off" },
  ];
}

export interface SamlTestRow {
  key: "metadata" | "entity" | "cert" | "slo" | "spkey";
  s: Signal;
  detail?: string;
}

/** The SAML connection test: metadata, entity ID / SSO URL, certificate validity, SLO, SP key. */
export function samlTestRows(result: AnyTestResult): SamlTestRow[] {
  const valid = (result.certs ?? []).filter((c) => !c.error && !c.expired);
  const latest = valid.map((c) => c.not_after ?? "").sort().at(-1);
  return [
    {
      key: "metadata",
      s: result.metadata_fetched == null ? "info" : result.metadata_matches ? "ok" : "act",
    },
    { key: "entity", s: result.entity_id && result.sso_url ? "ok" : "act", detail: result.entity_id ?? undefined },
    { key: "cert", s: result.cert_valid ? "ok" : "act", detail: latest ? latest.slice(0, 10) : undefined },
    { key: "slo", s: result.slo_url ? "ok" : "info", detail: result.slo_url ?? undefined },
    { key: "spkey", s: result.sp_key ? "ok" : "act" },
  ];
}

/** Days until a certificate's `not_after` (negative once expired), or null. */
export function daysLeft(notAfter: string | undefined, now: number = Date.now()): number | null {
  if (!notAfter) return null;
  const at = Date.parse(notAfter);
  return Number.isFinite(at) ? Math.floor((at - now) / 86_400_000) : null;
}

export type MappingProblem = "group" | "target" | "role";

export function mappingProblems(row: SsoMapping): MappingProblem[] {
  const problems: MappingProblem[] = [];
  if (!row.group.trim()) problems.push("group");
  if (row.kind !== "role" && !row.target_id) problems.push("target");
  if (!row.role_key) problems.push("role");
  return problems;
}

export function blankMapping(kind: SsoMapping["kind"] = "role"): SsoMapping {
  return { group: "", kind, target_id: "", role_key: kind === "role" ? "member" : "viewer" };
}

/** The create body for a preset: its template's scopes and claims, disabled. */
export function createBody(preset: AnyPreset, name: string, template?: PresetTemplate): SsoProviderWrite {
  return {
    preset,
    name: name.trim(),
    ...(template?.scopes ? { scopes: template.scopes } : {}),
    ...(template ? { claims: { ...template.claims } } : {}),
  };
}

/** Whether a provider can be switched on: its channel's required fields are set. */
export function canEnable(provider: Provider): boolean {
  if (provider.kind === "saml") {
    const saml = provider.saml;
    return Boolean(saml && saml.idp_entity_id && saml.sso_url && saml.idp_certs.length && saml.sp_key_set);
  }
  return Boolean(provider.issuer && provider.client_id);
}

/** Backend `sso_error` code → `auth.sso.errors.*` key; anything else reads as generic. */
const ERROR_KEYS: Record<string, string> = {
  "auth.sso.state_invalid": "stateInvalid",
  "auth.sso.state_expired": "stateExpired",
  "auth.sso.idp_error": "idpError",
  "auth.sso.not_provisioned": "notProvisioned",
  "auth.sso.email_domain_blocked": "domainBlocked",
  "auth.sso.email_taken": "emailTaken",
  "auth.sso.email_missing": "emailMissing",
  "auth.sso.email_invalid": "emailMissing",
  "auth.account_disabled": "accountDisabled",
  "auth.account_pending": "accountDisabled",
  "auth.sso.bad_signature": "verifyFailed",
  "auth.sso.bad_issuer": "verifyFailed",
  "auth.sso.bad_audience": "verifyFailed",
  "auth.sso.bad_nonce": "verifyFailed",
  "auth.sso.expired": "verifyFailed",
  "auth.sso.no_subject": "verifyFailed",
  "auth.sso.no_id_token": "verifyFailed",
  "sso.provider_not_found": "providerGone",
  "sso.idp_unreachable": "providerUnreachable",
  "sso.idp_http_error": "providerUnreachable",
  "sso.idp_bad_response": "providerUnreachable",
  "sso.issuer_mismatch": "providerUnreachable",
  "sso.secret_unreadable": "providerUnreachable",
  // SAML (SSO-2)
  "auth.saml.unsigned": "verifyFailed",
  "auth.saml.bad_signature": "verifyFailed",
  "auth.saml.bad_audience": "verifyFailed",
  "auth.saml.bad_issuer": "verifyFailed",
  "auth.saml.bad_destination": "verifyFailed",
  "auth.saml.bad_subject": "verifyFailed",
  "auth.saml.no_subject": "verifyFailed",
  "auth.saml.not_encrypted": "verifyFailed",
  "auth.saml.malformed": "verifyFailed",
  "auth.saml.invalid": "verifyFailed",
  "auth.saml.expired": "samlExpired",
  "auth.saml.replayed": "samlReplayed",
  "auth.saml.in_response_to": "stateInvalid",
  "auth.saml.unsolicited": "samlUnsolicited",
  "auth.saml.idp_error": "idpError",
  "auth.saml.no_response": "idpError",
  "auth.saml.logout_invalid": "logoutFailed",
  "sso.incomplete": "providerUnreachable",
  "sso.saml_key_unreadable": "providerUnreachable",
  "auth.local_login_hidden": "localHidden",
};

export function ssoErrorKey(code: string): string {
  return `auth.sso.errors.${ERROR_KEYS[code] ?? "generic"}`;
}
