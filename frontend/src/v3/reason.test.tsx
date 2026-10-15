import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { Reasoned } from "./reason";
import { Btn, Confirm } from "./ui";

const html = (node: React.ReactNode) => renderToStaticMarkup(<MemoryRouter>{node}</MemoryRouter>);
const describedBy = (out: string) => /aria-describedby="([^"]+)"/.exec(out)?.[1];

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("Btn disabledReason (guided-console §4)", () => {
  it("a disabled button with a reason stays focusable and is described by its flag", () => {
    const out = html(<Btn disabled disabledReason="Choose an agent first." onClick={() => undefined}>Go</Btn>);
    expect(out).toContain('aria-disabled="true"');
    expect(out).not.toMatch(/<button[^>]* disabled=""/);
    const id = describedBy(out);
    expect(id).toBeTruthy();
    expect(out).toContain(`id="${id}"`);
    expect(out).toContain('role="tooltip"');
    expect(out).toContain("Choose an agent first.");
    expect(out).toContain("Not yet");
  });

  it("a reason with a step links there through the action table", () => {
    const out = html(<Btn disabled disabledReason={{ text: "Complete first: Create the agent", next: { action: "agent.create", params: {} } }}>Go</Btn>);
    expect(out).toContain('href="/v3/create"');
    expect(out).toContain('data-action="agent.create"');
  });

  it("an enabled button, or one only busy, shows no reason", () => {
    expect(html(<Btn disabledReason="why">Go</Btn>)).not.toContain("v3-why");
    const busy = html(<Btn busy disabled disabledReason="why">Go</Btn>);
    expect(busy).not.toContain("v3-why");
    expect(busy).toMatch(/<button[^>]* disabled=""/);
    expect(busy).toContain('aria-busy="true"');
  });

  it("a disabled button without a reason renders as before", () => {
    const out = html(<Btn disabled kind="primary" size="sm">Go</Btn>);
    expect(out).toBe('<button type="button" class="v3-btn primary sm" disabled="">Go</button>');
  });

  it("Confirm's held button carries its confirmReason", () => {
    const out = html(
      <Confirm title="Delete" confirmLabel="Delete" cancelLabel="Cancel" confirmDisabled confirmReason="Type the name to confirm first."
        onConfirm={() => undefined} onCancel={() => undefined}>body</Confirm>,
    );
    expect(out).toContain("Type the name to confirm first.");
  });
});

describe("Reasoned (native controls)", () => {
  it("a disabled field keeps its native lock and gets the description", () => {
    const out = html(<Reasoned disabledReason="Needs the eval.run permission."><select disabled aria-describedby="hint" /></Reasoned>);
    expect(out).toMatch(/<select[^>]* disabled=""/);
    const ids = describedBy(out)?.split(" ") ?? [];
    expect(ids[0]).toBe("hint");
    expect(out).toContain(`id="${ids[1]}"`);
  });

  it("a native button turns into aria-disabled so it stays reachable", () => {
    const out = html(<Reasoned disabledReason="why"><button type="button" disabled>Go</button></Reasoned>);
    expect(out).toContain('aria-disabled="true"');
    expect(out).not.toMatch(/<button[^>]* disabled=""/);
  });

  it("an enabled control, or no reason, renders the child as it is", () => {
    expect(html(<Reasoned disabledReason="why"><input /></Reasoned>)).toBe("<input/>");
    expect(html(<Reasoned disabledReason={null}><input disabled /></Reasoned>)).toBe('<input disabled=""/>');
  });
});
