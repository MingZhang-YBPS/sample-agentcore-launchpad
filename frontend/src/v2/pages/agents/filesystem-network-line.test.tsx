import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { emptyAgentForm } from "../../../lib/agent-spec";

vi.mock("../../../auth/auth-context", () => ({ useAuth: () => ({ isAdmin: false }) }));

const { FilesystemCard } = await import("./MethodSections");

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("FilesystemCard network line", () => {
  it("does not reuse the network card's profile-picker test id", () => {
    // the network card's <Select> owns v2-agent-network-profile; a second element with
    // the same id made selectors (and the workshop capture's highlight) match both
    const form = { ...emptyAgentForm("zip_runtime"), networkProfileId: "np-1", networkVpc: true };
    const html = renderToStaticMarkup(
      <FilesystemCard form={form} set={() => undefined} touched={false} network={false} />,
    );
    expect(html).toContain('data-testid="v2-agent-fs-network-profile"');
    expect(html).not.toContain('data-testid="v2-agent-network-profile"');
  });
});
