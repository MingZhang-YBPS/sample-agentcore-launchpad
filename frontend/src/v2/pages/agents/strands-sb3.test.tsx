import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { emptyAgentForm } from "../../../lib/agent-spec";
import { customToolTemplate, emptyStrandsForm, type StrandsForm } from "../../../lib/strands-builder";
import { CustomToolsPanel } from "./CustomToolEditor";
import { StrandsToolPicker } from "./StrandsSections";

const form = (over: Partial<StrandsForm>) => ({
  ...emptyAgentForm("zip_runtime"),
  name: "caller",
  systemPrompt: "Be brief.",
  modelId: "claude-sonnet-4-5",
  modelSource: "bedrock" as const,
  strands: { ...emptyStrandsForm(), ...over },
});
const set = () => undefined;

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("SB-3 wizard panels", () => {
  it("puts the sandbox enclosure and the custom tools in the tools group", () => {
    const html = renderToStaticMarkup(<StrandsToolPicker form={form({ sandboxTools: ["shell"] })} set={set} touched={false} />);
    expect(html).toContain('data-testid="sb-sandbox-tools"');
    expect(html).toContain("AgentCore Code Interpreter");
    expect(html).toContain('data-testid="sb-sandbox-python_repl"');
    expect(html).toContain('data-testid="sb-custom-tools"');
    expect(html).not.toContain("are not offered yet");
  });

  it("draws a bench per tool: slab with a gutter, where it runs, an unchecked verdict", () => {
    const code = customToolTemplate("trim");
    const html = renderToStaticMarkup(
      <CustomToolsPanel
        form={form({ customTools: [{ name: "trim", description: "Trim.", code, requirements: "", execution: "code_interpreter" }] })}
        set={set}
        touched={false}
      />,
    );
    expect(html).toContain('data-testid="sb-custom-tool-code-0"');
    expect(html.match(/<span>\d+<\/span>/g)?.length).toBe(code.split("\n").length);
    expect(html).toContain("ct-where code_interpreter");
    expect(html).toContain("The sandbox installs nothing");
    expect(html).toContain("not checked");
  });

  it("shows a row's own issue without waiting for continue", () => {
    const html = renderToStaticMarkup(
      <CustomToolsPanel
        form={form({ customTools: [{ name: "Bad", description: "", code: "x", requirements: "", execution: "runtime" }] })}
        set={set}
        touched={false}
      />,
    );
    expect(html).toContain("Tool names start with a lowercase letter");
    expect(html).not.toContain("Say when to call the tool");
    const touched = renderToStaticMarkup(
      <CustomToolsPanel
        form={form({ customTools: [{ name: "ok", description: "", code: "x", requirements: "", execution: "runtime" }] })}
        set={set}
        touched
      />,
    );
    expect(touched).toContain("Say when to call the tool");
  });
});
