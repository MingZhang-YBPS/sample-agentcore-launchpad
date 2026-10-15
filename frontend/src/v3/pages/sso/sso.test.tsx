import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { SSO_PRESETS, ssoStartUrl } from "../../../lib/api/sso";
import { ALL_PRESETS, kindOfPreset, type Provider as SsoProvider, signInUrl } from "../../../lib/api/sso-saml";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { hostedGroups } from "../../nav";
import { MappingRow } from "./MappingsEditor";
import { LoginPageSwitch, ScimPanel } from "./Provisioning";
import { SamlExchange } from "./SamlParts";
import {
  blankMapping,
  canEnable,
  daysLeft,
  samlTestRows,
  splitCerts,
  draftOf,
  draftProblems,
  isDirty,
  mappingProblems,
  parseDomains,
  patchOf,
  providerSignal,
  ssoErrorKey,
  testRows,
} from "./model";
import { LoginsTable, PresetGrid } from "./Sso";

const provider: SsoProvider = {
  id: "kc",
  name: "Acme SSO",
  kind: "oidc",
  preset: "keycloak",
  enabled: true,
  sort_order: 0,
  issuer: "https://idp.test/realms/acme",
  client_id: "launchpad",
  client_secret_set: true,
  client_secret_source: "encrypted",
  client_secret_ref: null,
  scopes: "openid email profile",
  claims: { username: "preferred_username", email: "email", name: "name", groups: "groups" },
  account_policy: "jit",
  allowed_email_domains: ["acme.com"],
  default_role: "member",
  valid_days: 30,
  mappings: [],
  last_test: null,
  callback_url: "https://console.test/api/auth/oidc/kc/callback",
  linked_accounts: 2,
  created_by: "admin",
  created_at: null,
  updated_at: null,
  saml: null,
  scim: { enabled: false, base_url: "https://console.test/api/scim/v2", token_hint: null, token_created_at: null, subject: "userName" },
};

const CERT = "MIIBszCCAVmgAwIBAgIUQ0VSVA==";
const samlProvider: SsoProvider = {
  ...provider,
  id: "ic",
  kind: "saml",
  preset: "identity-center",
  issuer: "",
  client_id: "",
  client_secret_set: false,
  client_secret_source: null,
  callback_url: "https://console.test/api/auth/saml/ic/acs",
  saml: {
    metadata_url: "https://portal.sso.us-east-1.amazonaws.com/saml/metadata/abc",
    idp_entity_id: "https://portal.sso.us-east-1.amazonaws.com/saml/assertion/abc",
    sso_url: "https://portal.sso.us-east-1.amazonaws.com/saml/assertion/abc",
    slo_url: "",
    idp_certs: [{ body: CERT, not_after: "2030-01-01T00:00:00+00:00", expired: false }],
    name_id_format: "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
    sign_requests: false,
    want_encrypted: false,
    allow_idp_initiated: true,
    sp_entity_id: "https://console.test/api/auth/saml/ic/metadata",
    acs_url: "https://console.test/api/auth/saml/ic/acs",
    sls_url: "https://console.test/api/auth/saml/ic/sls",
    metadata_endpoint: "https://console.test/api/auth/saml/ic/metadata",
    sp_cert: "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
    sp_cert_facts: { not_after: "2036-01-01T00:00:00+00:00", sha256: "ab".repeat(32), expired: false },
    sp_key_set: true,
  },
};

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("the provider draft", () => {
  it("sends nothing for an untouched form — never the secret", () => {
    const draft = draftOf(provider);
    expect(patchOf(provider, draft)).toEqual({});
    expect(isDirty(provider, draft)).toBe(false);
  });

  it("sends only the changed fields, the secret only when typed", () => {
    const draft = { ...draftOf(provider), name: " New name ", client_secret: "s3cret", allowed_email_domains: "acme.com\n@Beta.io, acme.com" };
    expect(patchOf(provider, draft)).toEqual({ name: "New name", client_secret: "s3cret", allowed_email_domains: ["acme.com", "beta.io"] });
    expect(patchOf(provider, { ...draftOf(provider), clear_secret: true, client_secret: "ignored" })).toEqual({ client_secret: "" });
  });

  it("flags an http issuer and scopes without openid", () => {
    expect(draftProblems({ ...draftOf(provider), issuer: "http://idp.example.com" })).toContain("issuer");
    expect(draftProblems({ ...draftOf(provider), issuer: "http://localhost:8080/realms/x" })).not.toContain("issuer");
    expect(draftProblems({ ...draftOf(provider), scopes: "email profile" })).toContain("scopes");
    expect(draftProblems({ ...draftOf(provider), valid_days: "0" })).toContain("valid_days");
    expect(draftProblems(draftOf(provider))).toEqual([]);
  });

  it("parses domains from lines, commas and @ prefixes", () => {
    expect(parseDomains(" @Acme.com,beta.io\n\nacme.com ")).toEqual(["acme.com", "beta.io"]);
  });
});

