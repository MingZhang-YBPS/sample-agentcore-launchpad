/**
 * Custom Python tools and the sandboxed built-in tools (strands-builder SB-3) —
 * backend `routers/custom_tools.py`: the wizard's code check, an administrator's
 * approval of one exact version (by code hash), and the workspace's sandbox setting.
 */
import { getJson, sendJson } from "../api";
import type { CustomToolExecution, SandboxTool } from "../strands-builder";

export interface CustomToolFinding {
  /** import | dangerous_import | call | open | dunder | import_time | requirement */
  kind: string;
  /** 0 = not a source line (a requirement) */
  line: number;
  message: string;
}

export interface CustomToolApproval {
  code_hash: string;
  tool_name: string;
  execution: CustomToolExecution;
  requirements: string[];
  findings: CustomToolFinding[];
  approved_by: string;
  approved_at: string;
  note: string;
}

export interface CustomToolCheck {
  /** false = a syntax / structure error: the tool cannot deploy as written */
  ok: boolean;
  error: { message: string; line: number; col: number } | null;
  findings: CustomToolFinding[];
  code_hash: string;
  input_schema: { properties?: Record<string, { type?: string }>; required?: string[] } | null;
  needs_approval: boolean;
  approval: CustomToolApproval | null;
}

export interface ToolVersion {
  name: string;
  code: string;
  requirements: string[];
  execution: CustomToolExecution;
}

export interface SandboxSetting {
  allowed_tools: SandboxTool[];
  code_interpreter_id: string | null;
  effective_code_interpreter_id: string;
  updated_by?: string | null;
  updated_at?: string | null;
}

export const customToolsApi = {
  check: (version: ToolVersion) => sendJson<CustomToolCheck>("/api/custom-tools/check", "POST", version),
  approve: (version: ToolVersion & { description?: string; note?: string }) =>
    sendJson<CustomToolApproval>("/api/custom-tools/approvals", "POST", version),
  revoke: (codeHash: string) =>
    sendJson<{ revoked: string }>(`/api/custom-tools/approvals/${encodeURIComponent(codeHash)}`, "DELETE"),
  sandbox: () => getJson<SandboxSetting>("/api/custom-tools/sandbox"),
  setSandbox: (allowed: SandboxTool[], codeInterpreterId: string | null) =>
    sendJson<SandboxSetting>("/api/custom-tools/sandbox", "PUT", {
      allowed_tools: allowed,
      code_interpreter_id: codeInterpreterId,
    }),
};
