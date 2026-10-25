// Client of the account-free annotation surface (Agent-DLC §7.4). Like `lib/review.ts`
// it does NOT go through `api.ts`'s `request`: an annotator has no console session and
// no workspace header — the link in the URL is all the backend needs.
import { ApiError, localizedMessage } from "./api";

export interface AnnotateItem {
  ref: string;
  input: string | null;
  answer: string | null;
  /** Only this annotator's own label — never the judge's, never another person's. */
  my_label: string | null;
  my_rationale: string | null;
  my_answer: string | null;
}

export interface AnnotateQueue {
  label: string;
  criterion_key: string;
  purpose: "judge_calibration" | "golden_answer" | "admission";
  status: "labeling" | "adjudicating" | "closed";
  total: number;
  labelled: number;
  items: AnnotateItem[];
}

async function failure(res: Response): Promise<ApiError> {
  const body = (await res.json().catch(() => ({}))) as { code?: string; message?: string };
  const code = body.code ?? `http.${res.status}`;
  return new ApiError(code, localizedMessage(code, body.message ?? res.statusText), null);
}

const base = (token: string) => `/share/annotate/${encodeURIComponent(token)}`;

export const annotateApi = {
  queue: async (token: string): Promise<AnnotateQueue> => {
    const res = await fetch(base(token));
    if (!res.ok) throw await failure(res);
    return (await res.json()) as AnnotateQueue;
  },
  label: async (
    token: string,
    body: { item_ref: string; label?: string; rationale?: string; answer?: string },
  ): Promise<AnnotateQueue> => {
    const res = await fetch(`${base(token)}/label`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw await failure(res);
    return (await res.json()) as AnnotateQueue;
  },
};
