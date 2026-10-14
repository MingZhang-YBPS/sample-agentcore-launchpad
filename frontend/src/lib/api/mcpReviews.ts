/**
 * Typed client of the MCP tool-review API (projects §15.6, backend
 * `routers/mcp_reviews.py`). Every tool list a hosted MCP server presents is
 * snapshotted; until a member holding `mcp.approve` — never the deployer — approves
 * it, the server's Gateway target is withheld. Workspace + project routes: they follow
 * the shared `X-Workspace` / `X-Project` selection like every other agent route.
 */
import { getJson, sendJson } from "../api";

const enc = encodeURIComponent;

export type McpReviewStatus = "reviewing" | "approved" | "rejected" | "superseded";
/** `none` before the server's first tool list */
export type McpGateState = "none" | "reviewing" | "approved" | "rejected";
export type McpReviewSource = "deploy" | "drift" | "baseline";
export type McpHintKind =
  | "dangerous_verb"
  | "write_name"
  | "missing_description"
  | "injection_phrase"
  | "url"
  | "cross_tool_reference"
  | "long_description";

export interface McpReviewedTool {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
}

export interface McpToolDiff {
  added: string[];
  removed: string[];
  description_changed: string[];
  schema_changed: string[];
}

export interface McpHint {
  tool: string;
  kind: McpHintKind;
  detail: string;
}

export interface McpReview {
  id: string;
  agent_id: string;
  version: string | null;
  target_name: string;
  tools_hash: string;
  tool_count: number;
  base_review_id: string | null;
  diff: McpToolDiff;
  hints: McpHint[];
  status: McpReviewStatus;
  source: McpReviewSource;
  requested_by: string;
  decided_by: string | null;
  decided_at: string | null;
  note: string;
  /** after an approval the withheld target is re-created in the background */
  publish_status: "publishing" | "published" | "failed" | null;
  publish_error: string | null;
  created_at: string | null;
}

/** `GET …/reviews/{id}`: the full list, and the approved list it is compared with */
export interface McpReviewDetail extends McpReview {
  tools: McpReviewedTool[];
  base_tools: McpReviewedTool[] | null;
}

export interface McpGate {
  state: McpGateState;
  open_review_id: string | null;
  approved_review_id: string | null;
  approved_hash: string | null;
}

export interface McpReviewList extends McpGate {
  agent_id: string;
  reviews: McpReview[];
}

export interface McpDriftCheck extends McpGate {
  checked: boolean;
  drift: boolean;
  reason?: string;
  review_id?: string;
}

/** `/api/mcp-servers/{agentId}/reviews[/{...rest}]`, each segment encoded. */
export function mcpReviewPath(agentId: string, ...rest: string[]): string {
  return [`/api/mcp-servers/${enc(agentId)}/reviews`, ...rest.map(enc)].join("/");
}

export const listMcpReviews = (agentId: string) => getJson<McpReviewList>(mcpReviewPath(agentId));

export const getMcpReview = (agentId: string, reviewId: string) =>
  getJson<McpReviewDetail>(mcpReviewPath(agentId, reviewId));

export const approveMcpReview = (agentId: string, reviewId: string, note = "") =>
  sendJson<McpReviewDetail>(mcpReviewPath(agentId, reviewId, "approve"), "POST", { note });

export const rejectMcpReview = (agentId: string, reviewId: string, note = "") =>
  sendJson<McpReviewDetail>(mcpReviewPath(agentId, reviewId, "reject"), "POST", { note });

/** Re-sync the live target and compare with the approved list (drift). */
export const checkMcpDrift = (agentId: string) => sendJson<McpDriftCheck>(mcpReviewPath(agentId, "check"), "POST");

/** Which tools a reviewer should read first: changed ones, then hinted ones. */
export function reviewFocus(review: Pick<McpReview, "diff" | "hints">): Set<string> {
  const d = review.diff;
  return new Set([...(d.added ?? []), ...(d.description_changed ?? []), ...(d.schema_changed ?? []), ...review.hints.map((h) => h.tool)]);
}

/** The change marker of one tool in a review (`+` added, `~` changed, `""` same). */
export function changeMark(diff: McpToolDiff, name: string): "+" | "~" | "" {
  if (diff.added?.includes(name)) return "+";
  if (diff.description_changed?.includes(name) || diff.schema_changed?.includes(name)) return "~";
  return "";
}
