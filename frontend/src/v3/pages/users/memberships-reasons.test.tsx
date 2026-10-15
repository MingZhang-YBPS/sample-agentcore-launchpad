import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { MembershipRows } from "./Memberships";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

const targets = [{ id: "risk", name: "Risk assistant" }];
const rolesFor = () => [{ key: "developer", label: "Developer" }];

const render = (rows: { id: string; role: string }[], busy = false) =>
  renderToStaticMarkup(
    <MembershipRows kind="projects" rows={rows} onChange={() => {}} targets={targets} rolesFor={rolesFor} busy={busy} />,
  );

describe("MembershipRows disabled reasons (storyline-ux §4)", () => {
  it("says the role waits for a project, and add waits for a free target", () => {
    const html = render([{ id: "", role: "" }]);
    expect(html).toContain("Choose a project first.");
    // the only target is not taken by an empty row, so add stays on
    expect(html).not.toContain("Every one is already in the list.");
    const full = render([{ id: "risk", role: "developer" }]);
    expect(full).toContain("Every one is already in the list.");
    expect(full).not.toContain("Choose a project first.");
  });

  it("shows no reason while a save runs: busy is not a reason", () => {
    const html = render([{ id: "", role: "" }], true);
    expect(html).not.toContain("v3-why-tip");
  });
});
