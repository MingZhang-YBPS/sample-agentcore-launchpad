/**
 * Console single sign-on (sso design §2.6, SSO-1: OIDC). The login page's public
 * provider list and the browser leg (`/api/auth/oidc/{id}/start`, a full-page
 * navigation, not a fetch), and the administrator's IdP configuration. Hub-global:
 * no workspace header. The client secret is write-only — answers say whether one
 * is set, never its value.
 */
import { getJson, sendJson } from "../api";

const enc = encodeURIComponent;

export type SsoPreset = "keycloak" | "authentik" | "okta" | "entra" | "google" | "oidc";
export const SSO_PRESETS: readonly SsoPreset[] = ["keycloak", "authentik", "oidc", "okta", "entra", "google"];
/** The presets SSO-1 ships configuration steps for; the rest are attribute templates. */
export const PRIMARY_PRESETS: ReadonlySet<SsoPreset> = new Set(["keycloak", "authentik", "oidc"]);

export interface SsoPublicProvider {
  id: string;
  name: string;
  kind: "oidc";
  preset: SsoPreset;
}

export type SsoRole = "admin" | "steward" | "operator" | "member";
export const SSO_ROLES: readonly SsoRole[] = ["member", "operator", "steward", "admin"];
export type SsoMappingKind = "role" | "project" | "group";

export interface SsoMapping {
  group: string;
  kind: SsoMappingKind;
  /** a project id / project-group id; "" for a role mapping */
  target_id: string;
  /** a global role (role kind) or a project role template */
  role_key: string;
}

export interface SsoClaims {
  username: string;
  email: string;
  name: string;
  groups: string;
}

export interface SsoTestResult {
  ok: boolean;
  checked_at: string;
  issuer?: string;
  authorization_endpoint?: string;
  token_endpoint?: string;
  userinfo_endpoint?: string | null;
  end_session_endpoint?: string | null;
  scopes_supported?: string[] | null;
  /** null: discovery does not say */
  pkce_s256?: boolean | null;
  jwks_keys?: number;
  secret_configured?: boolean;
  error?: { code: string; message: string };
}

export interface SsoProvider {
  id: string;
  name: string;
  kind: "oidc";
  preset: SsoPreset;
  enabled: boolean;
  sort_order: number;
  issuer: string;
  client_id: string;
  client_secret_set: boolean;
  client_secret_source: "encrypted" | "secrets_manager" | null;
  client_secret_ref: string | null;
  scopes: string;
  claims: SsoClaims;
  account_policy: "jit" | "existing_only";
  allowed_email_domains: string[];
  default_role: SsoRole;
  valid_days: number;
  mappings: SsoMapping[];
  last_test: SsoTestResult | null;
  callback_url: string;
  linked_accounts: number;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface SsoPresetTemplate {
  issuer_hint: string;
  scopes: string;
  claims: SsoClaims;
}

export interface SsoProviderList {
  items: SsoProvider[];
  presets: Record<SsoPreset, SsoPresetTemplate>;
  callback_url_template: string;
}

/** Create / patch body: `client_secret` "" clears it, an `arn:aws:secretsmanager:` value is a reference. */
export type SsoProviderWrite = Partial<
  Pick<SsoProvider, "id" | "name" | "preset" | "enabled" | "sort_order" | "issuer" | "client_id" | "scopes"
    | "claims" | "account_policy" | "allowed_email_domains" | "default_role" | "valid_days">
> & { client_secret?: string };

export interface SsoLogin {
  at: string | null;
  outcome: "success" | "refused" | "failed";
  username: string | null;
  reason: string | null;
  jit: boolean;
  target: string;
}

export interface SsoAccount {
  source: "local" | "sso";
  sso: {
    provider_id: string;
    provider_name: string | null;
    subject: string | null;
    last_login_at: string | null;
    role_conflict: string | null;
  } | null;
}

/** Where the browser goes to sign in with `providerId`, landing back on `returnTo`. */
export function ssoStartUrl(providerId: string, returnTo?: string): string {
  const back = returnTo && returnTo !== "/" ? `?return_to=${enc(returnTo)}` : "";
  return `/api/auth/oidc/${enc(providerId)}/start${back}`;
}

/** The `?sso_error=` a failed callback lands on (pure: `clearSsoError` removes it). */
export function readSsoError(): { code: string; provider: string | null } | null {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("sso_error");
  return code ? { code, provider: params.get("sso_provider") } : null;
}

/** Drop the callback's error from the address bar, so a reload does not show it again. */
export function clearSsoError(): void {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("sso_error")) return;
  url.searchParams.delete("sso_error");
  url.searchParams.delete("sso_provider");
  window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
}

/** `?local=1` keeps the username / password form open (the built-in admin's way in). */
export function wantsLocalLogin(): boolean {
  return new URLSearchParams(window.location.search).get("local") === "1";
}

const base = "/api/admin/sso/providers";

export const ssoApi = {
  providers: () => getJson<{ providers: SsoPublicProvider[] }>("/api/auth/providers"),
  /** `POST /api/auth/logout`; an SSO session's answer names the IdP's logout page. */
  logout: () => sendJson<{ ok: boolean; end_session_url?: string }>("/api/auth/logout", "POST"),

  list: () => getJson<SsoProviderList>(base),
  get: (id: string) => getJson<SsoProvider>(`${base}/${enc(id)}`),
  create: (body: SsoProviderWrite) => sendJson<SsoProvider>(base, "POST", body),
  update: (id: string, body: SsoProviderWrite) => sendJson<SsoProvider>(`${base}/${enc(id)}`, "PATCH", body),
  remove: (id: string) => sendJson<{ ok: boolean }>(`${base}/${enc(id)}`, "DELETE"),
  test: (id: string) => sendJson<SsoTestResult>(`${base}/${enc(id)}/test`, "POST"),
  putMappings: (id: string, mappings: SsoMapping[]) =>
    sendJson<{ mappings: SsoMapping[]; default_role: SsoRole }>(`${base}/${enc(id)}/mappings`, "PUT", { mappings }),
  logins: (id: string) => getJson<{ items: SsoLogin[] }>(`${base}/${enc(id)}/logins`),
  account: (userId: string) => getJson<SsoAccount>(`/api/admin/sso/users/${enc(userId)}`),
};
