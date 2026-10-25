import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { Chooser } from "./NewAgentChooser";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

const render = (initial = {}) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <Chooser onSkip={() => {}} initial={initial} />
    </MemoryRouter>,
  );

describe("Chooser", () => {
  it("asks the three questions and waits before recommending", () => {
    const html = render();
    expect(html).toContain("What do you have right now?");
    expect(html).toContain("What is it for?");
    expect(html).toContain("Skip, show all ways");
    expect(html).toContain("v3-chooser-readout pending");
    expect(html).not.toContain("Create with");
    // presets and the marketplace sit below, outside the questions
    expect(html).toContain('href="/agents/new"');
    expect(html).toContain('href="/v3/fleet?view=market"');
  });

  it("the workshop developer's answers read out Strands with Harness as the alternative and a knowledge base preset", () => {
    const html = render({ have: "nothing", use: "qa", dlc: "full" });
    expect(html).toContain("Create with Strands");
    expect(html).toContain('href="/v2/agents?view=new&amp;method=zip_runtime&amp;kb=1"');
    expect(html).toContain("fastest, no code, but no A/B experiments");
    expect(html).toContain("A knowledge base is preselected");
    // the Strands column of the comparison is lit
    expect(html).toMatch(/<th scope="col" class="lit" aria-current="true">Strands \(zip\)/);
  });

  it("an import has no comparison column and says so", () => {
    const html = render({ have: "runtime" });
    expect(html).toContain('href="/agents/import"');
    expect(html).toContain("is not a build method of its own");
    expect(html).not.toContain('class="lit"');
    expect(html).toContain("Does not change the pick");
  });
});
