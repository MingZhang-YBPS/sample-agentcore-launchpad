import type { ChatAttachmentMetadata, FeedbackVerdict } from "../../../lib/api";
import type { PolicyDeny } from "../../../lib/policy-deny";
import type { AuthAsk } from "../../../lib/user-grants";

export { retryPromptFor } from "../../../lib/user-grants";

export interface ChatMessage {
  kind: "user" | "agent" | "tool" | "memory" | "error" | "auth" | "policy";
  text: string;
  name?: string;
  streaming?: boolean;
  attachments?: ChatAttachmentMetadata[];
  /** kind "auth": the as_user consent ask */
  auth?: AuthAsk;
  /** ledger id of a persisted agent answer — what a thumbs verdict attaches to */
  id?: number;
  /** the viewer's current verdict on this answer */
  verdict?: FeedbackVerdict | null;
  /** T35: a curated answer (rule), not the model, produced this reply */
  curated?: boolean;
  /** kind "policy": a Gateway tool call a Cedar policy denied */
  policy?: PolicyDeny;
  /** kind "tool" of a live turn: the call's run (status, duration); a restored row has none */
  run?: import("../../../lib/chat").ToolRun;
  /** the live turn a tool row belongs to (tool ids repeat across turns) */
  turn?: number;
}

/** Append a streamed delta. An auth card asked mid-answer sits after the open
 * bubble without closing it, so the text around the card stays one bubble. */
export function appendDelta(
  messages: ChatMessage[],
  text: string,
  open: boolean,
  curated = false,
): ChatMessage[] {
  const next = [...messages];
  let i = next.length - 1;
  while (i >= 0 && next[i].kind === "auth") i -= 1;
  const bubble = next[i];
  if (open && bubble?.kind === "agent" && bubble.streaming) {
    next[i] = { ...bubble, text: bubble.text + text };
  } else {
    // T35: `curated` marks a bubble a curated answer rule wrote, not the model
    next.push(curated ? { kind: "agent", text, streaming: true, curated } : { kind: "agent", text, streaming: true });
  }
  return next;
}

/**
 * The error item a turn retry attaches to: the latest error after the thread's last
 * user message (only the latest turn can be retried), and whether that turn carried
 * files (a turn with attachments is not retried — the user sends it again). Null
 * when the latest turn shows no error.
 */
export function latestRetryTarget(messages: ChatMessage[]): { index: number; attachments: boolean } | null {
  let user = -1;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].kind === "user") {
      user = i;
      break;
    }
  }
  if (user < 0) return null;
  for (let i = messages.length - 1; i > user; i -= 1) {
    if (messages[i].kind === "error") return { index: i, attachments: !!messages[user].attachments?.length };
  }
  return null;
}
