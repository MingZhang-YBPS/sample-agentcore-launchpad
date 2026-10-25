/**
 * Console single sign-on, SSO-2 / SSO-3 (sso design §2.1-§2.7): the SAML 2.0 channel,
 * SCIM 2.0 provisioning and the "hide local login" switch. Extends the SSO-1 module
 * (`./sso`) — its OIDC types and calls stay as they are; a provider here is either
 * kind, with a `saml` block (SAML only) and a `scim` block (both). Hub-global: no
 * workspace header. The SP signing key and the SCIM token never come back: the token
 * is in the mint answer only.
 */
import { getJson, sendJson } from "../api";
import type {
  SsoClaims,
  SsoPreset,
  SsoProvider,
  SsoProviderWrite,
  SsoTestResult,
} from "./sso";

const enc = encodeURIComponent;

export type SsoKind = "oidc" | "saml";
export type SamlPreset = "identity-center" | "entra-saml" | "saml";
export type AnyPreset = SsoPreset | SamlPreset;
export const SAML_PRESETS: readonly SamlPreset[] = ["identity-center", "entra-saml", "saml"];
/** The presets with a configuration guide, per channel; the rest are attribute templates. */
export const CHANNEL_PRESETS: Record<SsoKind, readonly AnyPreset[]> = {
  saml: SAML_PRESETS,
  oidc: ["keycloak", "authentik", "oidc"],
};
export const TEMPLATE_PRESETS: readonly AnyPreset[] = ["okta", "entra", "google"];
export const ALL_PRESETS: readonly AnyPreset[] = [...CHANNEL_PRESETS.saml, ...CHANNEL_PRESETS.oidc, ...TEMPLATE_PRESETS];

export function kindOfPreset(preset: AnyPreset): SsoKind {
  return (SAML_PRESETS as readonly string[]).includes(preset) ? "saml" : "oidc";
}

export const NAMEID_FORMATS = [
  "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
  "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
  "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
  "urn:oasis:names:tc:SAML:2.0:nameid-format:transient",
] as const;

export interface CertFacts {
  subject?: string;
  not_after?: string;
  sha256?: string;
  expired?: boolean;
  error?: string;
}

export interface SamlBlock {
  metadata_url: string;
  idp_entity_id: string;
  sso_url: string;
  slo_url: string;
  idp_certs: (CertFacts & { body: string })[];
  name_id_format: string;
  sign_requests: boolean;
  want_encrypted: boolean;
  allow_idp_initiated: boolean;
  sp_entity_id: string;
  acs_url: string;
  sls_url: string;
  metadata_endpoint: string;
  /** the SP's public certificate (PEM); its key is never returned */
  sp_cert: string;
  sp_cert_facts: CertFacts | null;
  sp_key_set: boolean;
}

export interface ScimBlock {
  enabled: boolean;
  base_url: string;
  token_hint: string | null;
  token_created_at: string | null;
  subject: "userName" | "externalId";
  counts?: { users: number; groups: number };
}

export interface SamlTestFields {
  kind?: "saml";
  metadata_fetched?: boolean;
  metadata_matches?: boolean;
  entity_id?: string | null;
  sso_url?: string | null;
  slo_url?: string | null;
  certs?: CertFacts[];
  cert_valid?: boolean;
  sp_cert?: CertFacts;
  sp_key?: boolean;
}
export type AnyTestResult = SsoTestResult & SamlTestFields;

export type Provider = Omit<SsoProvider, "kind" | "preset" | "last_test"> & {
  kind: SsoKind;
  preset: AnyPreset;
  last_test: AnyTestResult | null;
  saml: SamlBlock | null;
  scim: ScimBlock;
};

export interface PresetTemplate {
  kind: SsoKind;
  claims: SsoClaims;
  issuer_hint?: string;
  scopes?: string;
  metadata_hint?: string;
  name_id_format?: string;
}

export interface LoginSettings {
  /** the administrators' switch */
  hide_local_login: boolean;
  /** in effect: the switch is on and some provider is enabled */
  local_login_hidden: boolean;
}

export interface ProviderList {
  items: Provider[];
  presets: Partial<Record<AnyPreset, PresetTemplate>>;
  callback_url_template: string;
  acs_url_template: string;
  scim_base_url: string;
  settings: LoginSettings;
}

export interface SamlWrite {
  saml_metadata_url?: string;
  saml_metadata_xml?: string;
  saml_idp_entity_id?: string;
  saml_sso_url?: string;
  saml_slo_url?: string;
  saml_idp_certs?: string[];
  saml_name_id_format?: string;
  saml_sign_requests?: boolean;
  saml_want_encrypted?: boolean;
  saml_allow_idp_initiated?: boolean;
  scim_subject?: ScimBlock["subject"];
}
export type ProviderWrite = Omit<SsoProviderWrite, "preset"> & { preset?: AnyPreset } & SamlWrite;

export interface PublicProvider {
  id: string;
  name: string;
  kind: SsoKind;
  preset: AnyPreset;
}

/** Where the browser goes to sign in with a provider of either channel. */
export function signInUrl(provider: Pick<PublicProvider, "id" | "kind">, returnTo?: string): string {
  const back = returnTo && returnTo !== "/" ? `?return_to=${enc(returnTo)}` : "";
  return `/api/auth/${provider.kind === "saml" ? "saml" : "oidc"}/${enc(provider.id)}/start${back}`;
}

const base = "/api/admin/sso/providers";

export const ssoAdminApi = {
  /** the login page's buttons and whether the local form is hidden */
  providers: () => getJson<{ providers: PublicProvider[]; hide_local_login?: boolean }>("/api/auth/providers"),

  list: () => getJson<ProviderList>(base),
  get: (id: string) => getJson<Provider>(`${base}/${enc(id)}`),
  create: (body: ProviderWrite) => sendJson<Provider>(base, "POST", body),
  update: (id: string, body: ProviderWrite) => sendJson<Provider>(`${base}/${enc(id)}`, "PATCH", body),
  test: (id: string) => sendJson<AnyTestResult>(`${base}/${enc(id)}/test`, "POST"),
  /** (re)load IdP metadata from a URL (the stored one when omitted) or an uploaded document */
  loadMetadata: (id: string, source: { url?: string; xml?: string }) =>
    sendJson<Provider>(`${base}/${enc(id)}/saml/metadata`, "POST", source),
  rotateSpKey: (id: string) => sendJson<Provider>(`${base}/${enc(id)}/saml/sp-key`, "POST"),
  /** the token is in this answer only */
  mintScimToken: (id: string) => sendJson<{ token: string; scim: ScimBlock }>(`${base}/${enc(id)}/scim/token`, "POST"),
  revokeScimToken: (id: string) => sendJson<{ scim: ScimBlock }>(`${base}/${enc(id)}/scim/token`, "DELETE"),
  settings: () => getJson<LoginSettings>("/api/admin/sso/settings"),
  putSettings: (body: { hide_local_login: boolean }) => sendJson<LoginSettings>("/api/admin/sso/settings", "PUT", body),
};
