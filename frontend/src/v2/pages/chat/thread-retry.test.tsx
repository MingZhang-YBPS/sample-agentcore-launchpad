import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { latestRetryTarget, type ChatMessage } from "./messages";
import { Thread } from "./Thread";

const FILE = { name: "a.txt", media_type: "text/plain", size: 3, delivery: "text" } as const;

const render = (messages: ChatMessage[], onRetryTurn?: () => void) =>
  renderToStaticMarkup(
    <Thread messages={messages} userLabel="me" agentLabel="bot" restoring={false} onRetryTurn={onRetryTurn} />,
  );

describe("v2 chat turn retry", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  it("targets the latest error of the latest turn only", () => {
    const failed: ChatMessage[] = [
      { kind: "user", text: "q1" },
      { kind: "error", text: "old" },
      { kind: "user", text: "q2" },
      { kind: "error", text: "boom" },
    ];
    expect(latestRetryTarget(failed)).toEqual({ index: 3, attachments: false });
    expect(latestRetryTarget([...failed, { kind: "user", text: "q3" }])).toBeNull();
    expect(latestRetryTarget([{ kind: "user", text: "q", attachments: [FILE] }, { kind: "error", text: "x" }])).toEqual({
      index: 1,
      attachments: true,
    });
  });

  it("renders the Retry button inside the error item when offered", () => {
    const messages: ChatMessage[] = [
      { kind: "user", text: "q" },
      { kind: "error", text: "boom" },
    ];
    const html = render(messages, () => {});
    expect(html).toContain('data-testid="chat-retry"');
    expect(html.indexOf("chat-retry")).toBeGreaterThan(html.indexOf("v2-chat-error"));
    expect(render(messages)).not.toContain('data-testid="chat-retry"');
  });

  it("a turn with files says it is not retried instead of offering the button", () => {
    const html = render([{ kind: "user", text: "q", attachments: [FILE] }, { kind: "error", text: "boom" }], () => {});
    expect(html).not.toContain('data-testid="chat-retry"');
    expect(html).toContain('data-testid="chat-retry-attachments"');
    expect(html).toContain(i18n.t("v3.chat.retry.attachments").split(" — ")[0]);
  });
});
