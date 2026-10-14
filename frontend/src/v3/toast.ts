import { ApiError, apiErrorFacts } from "../lib/api";

/**
 * The error an `act` toast shows details for: an `ApiError` carrying a request id
 * or AWS facts, retitled with the toast's own text so the headline is not said
 * twice. Anything else ⇒ null, and the toast is the plain text it always was.
 */
export function toastDetail(s: "ok" | "act", text: string, err: unknown): ApiError | null {
  if (s !== "act" || !(err instanceof ApiError) || !apiErrorFacts(err)) return null;
  const copy = new ApiError(err.code, err.message, err.detail, { rawMessage: err.rawMessage, title: text, requestId: err.requestId });
  copy.at = err.at;
  return copy;
}
