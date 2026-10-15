import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../auth/auth-context";
import i18n from "../i18n";
import { ApiError, parseNextSteps } from "../lib/api";
import { ErrorCallout } from "./ErrorCallout";

const auth = (isAdmin: boolean): AuthContextValue => ({
  authRequired: true, username: "u", role: isAdmin ? "admin" : "member", email: null, accountExpiresAt: null,
  builtin: false, accountSource: null, isAdmin, can: () => true, logout: async () => undefined,
});

const html = (error: unknown, { isAdmin = false, params = {} }: { isAdmin?: boolean; params?: Record<string, string> } = {}) =>
  renderToStaticMarkup(
    <AuthContext.Provider value={auth(isAdmin)}>
      <MemoryRouter>
        <ErrorCallout error={error} params={params} onDismiss={() => undefined} />
      </MemoryRouter>
    </AuthContext.Provider>,
  );

const apiError = (code: string, extra: ConstructorParameters<typeof ApiError>[3] = {}) =>
  new ApiError(code, i18n.t(`apiErrors.${code}`), null, { title: i18n.t(`apiErrors.${code}`), ...extra });

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("ErrorCallout (guided-console §3.4)", () => {
  it("renders nothing without an error", () => {
    expect(html(null)).toBe("");
    expect(html("")).toBe("");
  });

  it("three lines: what went wrong, why, and the way out", () => {
    const out = html(apiError("release.no_criteria", { next: [{ action: "criteria.publish", params: { agent_id: "ag-1" } }] }));
    expect(out).toContain('data-code="release.no_criteria"');
    expect(out).toContain("no published criteria table");
    expect(out).toContain('data-testid="error-callout-why"');
    expect(out).toContain("criterion by criterion");
    expect(out).toContain('href="/v3/standards?agent=ag-1&amp;view=criteria"');
    expect(out).toContain("Publish the criteria table");
    expect(out).toContain('role="alert"');
  });

  it("the first action is primary, later ones secondary; unknown actions are dropped", () => {
    const out = html(apiError("release.empty_golden", {
      next: [
        { action: "golden.open", params: { dataset_id: "gs-1" } },
        { action: "doc.golden_format", params: {} },
        { action: "admission.open", params: {} },
      ],
    }), { params: { agent_id: "ag-1" } });
    expect(out).toMatch(/class="v3-btn sm primary"[^>]*href="\/v3\/standards\?agent=ag-1&amp;view=golden&amp;golden=gs-1"|href="\/v3\/standards\?agent=ag-1&amp;view=golden&amp;golden=gs-1"[^>]*class="v3-btn sm primary"/);
    expect(out).toContain('data-action="admission.open"');
    expect(out).not.toContain("doc.golden_format");
  });

  it("a permission it needs: who holds it, and the Users link only for administrators", () => {
    const err = apiError("release.self_sign", { needs: "perm:release.sign" });
    const member = html(err);
    expect(member).toContain("release.sign");
    expect(member).toContain("release-ops");
    expect(member).toContain("ask an administrator");
    expect(member).not.toContain('href="/v3/users"');
    expect(member).toContain('data-s="wait"');
    const admin = html(err, { isAdmin: true });
    expect(admin).toContain('href="/v3/users"');
  });

  it("no next and no needs: the headline alone, still dismissable", () => {
    const out = html(apiError("golden.full"));
    expect(out).toContain("This split is full.");
    expect(out).not.toContain("error-callout-next");
    expect(out).not.toContain("error-callout-why");
    expect(out).toContain('aria-label="Dismiss"');
  });

  it("a page's own validation string reads as the headline", () => {
    expect(html("Choose an agent")).toContain("Choose an agent");
  });

  it("an AWS error keeps the collapsible details", () => {
    const err = new ApiError("aws.validation", "headline", { aws_error_code: "ValidationException" }, { title: "headline", requestId: "r-1" });
    expect(html(err)).toContain("<details");
  });
});

describe("parseNextSteps (the envelope's next)", () => {
  it("keeps well-formed entries and string params only", () => {
    expect(parseNextSteps([
      { action: "golden.open", params: { dataset_id: "gs-1", n: 2, bad: { x: 1 } } },
      { action: "" },
      "nope",
      { params: {} },
      { action: "agent.create" },
    ])).toEqual([
      { action: "golden.open", params: { dataset_id: "gs-1", n: "2" } },
      { action: "agent.create", params: {} },
    ]);
    expect(parseNextSteps(undefined)).toEqual([]);
  });
});
