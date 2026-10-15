import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { SSO_PRESETS, type SsoProvider, ssoStartUrl } from "../../../lib/api/sso";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { hostedGroups } from "../../nav";
import { MappingRow } from "./MappingsEditor";
import {
  blankMapping,
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
  it("shows the three SSO-1 presets first and the templates beneath", () => {
    const html = renderToStaticMarkup(<PresetGrid value="authentik" onPick={() => undefined} />);
    const order = SSO_PRESETS.map((p) => html.indexOf(`sso-preset-${p}`));
    expect(order.every((at) => at >= 0)).toBe(true);
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

describe("navigation and copy", () => {
  it("is an administrator's Configure entry only", () => {
    const tos = (admin: boolean) => hostedGroups(admin).flatMap((g) => g.items.map((i) => i.to));
    expect(tos(true)).toContain("/v3/sso");
    expect(tos(false)).not.toContain("/v3/sso");
  });

  it("names every preset in both languages", () => {
    for (const preset of SSO_PRESETS) {
      for (const bundle of [en, zh]) {
        expect(bundle.auth.sso.preset[preset]).toBeTruthy();
        expect(bundle.auth.sso.presetShort[preset]).toBeTruthy();
        expect(bundle.v3.sso.presetBody[preset]).toBeTruthy();
      }
    }
  });
});