describe("states and checks", () => {
  it("lights a provider by its enablement and last test", () => {
    expect(providerSignal({ ...provider, last_test: null })).toBe("wait");
    expect(providerSignal({ ...provider, enabled: false })).toBe("off");
    expect(providerSignal({ ...provider, last_test: { ok: true, checked_at: "" } })).toBe("ok");
    expect(providerSignal({ ...provider, last_test: { ok: false, checked_at: "" } })).toBe("act");
  });

  it("turns a test result into a checklist", () => {
    const rows = testRows({ ok: true, checked_at: "", issuer: "https://idp.test", jwks_keys: 2, pkce_s256: null, secret_configured: false, end_session_endpoint: null });
    expect(rows.map((r) => [r.key, r.s])).toEqual([
      ["discovery", "ok"], ["jwks", "ok"], ["pkce", "wait"], ["secret", "info"], ["logout", "info"],
    ]);
    const failed = testRows({ ok: false, checked_at: "", error: { code: "sso.idp_unreachable", message: "down" } });
    expect(failed[0]).toMatchObject({ key: "discovery", s: "act" });
    expect(failed[1].s).toBe("off");
  });

  it("validates a mapping row", () => {
    expect(mappingProblems(blankMapping())).toEqual(["group"]);
    expect(mappingProblems({ ...blankMapping("project"), group: "dev" })).toEqual(["target"]);
    expect(mappingProblems({ group: "dev", kind: "project", target_id: "alpha", role_key: "developer" })).toEqual([]);
  });

  it("explains callback errors, generic for an unknown code", () => {
    expect(ssoErrorKey("auth.sso.not_provisioned")).toBe("auth.sso.errors.notProvisioned");
    expect(ssoErrorKey("auth.sso.bad_nonce")).toBe("auth.sso.errors.verifyFailed");
    expect(ssoErrorKey("something.else")).toBe("auth.sso.errors.generic");
    for (const key of [ssoErrorKey("auth.sso.email_taken"), "auth.sso.errors.generic", "auth.sso.errors.contactAdmin"]) {
      expect(i18n.exists(key)).toBe(true);
    }
  });

  it("starts the browser leg on a same-origin path", () => {
    expect(ssoStartUrl("kc")).toBe("/api/auth/oidc/kc/start");
    expect(ssoStartUrl("kc", "/v3/agents?x=1")).toBe("/api/auth/oidc/kc/start?return_to=%2Fv3%2Fagents%3Fx%3D1");
  });
});

