/**
 * A stdio-only MCP server deployed behind the platform's streamable-HTTP bridge
 * (`samples/mcp_stdio_bridge/`, spec `mcp.stdio_wrapper`; projects §15.5).
 * code_zip only: `python` stays on the managed Python runtime, `node` builds an
 * image on the bridge's Node base. The command runs without a shell, so the
 * arguments are one per line — exactly the argv the server receives.
 */

export type McpStdioBase = "python" | "node";
export const MCP_STDIO_BASES: McpStdioBase[] = ["python", "node"];

/** `backend/app/schemas/agent.py` McpStdioWrapper */
export interface McpStdioWrapperInput {
  command: string;
  args: string[];
  base: McpStdioBase;
}

/** The wizard's draft of the wrapper. */
export interface McpStdioForm {
  on: boolean;
  base: McpStdioBase;
  command: string;
  /** one argument per line */
  args: string;
}

/** The form field that carries it (`AgentForm.mcpStdio`; absent = off). */
export interface McpStdioFields {
  mcpStdio?: McpStdioForm;
}

export const MCP_STDIO_ARGS_MAX = 32;
const ARG_MAX = 1024;

/** The command each base starts with (and switches to while it is untouched). */
export const MCP_STDIO_DEFAULT_COMMAND: Record<McpStdioBase, string> = { python: "python", node: "node" };

export const MCP_STDIO_OFF: McpStdioForm = { on: false, base: "python", command: "python", args: "" };

export function mcpStdioArgs(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

/** Switch base; a command still on the old base's default follows the new one. */
export function withBase(form: McpStdioForm, base: McpStdioBase): McpStdioForm {
  const untouched = form.command.trim() === "" || form.command.trim() === MCP_STDIO_DEFAULT_COMMAND[form.base];
  return { ...form, base, command: untouched ? MCP_STDIO_DEFAULT_COMMAND[base] : form.command };
}

/** The spec's `mcp.stdio_wrapper`; null when off (or the artifact is not a code zip). */
export function mcpStdioSpec(form: McpStdioForm | undefined, byocKind: string): McpStdioWrapperInput | null {
  if (!form?.on || byocKind !== "code_zip") return null;
  return { command: form.command.trim(), args: mcpStdioArgs(form.args), base: form.base };
}

/** A stored wrapper read back into the form (re-publish). */
export function mcpStdioFromSpec(wrapper: McpStdioWrapperInput | null | undefined): McpStdioForm {
  if (!wrapper) return { ...MCP_STDIO_OFF };
  return { on: true, base: wrapper.base, command: wrapper.command, args: wrapper.args.join("\n") };
}

export interface McpStdioIssues {
  /** empty, or more than one word (it is an argv[0], not a shell line) */
  command: boolean;
  /** too many, or one longer than the backend accepts */
  args: boolean;
}

export function mcpStdioIssues(form: McpStdioForm | undefined, byocKind: string): McpStdioIssues {
  if (!form?.on || byocKind !== "code_zip") return { command: false, args: false };
  const command = form.command.trim();
  const args = mcpStdioArgs(form.args);
  return {
    command: !command || /\s/.test(command) || command.length > 255,
    args: args.length > MCP_STDIO_ARGS_MAX || args.some((a) => a.length > ARG_MAX),
  };
}
