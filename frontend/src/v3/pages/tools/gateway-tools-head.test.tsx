import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { GatewayToolTargets } from "./GatewayToolTargets";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("Gateway tool targets page", () => {
  it("opens with the standard V3 page head, the lists before the create form", () => {
    const html = renderToStaticMarkup(<GatewayToolTargets />);
    expect(html).toContain('class="v3-head"');
    expect(html).toContain('<h1 class="v3-title">Gateway tool targets</h1>');
    expect(html).toContain('class="v3-eyebrow">Tools<');
    expect(html.indexOf("v3-head")).toBeLessThan(html.indexOf("Lambda tool targets"));
    expect(html.indexOf("Lambda tool targets")).toBeLessThan(html.lastIndexOf("New Lambda tool target"));
  });

  it("has a Chinese head", async () => {
    await i18n.changeLanguage("zh-CN");
    const html = renderToStaticMarkup(<GatewayToolTargets />);
    expect(html).toContain('<h1 class="v3-title">Gateway 工具目标</h1>');
    await i18n.changeLanguage("en");
  });
});
