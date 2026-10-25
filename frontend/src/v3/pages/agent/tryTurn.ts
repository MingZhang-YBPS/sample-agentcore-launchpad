import { ApiError } from "../../../lib/api";
import { chatErrorText, sseEvents } from "../../../lib/chat";

/** Read one console chat turn's SSE body (`POST /api/chat/{id}`): the answer text,
 *  each `delta` reported through `onText` as it arrives; an `error` event throws its
 *  envelope as an `ApiError` so the callout keeps the code. */
export async function readTurn(res: Response, onText: (text: string) => void, failedText: string): Promise<string> {
  let text = "";
  for await (const { event, data } of sseEvents(res)) {
    if (event === "delta") {
      text += data.text ?? "";
      onText(text);
    } else if (event === "error") {
      throw new ApiError(data.code ?? "chat.failed", chatErrorText(data, failedText), data.detail ?? null);
    }
  }
  return text;
}
