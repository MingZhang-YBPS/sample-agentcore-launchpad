import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api";
import {
  type AssistantContextDigest,
  conversationDigest,
  digestPending,
  regenerateDigest,
} from "./assistant";

const here = dirname(fileURLToPath(import.meta.url));
const digestKeys = (lang: string) =>
  JSON.parse(readFileSync(resolve(here, `../locales/${lang}/common.json`), "utf-8")).v3.assistant.digest as Record<string, string>;

const digest = (over: Partial<AssistantContextDigest> = {}): AssistantContextDigest => ({
  text: "## Confirmed items (turns 1–2)\n- Constraint: data stays in region (turn 2, member stated)",
  through_turn: 2,
  updated_at: "2026-10-15T00:00:00Z",
  generating: false,
  ...over,
});

describe("assistant confirmed-items digest", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reads the digest off a conversation detail, null from an older backend", () => {
    expect(conversationDigest({ context_digest: digest() })?.through_turn).toBe(2);
    expect(conversationDigest({})).toBeNull();
    expect(conversationDigest(null)).toBeNull();
  });

  it("is pending while the omitted turns are not all covered or a refresh runs", () => {
    const ctx = { replayed_turns: 12, omitted_turns: 3, first_replayed_turn: 4 };
    expect(digestPending(ctx, digest({ through_turn: 3 }))).toBe(false);
    expect(digestPending(ctx, digest({ through_turn: 2 }))).toBe(true);
    expect(digestPending(ctx, digest({ text: null, through_turn: null }))).toBe(true);
    expect(digestPending(ctx, digest({ through_turn: 3, generating: true }))).toBe(true);
    // nothing omitted, an older backend, or nothing replayed: no polling
    expect(digestPending({ ...ctx, omitted_turns: 0 }, digest({ through_turn: 0 }))).toBe(false);
    expect(digestPending(ctx, null)).toBe(false);
    expect(digestPending({ ...ctx, first_replayed_turn: null }, digest({ through_turn: 0 }))).toBe(false);
  });

  it("regenerates through the owner route, pinned to the workspace", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ context_digest: digest() }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const out = await regenerateDigest("c/1", "ws-a");
    expect(out.through_turn).toBe(2);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/assistant/architect/conversations/c%2F1/digest/regenerate");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["X-Workspace"]).toBe("ws-a");
  });

  it("surfaces the server's refusal as an ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ code: "assistant.digest_owner_only", message: "only the owner" }), { status: 403 },
    )));
    const err = await regenerateDigest("c1", null).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("assistant.digest_owner_only");
  });

  it("has every label in both languages, the Chinese ones with full-width punctuation", () => {
    const en = digestKeys("en");
    const zh = digestKeys("zh-CN");
    expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort());
    for (const text of Object.values(zh)) expect(text).not.toMatch(/[,;:()?](?!\})/);
    expect(zh.show).toContain("{{n}}");
  });
});
