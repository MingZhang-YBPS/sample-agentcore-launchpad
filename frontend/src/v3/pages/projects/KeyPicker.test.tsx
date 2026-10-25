import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { KeyPicker } from "./KeyPicker";

const html = (value: string[], strict = false) =>
  renderToStaticMarkup(
    <KeyPicker
      projectKeys={["project.use", "mcp.approve"]}
      agentKeys={["agents.deploy", "release.sign"]}
      value={value}
      onChange={() => undefined}
      strict={strict}
    />,
  );

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("KeyPicker", () => {
  it("labels each key and shows the raw key, mcp.approve included", () => {
    const out = html([]);
    expect(out).toContain("Approve MCP tools");
    expect(out).toContain("<small>mcp.approve</small>");
    expect(out).toContain("Project keys");
    expect(out).not.toContain("Holds both sides");
  });

  it("warns about combined duties, and says a strict project refuses them", () => {
    expect(html(["agents.deploy", "release.sign"])).toContain("Holds both sides of: agents.deploy + release.sign. Allowed");
    expect(html(["agents.deploy", "release.sign"], true)).toContain("This project refuses that combination.");
  });
});
