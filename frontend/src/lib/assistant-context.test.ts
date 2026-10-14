import { describe, expect, it } from "vitest";

import {
  type AssistantLiveMessage,
  contextDividerIndex,
  contextFromMeta,
  conversationContext,
  isContextTrimmed,
} from "./assistant";

const row = (turn: number | undefined, role: AssistantLiveMessage["role"] = "user"): AssistantLiveMessage => ({
  role, text: `t${turn}`, turn,
});

describe("assistant context window", () => {
  const stored = [row(1), row(1, "assistant"), row(2), row(2, "assistant"), row(3), row(3, "assistant")];

  it("draws no divider while every turn is replayed", () => {
    expect(contextDividerIndex(stored, null)).toBe(-1);
    expect(contextDividerIndex(stored, { replayed_turns: 3, omitted_turns: 0, first_replayed_turn: 1 })).toBe(-1);
  });

  it("puts the divider before the first row of the oldest replayed turn", () => {
    expect(contextDividerIndex(stored, { replayed_turns: 1, omitted_turns: 2, first_replayed_turn: 3 })).toBe(4);
  });

  it("skips a turn missing from the transcript (a retried turn is never replayed)", () => {
    const gap = [row(1), row(1, "assistant"), row(3), row(3, "assistant")];
    expect(contextDividerIndex(gap, { replayed_turns: 1, omitted_turns: 1, first_replayed_turn: 2 })).toBe(2);
  });

  it("puts it after the stored rows when no turn fits", () => {
    const live = [...stored, row(undefined), row(undefined, "assistant")];
    expect(contextDividerIndex(live, { replayed_turns: 0, omitted_turns: 3, first_replayed_turn: null })).toBe(6);
    expect(contextDividerIndex(stored, { replayed_turns: 0, omitted_turns: 3, first_replayed_turn: null })).toBe(6);
  });

  it("reads the window from a detail and from a meta event", () => {
    const ctx = { replayed_turns: 12, omitted_turns: 2, first_replayed_turn: 3, budget_used_ratio: 0.4 };
    expect(conversationContext({ id: "c1", context: ctx })).toEqual(ctx);
    expect(conversationContext({ id: "c1" })).toBeNull();
    expect(contextFromMeta({ turn: 15, omitted_turns: 2, first_replayed_turn: 3, context: ctx })).toEqual(ctx);
    expect(contextFromMeta({ turn: 15, omitted_turns: 1 })).toEqual({
      replayed_turns: 0, omitted_turns: 1, first_replayed_turn: null,
    });
    expect(contextFromMeta({ turn: 15 })).toBeNull();
  });

  it("marks only list rows the backend flagged", () => {
    expect(isContextTrimmed({ id: "a", context_trimmed: true })).toBe(true);
    expect(isContextTrimmed({ id: "b", context_trimmed: false })).toBe(false);
    expect(isContextTrimmed({ id: "c" })).toBe(false);
  });
});
