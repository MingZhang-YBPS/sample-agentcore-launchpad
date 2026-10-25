import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { Reasoned } from "./reason";
import { Select } from "./select";

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("V3 Select (a drop-in for a native <select>)", () => {
  it("renders a themed button trigger showing the chosen option, not a native select", () => {
    const out = renderToStaticMarkup(
      <Select className="v3-input" value="b" onChange={() => undefined} aria-label="Pick">
        <option value="a">Alpha</option>
        <option value="b">Beta</option>
      </Select>,
    );
    expect(out).not.toContain("<select");
    expect(out).toMatch(/<button[^>]*class="v3-sel v3-input"/);
    expect(out).toContain('aria-haspopup="listbox"');
    expect(out).toContain('aria-label="Pick"');
    expect(out).toContain('<span class="val">Beta</span>');
  });

  it("reads options through fragments, mapped arrays and optgroups", () => {
    const rows = ["x", "y"];
    const out = renderToStaticMarkup(
      <Select value="y" onChange={() => undefined}>
        <>
          <option value="">All</option>
        </>
        <optgroup label="Group">{rows.map((r) => <option key={r} value={r}>Row {r}</option>)}</optgroup>
      </Select>,
    );
    expect(out).toContain('<span class="val">Row y</span>');
  });

  it("falls back to the first option for a value no option carries, like a native select", () => {
    const out = renderToStaticMarkup(
      <Select value="gone" onChange={() => undefined}>
        <option value="a">Alpha</option>
      </Select>,
    );
    expect(out).toContain('<span class="val">Alpha</span>');
  });

  it("defaults to the v3-select look and keeps a form-readable value when named", () => {
    const out = renderToStaticMarkup(
      <Select value="admin" name="role" onChange={() => undefined}>
        <option value="admin">Admin</option>
      </Select>,
    );
    expect(out).toMatch(/class="v3-sel v3-select"/);
    expect(out).toContain('<input type="hidden" name="role" value="admin"/>');
  });

  it("a disabled Select inside Reasoned is described by its flag", () => {
    const out = renderToStaticMarkup(
      <Reasoned disabledReason="Needs the eval.run permission.">
        <Select disabled value="a" onChange={() => undefined}>
          <option value="a">Alpha</option>
        </Select>
      </Reasoned>,
    );
    expect(out).toMatch(/<button[^>]* disabled=""/);
    const id = /aria-describedby="([^"]+)"/.exec(out)?.[1];
    expect(id).toBeTruthy();
    expect(out).toContain(`id="${id}"`);
    expect(out).toContain("Needs the eval.run permission.");
  });
});
