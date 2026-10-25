import { describe, expect, it } from "vitest";

import {
  customToolTemplate,
  emptyStrandsForm,
  nextCustomToolName,
  renameInCode,
  sb3Issues,
  sb3RowIssues,
  type StrandsForm,
  strandsFormFromSpec,
  strandsSpec,
  strandsValid,
} from "./strands-builder";

const ctx = { protocol: "http" as const, modelId: "claude-sonnet-4-5", modelSource: "bedrock" as const, hasKbs: false, guardrail: false };
const form = (over: Partial<StrandsForm>): StrandsForm => ({ ...emptyStrandsForm(), ...over });
const tool = (over: Partial<StrandsForm["customTools"][number]> = {}) => ({
  name: "my_tool",
  description: "Trim a text.",
  code: customToolTemplate("my_tool"),
  requirements: "",
  execution: "runtime" as const,
  ...over,
});

describe("SB-3 spec mapping", () => {
  it("posts nothing new for a form that sets nothing", () => {
    expect(strandsSpec(emptyStrandsForm(), ctx)).toEqual({});
    // a draft saved before SB-3 carries no SB-3 slice at all
    const old: Partial<StrandsForm> = emptyStrandsForm();
    delete old.customTools;
    delete old.sandboxTools;
    expect(strandsSpec(old as StrandsForm, ctx)).toEqual({});
  });

  it("posts only what is set and reads it back", () => {
    const spec = strandsSpec(
      form({
        customTools: [tool({ requirements: "requests==2.32.3\n\nrequests==2.32.3" }), tool({ name: "b", execution: "code_interpreter" })],
        sandboxTools: ["shell", "python_repl"],
      }),
      ctx,
    );
    expect(spec.custom_tools).toEqual([
      { name: "my_tool", description: "Trim a text.", code: customToolTemplate("my_tool"), requirements: ["requests==2.32.3"] },
      { name: "b", description: "Trim a text.", code: customToolTemplate("my_tool"), execution: "code_interpreter" },
    ]);
    expect(spec.sandbox_tools).toEqual(["python_repl", "shell"]);
    const back = strandsFormFromSpec({ ...spec, sandbox_tools: ["shell", "rm_rf"] });
    expect(back.customTools[0].requirements).toBe("requests==2.32.3");
    expect(back.customTools[1].execution).toBe("code_interpreter");
    expect(back.sandboxTools).toEqual(["shell"]);
  });
});

describe("SB-3 checks", () => {
  it("passes a fresh template", () => {
    expect(sb3Issues(form({ customTools: [tool()] }))).toEqual({});
    expect(strandsValid(form({ customTools: [tool()] }), ctx)).toBe(true);
  });

  it("flags names, clashes, pins and sandbox requirements per row", () => {
    const rows = sb3RowIssues(
      form({
        tools: ["think"],
        customTools: [
          tool({ name: "Bad" }),
          tool({ name: "think" }),
          tool({ name: "c", requirements: "requests>=2" }),
          tool({ name: "d", requirements: "x==1", execution: "code_interpreter", description: " " }),
        ],
      }),
    );
    expect(rows.get(0)).toEqual({ customToolName: "Bad" });
    expect(rows.get(1)).toEqual({ customToolDuplicate: "think" });
    expect(rows.get(2)).toEqual({ customToolRequirement: "requests>=2" });
    expect(rows.get(3)).toEqual({ customToolDescription: "", customToolSandboxRequirements: "" });
    expect(strandsValid(form({ customTools: [tool({ name: "Bad" })] }), ctx)).toBe(false);
  });

  it("names and renames tools", () => {
    expect(nextCustomToolName([])).toBe("my_tool");
    expect(nextCustomToolName([tool(), tool({ name: "my_tool_2" })])).toBe("my_tool_3");
    const code = customToolTemplate("my_tool");
    expect(renameInCode(code, "my_tool", "trim")).toContain("def trim(text: str");
    expect(renameInCode("x = 1", "my_tool", "trim")).toBe("x = 1");
  });
});