describe("rendering", () => {
  it("shows the SAML and OIDC lanes first and the templates beneath", () => {
    const html = renderToStaticMarkup(<PresetGrid value="authentik" onPick={() => undefined} />);
    const order = ALL_PRESETS.map((p) => html.indexOf(`sso-preset-${p}`));
    expect(order.every((at) => at >= 0)).toBe(true);
    expect(html.indexOf("sso-preset-identity-center")).toBeLessThan(html.indexOf("sso-preset-keycloak"));
    expect(html.indexOf("sso-preset-oidc")).toBeLessThan(html.indexOf("Attribute templates"));
    expect(html.indexOf("Attribute templates")).toBeLessThan(html.indexOf("sso-preset-okta"));
    expect(html).toContain('aria-pressed="true"');
  });

  it("renders a project mapping with its target and role choices", () => {
    const html = renderToStaticMarkup(
      <MappingRow
        row={{ group: "/dev", kind: "project", target_id: "alpha", role_key: "developer" }}
        index={0}
        targets={{ projects: [{ id: "alpha", name: "Alpha" }], groups: [], roleTemplates: ["developer", "viewer"] }}
        onChange={() => undefined}
        onRemove={() => undefined}
      />,
    );
    expect(html).toContain('data-kind="project"');
    expect(html).toContain("Alpha");
    expect(html).toContain("Developer");
  });

  it("lists recent sign-ins with their outcome", () => {
    const html = renderToStaticMarkup(
      <LoginsTable items={[
        { at: new Date().toISOString(), outcome: "success", username: "alice", reason: null, jit: true, target: "" },
        { at: new Date().toISOString(), outcome: "refused", username: null, reason: "auth.sso.email_taken", jit: false, target: "" },
      ]} />,
    );
    expect(html).toContain("alice");
    expect(html).toContain("auth.sso.email_taken");
    expect(html).toContain("Signed in");
  });
});

describe("the SAML channel", () => {
  it("sends only SAML fields for a SAML provider, certificates as bodies", () => {
    const draft = draftOf(samlProvider);
    expect(patchOf(samlProvider, draft)).toEqual({});
    const changed = {
      ...draft,
      saml_slo_url: " https://idp.test/slo ",
      saml_idp_certs: `-----BEGIN CERTIFICATE-----\n${CERT}\n-----END CERTIFICATE-----\n-----BEGIN CERTIFICATE-----\nQkJC\n-----END CERTIFICATE-----`,
      saml_allow_idp_initiated: false,
      issuer: "https://ignored.test",
    };
    expect(patchOf(samlProvider, changed)).toEqual({
      saml_slo_url: "https://idp.test/slo",
      saml_idp_certs: [CERT, "QkJC"],
      saml_allow_idp_initiated: false,
    });
  });

  it("splits certificates from PEM blocks or blank-line separated bodies", () => {
    expect(splitCerts("AAA\nBBB\n\nCCC")).toEqual(["AAABBB", "CCC"]);
    expect(splitCerts("")).toEqual([]);
  });

  it("checks SAML URLs, not OIDC fields", () => {
    const draft = { ...draftOf(samlProvider), saml_sso_url: "http://idp.example.com/sso" };
    expect(draftProblems(draft, "saml")).toEqual(["saml_sso_url"]);
    expect(draftProblems(draftOf(samlProvider), "saml")).toEqual([]);
  });

  it("enables only a complete provider of either channel", () => {
    expect(canEnable(samlProvider)).toBe(true);
    expect(canEnable({ ...samlProvider, saml: { ...samlProvider.saml!, idp_certs: [] } })).toBe(false);
    expect(canEnable(provider)).toBe(true);
    expect(canEnable({ ...provider, client_id: "" })).toBe(false);
  });

  it("turns a SAML test into a checklist", () => {
    const rows = samlTestRows({
      ok: true, checked_at: "", metadata_fetched: true, metadata_matches: true, entity_id: "e", sso_url: "s",
      slo_url: null, cert_valid: true, sp_key: true, certs: [{ not_after: "2030-01-01T00:00:00+00:00", expired: false }],
    });
    expect(rows.map((r) => [r.key, r.s])).toEqual([
      ["metadata", "ok"], ["entity", "ok"], ["cert", "ok"], ["slo", "info"], ["spkey", "ok"],
    ]);
    expect(rows[2].detail).toBe("2030-01-01");
    expect(samlTestRows({ ok: false, checked_at: "" })[0].s).toBe("info");
  });

  it("counts certificate days", () => {
    const now = Date.parse("2029-12-01T00:00:00Z");
    expect(daysLeft("2030-01-01T00:00:00+00:00", now)).toBe(31);
    expect(daysLeft("2029-11-30T00:00:00+00:00", now)).toBe(-1);
    expect(daysLeft(undefined)).toBeNull();
  });

  it("starts each channel on its own leg", () => {
    expect(signInUrl({ id: "ic", kind: "saml" }, "/v3/agents")).toBe("/api/auth/saml/ic/start?return_to=%2Fv3%2Fagents");
    expect(signInUrl({ id: "kc", kind: "oidc" })).toBe("/api/auth/oidc/kc/start");
    expect(kindOfPreset("entra-saml")).toBe("saml");
    expect(kindOfPreset("entra")).toBe("oidc");
  });

  it("explains SAML refusals", () => {
    expect(ssoErrorKey("auth.saml.replayed")).toBe("auth.sso.errors.samlReplayed");
    expect(ssoErrorKey("auth.saml.unsigned")).toBe("auth.sso.errors.verifyFailed");
    for (const code of ["auth.saml.expired", "auth.saml.unsolicited", "auth.saml.logout_invalid", "auth.local_login_hidden"]) {
      expect(i18n.exists(ssoErrorKey(code))).toBe(true);
    }
  });

  it("renders the exchange: what to give, and what was received", () => {
    const html = renderToStaticMarkup(<SamlExchange provider={samlProvider} onChanged={() => undefined} />);
    expect(html).toContain('data-received="true"');
    expect(html).toContain("https://console.test/api/auth/saml/ic/acs");
    expect(html).toContain("https://console.test/api/auth/saml/ic/metadata");
    expect(html).toContain("sso-received");
    const waiting = renderToStaticMarkup(
      <SamlExchange provider={{ ...samlProvider, saml: { ...samlProvider.saml!, idp_entity_id: "", idp_certs: [] } }} onChanged={() => undefined} />,
    );
    expect(waiting).toContain('data-received="false"');
    expect(waiting).not.toContain("sso-received");
  });
});

