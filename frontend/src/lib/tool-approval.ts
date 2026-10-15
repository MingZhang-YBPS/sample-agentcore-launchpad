// Tool-call approval in the console chat (strands-builder §2.8, SB-2 Hooks).
//
// A Strands agent with an approval list pauses a listed tool call and the stream
// carries `approval_required` ({tool, tool_use_id, input}; the input is already
// masked by the runtime: a masked argument reads "***"). The console answers with
// the next turn: Approve sends `approved_tools: [tool]` — one approved name lets
// one call of that tool run on that turn. Decline sends nothing: the
// call stays paused and the user tells the agent what to do instead.
// `approval_refused` is the runtime refusing a listed tool for a caller that can
// not approve (never the console chat in practice — shown so it is not lost).
import type { TFunction } from "i18next";

import type { ChatHistoryMessage, ChatStreamPayload } from "./api";

export type ApprovalDecision = "approved" | "declined";

/** One paused (or refused) tool call. */
export interface ToolApproval {
  tool: string;
  toolUseId: string;
  /** the masked tool input as the runtime sent it */
  input: Record<string, unknown>;
  /** `approval_refused`: the runtime refused it outright (no approval possible) */
  refused?: boolean;
  /** restored from history: shown, never actionable (the turn it paused is over) */
  restored?: boolean;
  /** what the user answered on the card */
  decision?: ApprovalDecision;
}

/** What the runtime writes for a masked argument (builder_sb2 `launchpad_mask`). */
export const MASKED = "***";

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

export function liveApproval(payload: Partial<ChatStreamPayload>, refused = false): ToolApproval {
  return {
    tool: payload.tool ?? "",
    toolUseId: payload.tool_use_id ?? "",
    input: isRecord(payload.input) ? payload.input : {},
    ...(refused ? { refused: true } : {}),
  };
}

/** A history row of role `approval`: the tool in `name`, the masked input as JSON in `text`. */
export function restoredApproval(row: Pick<ChatHistoryMessage, "name"> & { text?: string }): ToolApproval {
  let input: Record<string, unknown> = {};
  try {
    const parsed: unknown = row.text ? JSON.parse(row.text) : {};
    if (isRecord(parsed)) input = parsed;
  } catch {
    // an unreadable row still shows the card, just without its input
  }
  return { tool: row.name ?? "", toolUseId: "", input, restored: true };
}

/** The approval card's phase: what it shows and whether its buttons are live. */
export type ApprovalPhase = "pending" | "approved" | "declined" | "refused" | "expired";

export function approvalPhase(approval: ToolApproval, actionable: boolean): ApprovalPhase {
  if (approval.refused) return "refused";
  if (approval.decision) return approval.decision;
  return actionable && !approval.restored ? "pending" : "expired";
}

/**
 * Whether the card at `index` can still be answered: it belongs to the thread's
 * latest turn (after the last user message) and has no decision. A paused call of
 * an older turn is gone — the agent moved on.
 */
export function approvalActionable(messages: { kind: string; approval?: ToolApproval }[], index: number): boolean {
  const card = messages[index]?.approval;
  if (!card || card.refused || card.restored || card.decision) return false;
  for (let i = messages.length - 1; i > index; i--) if (messages[i].kind === "user") return false;
  return true;
}

/** Record the user's answer on the card at `index`. */
export function decideApproval<M extends { approval?: ToolApproval }>(
  messages: M[], index: number, decision: ApprovalDecision,
): M[] {
  return messages.map((m, i) => (i === index && m.approval ? { ...m, approval: { ...m.approval, decision } } : m));
}

/** The prompt of the follow-up turn Approve sends. */
export function approvalPrompt(t: TFunction, tool: string): string {
  return t("chatApproval.prompt", { tool });
}

/** The `approved_tools` request field of a turn that carries approvals; omitted when there are none. */
export function approvedToolsField(approved: string[] | undefined): string[] | undefined {
  const names = (approved ?? []).filter(Boolean);
  return names.length ? [...new Set(names)] : undefined;
}

/** One piece of the masked-input JSON: plain text, or a masked value to draw as a redaction. */
export interface InputPiece {
  text: string;
  masked: boolean;
}

/** The input as pretty JSON, split so every masked value can be drawn as a redaction. */
export function maskedInputPieces(input: Record<string, unknown>): InputPiece[] {
  const json = JSON.stringify(input, null, 2) ?? "{}";
  const token = JSON.stringify(MASKED);
  const pieces: InputPiece[] = [];
  json.split(token).forEach((text, i) => {
    if (i > 0) pieces.push({ text: token, masked: true });
    if (text) pieces.push({ text, masked: false });
  });
  return pieces;
}

/** How many arguments (at any depth) the runtime masked. */
export function maskedCount(input: Record<string, unknown>): number {
  return maskedInputPieces(input).filter((p) => p.masked).length;
}
