import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../i18n";
import { scalarText, STRUCTURED_TABLE_MAX_KEYS, structuredView } from "../../lib/structured-answer";
import { attachStructured, type ChatMessage } from "../../v2/pages/chat/messages";
import { StructuredAnswer } from "./StructuredAnswer";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("structuredView", () => {
  it("draws a flat object as a table, in key order", () => {
    const view = structuredView({ city: "Paris", temp: 21.5, sunny: true, note: null });
    expect(view?.kind).toBe("table");
    expect(view?.kind === "table" && view.rows.map((r) => r.key)).toEqual(["city", "temp", "sunny", "note"]);
  });

  it("falls back to JSON for nested values, arrays, empty or very wide objects and scalars", () => {
    expect(structuredView({ a: { b: 1 } })?.kind).toBe("json");
    expect(structuredView({ tags: ["x"] })?.kind).toBe("json");
    expect(structuredView([{ a: 1 }])?.kind).toBe("json");
    expect(structuredView({})?.kind).toBe("json");
    const wide = Object.fromEntries(Array.from({ length: STRUCTURED_TABLE_MAX_KEYS + 1 }, (_, i) => [`k${i}`, i]));
    expect(structuredView(wide)?.kind).toBe("json");
    expect(structuredView("text")?.kind).toBe("json");
    expect(structuredView(undefined)).toBeNull();
  });

  it("keeps the pretty JSON for copy on both views", () => {
    expect(structuredView({ a: 1 })?.json).toBe('{\n  "a": 1\n}');
    expect(scalarText("x")).toBe("x");
    expect(scalarText(false)).toBe("false");
    expect(scalarText(null)).toBe("null");
  });
});

describe("attachStructured", () => {
  it("lands on the turn's answer", () => {
    const m: ChatMessage[] = [{ kind: "user", text: "q" }, { kind: "agent", text: "a" }];
    const next = attachStructured(m, { ok: 1 });
    expect(next).toHaveLength(2);
    expect(next[1].structured).toEqual({ ok: 1 });
    expect(m[1].structured).toBeUndefined();
  });

  it("never reaches back past the user's message: no answer yet, a new one", () => {
    const m: ChatMessage[] = [{ kind: "agent", text: "old" }, { kind: "user", text: "q" }, { kind: "tool", text: "t" }];
    const next = attachStructured(m, [1]);
    expect(next).toHaveLength(4);
    expect(next[3]).toMatchObject({ kind: "agent", text: "", structured: [1] });
    expect(next[0].structured).toBeUndefined();
  });
});

describe("StructuredAnswer", () => {
  it("renders a flat object as a table with its field count", () => {
    const html = renderToStaticMarkup(<StructuredAnswer value={{ city: "Paris", temp: 21 }} />);
    expect(html).toContain('data-view="table"');
    expect(html).toContain("2 fields");
    expect(html).toContain("<th scope=\"row\">city</th>");
    expect(html).toContain('data-type="number"');
  });

  it("renders a nested value as JSON", () => {
    const html = renderToStaticMarkup(<StructuredAnswer value={{ items: [{ a: 1 }] }} />);
    expect(html).toContain('data-view="json"');
    expect(html).toContain("lp-structured-json");
    expect(html).not.toContain("<table");
  });
});
