import { describe, expect, it } from "vitest";

import { followsOwnTurn, isNearBottom } from "./useStickToBottom";

describe("isNearBottom", () => {
  it("is true at the bottom and within the threshold", () => {
    expect(isNearBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 600 })).toBe(true);
    expect(isNearBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 560 })).toBe(true);
  });
  it("is false once the reader scrolled up to read", () => {
    expect(isNearBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 500 })).toBe(false);
    expect(isNearBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 0 })).toBe(false);
  });
  it("treats a thread shorter than its box as at the bottom", () => {
    expect(isNearBottom({ scrollHeight: 300, clientHeight: 400, scrollTop: 0 })).toBe(true);
  });
});

describe("followsOwnTurn", () => {
  it("re-attaches when the reader adds a turn", () => {
    expect(followsOwnTurn(4, 5, true)).toBe(true);
  });
  it("does not drag a reader down for the agent's own output", () => {
    expect(followsOwnTurn(4, 5, false)).toBe(false);
    expect(followsOwnTurn(5, 5, true)).toBe(false); // a streamed delta, same count
  });
  it("re-attaches when a thread fills from empty (a restored conversation)", () => {
    expect(followsOwnTurn(0, 12, false)).toBe(true);
  });
});
