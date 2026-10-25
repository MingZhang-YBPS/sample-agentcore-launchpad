// Client of the account-free `/share/*` surface (T13). It deliberately does NOT
// go through `api.ts`'s `request`: a share visitor has no console session, so
// nothing here may raise the console's "session expired" event, and no
// `X-Workspace` header is sent (the link itself decides the workspace).
import type { ChatStreamPayload, FeedbackVerdict } from "./api";
import { ApiError, localizedMessage } from "./api";

export interface ShareInfo {
  kind: string;
  label: string;
  agent: { display_name: string };
  expires_at: string | null;
}

async function failure(res: Response): Promise<ApiError> {
  const body = (await res.json().catch(() => ({}))) as { code?: string; message?: string };
  const code = body.code ?? `http.${res.status}`;
  return new ApiError(code, localizedMessage(code, body.message ?? res.statusText), null);
}

const base = (token: string) => `/share/${encodeURIComponent(token)}`;

export const shareApi = {
  info: async (token: string): Promise<ShareInfo> => {
    const res = await fetch(base(token));
    if (!res.ok) throw await failure(res);
    return (await res.json()) as ShareInfo;
  },
  /** Raw SSE response — read it with `sseEvents`. */
  chat: async (
    token: string,
    body: { prompt: string; session_id: string | null },
  ): Promise<Response> => {
    const res = await fetch(`${base(token)}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw await failure(res);
    return res;
  },
  rate: async (
    token: string,
    body: { session_id: string; message_id: number; verdict: FeedbackVerdict | "none"; comment?: string },
  ): Promise<void> => {
    const res = await fetch(`${base(token)}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw await failure(res);
  },
};

export type { ChatStreamPayload };
