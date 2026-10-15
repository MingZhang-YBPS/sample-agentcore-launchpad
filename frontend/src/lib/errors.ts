// The console's one wording of an error (storyline-ux §4, SG-5): V2 and V3 alike show
// a backend code as its `apiErrors.<code>` copy in the active locale — the backend's
// own `message` is operator-facing log English, kept only where no copy exists (and,
// for `aws.*`, appended after the copy: only AWS's words say which parameter).
// `backend/tests/test_error_copy.py` fails when a code the backend raises has no copy.
import { ApiError, errorMessage, localizedMessage, messageWithRaw } from "./api";

/**
 * An envelope's `{code, message}` as the console says it — for the places that get
 * the pair without an `ApiError` (an SSE `error` event, a job's stored failure).
 * No code ⇒ the message as sent.
 */
export function envelopeText(code: string | null | undefined, message: string): string {
  return code ? messageWithRaw(code, message) : message;
}

/**
 * Any caught error as the console says it, re-read in the current language (an
 * `ApiError` was worded when it arrived). A fetch-level failure gets the network copy.
 */
export function errorText(err: unknown): string {
  if (err instanceof ApiError) return envelopeText(err.code, err.rawMessage);
  return errorMessage(err);
}

/** The headline alone (no AWS text), for a title line above the details. */
export function errorTitle(err: unknown): string {
  if (err instanceof ApiError) return localizedMessage(err.code, err.rawMessage);
  return errorMessage(err);
}
