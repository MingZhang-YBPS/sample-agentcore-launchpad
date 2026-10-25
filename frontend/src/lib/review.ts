// Client of the account-free SME review surface (T34). Like `lib/share.ts` it does NOT
// go through `api.ts`'s `request`: a reviewer has no console session and no workspace
// header -- the link in the URL is all the backend needs.
import { ApiError, localizedMessage } from "./api";

export interface ReviewItem {
  message_id: number;
  question: string;
  answer: string;
  /** a curated answer (rule), not the model, produced it */
  curated: boolean;
  verdict: "up" | "down" | null;
  comment: string | null;
  correction: string | null;
}

export interface ReviewQueue {
  kind: string;
  label: string;
  agent: { display_name: string };
  expires_at: string | null;
  items: ReviewItem[];
}

async function failure(res: Response): Promise<ApiError> {
  const body = (await res.json().catch(() => ({}))) as { code?: string; message?: string };
  const code = body.code ?? `http.${res.status}`;
  return new ApiError(code, localizedMessage(code, body.message ?? res.statusText), null);
}

const base = (token: string) => `/share/review/${encodeURIComponent(token)}`;

export const reviewApi = {
  queue: async (token: string): Promise<ReviewQueue> => {
    const res = await fetch(base(token));
    if (!res.ok) throw await failure(res);
    return (await res.json()) as ReviewQueue;
  },
  rate: async (
    token: string,
    body: { message_id: number; verdict: "up" | "down" | "none"; comment?: string; correction?: string },
  ): Promise<void> => {
    const res = await fetch(`${base(token)}/rate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw await failure(res);
  },
};