describe("provisioning and the sign-in switch", () => {
  it("shows the SCIM base URL and counts, never a stored token", () => {
    const html = renderToStaticMarkup(
      <ScimPanel
        provider={{ ...samlProvider, preset: "saml", scim: { ...samlProvider.scim, enabled: true, token_hint: "wxyz", token_created_at: null, counts: { users: 12, groups: 3 } } }}
        onScim={() => undefined}
        subject="userName"
        onSubject={() => undefined}
      />,
    );
    expect(html).toContain("https://console.test/api/scim/v2");
    expect(html).toContain(">12<");
    expect(html).toContain("Rotate token");
    expect(html).not.toContain("sso-scim-token");
  });

  it("explains that Identity Center does not push users", () => {
    const html = renderToStaticMarkup(
      <ScimPanel provider={samlProvider} onScim={() => undefined} subject="userName" onSubject={() => undefined} />,
    );
    expect(html).toContain("inbound only");
  });

  it("draws the switch and refuses to hide with no provider enabled", () => {
    const off = renderToStaticMarkup(
      <LoginPageSwitch settings={{ hide_local_login: false, local_login_hidden: false }} enabledCount={0} onChange={() => undefined} />,
    );
    expect(off).toContain('data-hidden="false"');
    expect(off).toContain("disabled");
    const on = renderToStaticMarkup(
      <LoginPageSwitch settings={{ hide_local_login: true, local_login_hidden: false }} enabledCount={0} onChange={() => undefined} />,
    );
    expect(on).toContain('data-hidden="true"');
    expect(on).toContain("No provider is enabled");
  });
});

describe("navigation and copy", () => {
  it("is an administrator's Configure entry only", () => {
    const tos = (admin: boolean) => hostedGroups(admin).flatMap((g) => g.items.map((i) => i.to));
    expect(tos(true)).toContain("/v3/sso");
    expect(tos(false)).not.toContain("/v3/sso");
  });

  it("names every preset in both languages", () => {
    for (const preset of [...SSO_PRESETS, ...ALL_PRESETS]) {
      for (const bundle of [en, zh]) {
        expect(bundle.auth.sso.preset[preset]).toBeTruthy();
        expect(bundle.auth.sso.presetShort[preset]).toBeTruthy();
        expect(bundle.v3.sso.presetBody[preset]).toBeTruthy();
      }
    }
  });
});
