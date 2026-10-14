import { describe, expect, it } from "vitest";

import {
  MCP_STDIO_OFF,
  mcpStdioArgs,
  mcpStdioFromSpec,
  mcpStdioIssues,
  mcpStdioSpec,
  withBase,
} from "./mcp-stdio";

const on = { on: true, base: "python" as const, command: "python", args: "server.py\n  --verbose \n\n" };

describe("mcp stdio wrapper form", () => {
  it("splits arguments one per line and drops blank lines", () => {
    expect(mcpStdioArgs(" a \n\nb c\n")).toEqual(["a", "b c"]);
  });

  it("builds the spec only when on and the artifact is a code zip", () => {
    expect(mcpStdioSpec(on, "code_zip")).toEqual({ command: "python", args: ["server.py", "--verbose"], base: "python" });
    expect(mcpStdioSpec(on, "container_source")).toBeNull();
    expect(mcpStdioSpec({ ...on, on: false }, "code_zip")).toBeNull();
    expect(mcpStdioSpec(undefined, "code_zip")).toBeNull();
  });

  it("round-trips a stored wrapper", () => {
    const stored = { command: "node", args: ["build/index.js", "--port", "0"], base: "node" as const };
    expect(mcpStdioSpec(mcpStdioFromSpec(stored), "code_zip")).toEqual(stored);
    expect(mcpStdioFromSpec(null)).toEqual(MCP_STDIO_OFF);
  });

  it("moves an untouched command with the base and keeps an edited one", () => {
    expect(withBase(on, "node").command).toBe("node");
    expect(withBase({ ...on, command: "uvx" }, "node").command).toBe("uvx");
  });

  it("flags a shell line as the command and too many arguments", () => {
    expect(mcpStdioIssues({ ...on, command: "python server.py" }, "code_zip").command).toBe(true);
    expect(mcpStdioIssues({ ...on, command: " " }, "code_zip").command).toBe(true);
    expect(mcpStdioIssues({ ...on, args: Array(33).fill("a").join("\n") }, "code_zip").args).toBe(true);
    expect(mcpStdioIssues(on, "code_zip")).toEqual({ command: false, args: false });
    expect(mcpStdioIssues({ ...on, command: "" }, "container_image").command).toBe(false);
  });
});
