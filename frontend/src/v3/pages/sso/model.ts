/**
 * The single sign-on page's pure logic: the editable draft of a provider, the patch
 * that saves it, the connection test as a checklist, and mapping-row validation.
 */
import type { Signal } from "../../ui";
import type {
  SsoClaims,
  SsoMapping,
  SsoPreset,
  SsoPresetTemplate,
  SsoProvider,
  SsoProviderWrite,
  SsoTestResult,
} from "../../../lib/api/sso";

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
  };
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
  return patch;
}

export function isDirty(provider: SsoProvider, draft: ProviderDraft): boolean {
  return Object.keys(patchOf(provider, draft)).length > 0;
}

export type DraftProblem = "issuer" | "client_id" | "scopes" | "valid_days";

/** Field problems the form shows before a save round-trip. */
export function draftProblems(draft: ProviderDraft): DraftProblem[] {
  const problems: DraftProblem[] = [];
  const issuer = draft.issuer.trim();
  if (issuer && !/^https:\/\/[^\s/]+/.test(issuer) && !/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?(\/|$)/.test(issuer)) {
    problems.push("issuer");
  }
  if (draft.client_id.trim().length === 0 && issuer) problems.push("client_id");
  if (!draft.scopes.split(/\s+/).includes("openid")) problems.push("scopes");
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
export function testRows(result: SsoTestResult): TestRow[] {
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
export function createBody(preset: SsoPreset, name: string, template?: SsoPresetTemplate): SsoProviderWrite {
  return {
    preset,
    name: name.trim(),
    ...(template ? { scopes: template.scopes, claims: { ...template.claims } } : {}),
  };
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
};

export function ssoErrorKey(code: string): string {
  return `auth.sso.errors.${ERROR_KEYS[code] ?? "generic"}`;
}
