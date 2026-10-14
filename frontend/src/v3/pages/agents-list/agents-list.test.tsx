import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { useAgentActions } from "../../../lib/agent-admin/actions";
import { agentPermissions } from "../../../lib/agent-admin/permissions";
import type { AgentInfo } from "../../../lib/api";
import { BulkDeleteDialog } from "./BulkDeleteDialog";
import { RowMenu } from "./RowMenu";
import { bulkDeletable, deleteMany, liveSelection, rowEntries, toggleSelection } from "./rows";

const agent = (over: Partial<AgentInfo> = {}): AgentInfo =>
  ({
    id: "a1",
    name: "lab-service",
    display_name: "Lab service",
    method: "zip_runtime",
    status: "active",
    spec: {},
    system: null,
    ...over,
  }) as AgentInfo;

const PRESET = { managed: true as const, key: "assistant", label: "Assistant", skill_version: "1", protected_actions: [] };
const perms = (over: Partial<Parameters<typeof agentPermissions>[0]> = {}) =>
  agentPermissions({ canDeploy: true, canDelete: true, canConvert: true, isAdmin: false, locked: false, lockTitle: undefined, ...over });
const member = perms();
const keys = (a: AgentInfo, p = member) => rowEntries(a, p).map((e) => e.key);

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("row menu entries", () => {
  it("a platform agent: chat, edit, delete; a Harness agent can also convert", () => {
    expect(keys(agent())).toEqual(["chat", "edit", "delete"]);
    expect(keys(agent({ method: "harness" }))).toEqual(["chat", "edit", "convert", "delete"]);
  });

  it("an imported runtime has no editor and is removed, not deleted", () => {
    expect(keys(agent({ method: "discovered_runtime" }))).toEqual(["chat", "remove"]);
  });

  it("an MCP server has no chat", () => {
    expect(keys(agent({ spec: { protocol: "mcp" } }))).toEqual(["edit", "delete"]);
  });

  it("a preset: never deleted or converted, its edit disabled for a non-admin", () => {
    const entries = rowEntries(agent({ system: PRESET, method: "harness" }), member);
    expect(entries.map((e) => e.key)).toEqual(["chat", "edit"]);
    expect(entries[1]).toMatchObject({ disabled: true, why: "presetAdminOnly" });
    expect(rowEntries(agent({ system: PRESET }), perms({ isAdmin: true }))[1]).toMatchObject({ disabled: false, why: null });
  });

  it("without the permissions the entries are left out", () => {
    expect(keys(agent({ method: "harness" }), perms({ canDeploy: false, canDelete: false, canConvert: false }))).toEqual(["chat"]);
  });

  it("the prod lock disables, it does not hide", () => {
    const entries = rowEntries(agent({ method: "harness" }), perms({ locked: true, lockTitle: "prod" }));
    expect(entries.map((e) => e.key)).toEqual(["chat", "edit", "convert", "delete"]);
    expect(entries.filter((e) => e.key !== "chat").every((e) => e.disabled && e.why === "locked")).toBe(true);
  });

  it("a deploy in flight disables edit and conversion, not chat", () => {
    const entries = rowEntries(agent({ method: "harness", status: "deploying" }), member);
    expect(entries.find((e) => e.key === "edit")).toMatchObject({ disabled: true, why: "deploying" });
    expect(entries.some((e) => e.key === "convert")).toBe(false);
  });

  it("draws a closed menu button per row, nothing when no entry applies", () => {
    const actions = {} as ReturnType<typeof useAgentActions>;
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <RowMenu agent={agent()} perms={member} actions={actions} />
      </MemoryRouter>,
    );
    expect(html).toContain('data-testid="v3-agents-row-menu-a1"');
    expect(html).toContain('aria-label="Actions for Lab service"');
    expect(html).not.toContain('role="menu"');
    const none = renderToStaticMarkup(
      <MemoryRouter>
        <RowMenu agent={agent({ spec: { protocol: "mcp" } })} perms={perms({ canDeploy: false, canDelete: false })} actions={actions} />
      </MemoryRouter>,
    );
    expect(none).toBe("");
  });
});

describe("bulk selection", () => {
  it("only deletable, settled agents on an unlocked workspace can be picked", () => {
    expect(bulkDeletable(agent(), member)).toBe(true);
    expect(bulkDeletable(agent({ method: "discovered_runtime" }), member)).toBe(true);
    expect(bulkDeletable(agent({ system: PRESET }), perms({ isAdmin: true }))).toBe(false);
    expect(bulkDeletable(agent({ status: "deploying" }), member)).toBe(false);
    expect(bulkDeletable(agent(), perms({ locked: true }))).toBe(false);
    expect(bulkDeletable(agent(), perms({ canDelete: false }))).toBe(false);
  });

  it("keeps only the selected rows still on screen and still eligible", () => {
    const rows = [agent({ id: "a" }), agent({ id: "b", status: "deploying" }), agent({ id: "c" })];
    expect(liveSelection(new Set(["a", "b", "gone"]), rows, member).map((a) => a.id)).toEqual(["a"]);
  });

  it("toggles one id, or all: on when any is off, else off", () => {
    expect([...toggleSelection(new Set(["a"]), "b")]).toEqual(["a", "b"]);
    expect([...toggleSelection(new Set(["a", "b"]), "a")]).toEqual(["b"]);
    expect([...toggleSelection(new Set(["a"]), ["a", "b"])].sort()).toEqual(["a", "b"]);
    expect([...toggleSelection(new Set(["a", "b", "x"]), ["a", "b"])]).toEqual(["x"]);
  });

  it("deletes one after another and reports the failures without stopping", async () => {
    const order: string[] = [];
    const del = vi.fn(async (id: string) => {
      order.push(id);
      if (id === "b") throw new Error("boom");
    });
    const res = await deleteMany([agent({ id: "a" }), agent({ id: "b" }), agent({ id: "c" })], del);
    expect(order).toEqual(["a", "b", "c"]);
    expect(res.deleted.map((a) => a.id)).toEqual(["a", "c"]);
    expect(res.failed.map((f) => f.agent.id)).toEqual(["b"]);
    expect((res.failed[0].error as Error).message).toBe("boom");
  });

  it("the confirmation lists every name and marks an imported runtime", () => {
    const html = renderToStaticMarkup(
      <BulkDeleteDialog
        agents={[agent({ id: "a", display_name: "Alpha", name: "alpha" }), agent({ id: "b", display_name: "Imported", name: "imp", method: "discovered_runtime" })]}
        onDone={() => {}}
        onCancel={() => {}}
        del={async () => {}}
      />,
    );
    expect(html).toContain("Delete 2 agents?");
    expect(html).toContain("Alpha");
    expect(html).toContain("alpha");
    expect(html).toContain("Imported");
    expect(html.match(/only the platform record is removed/g)).toHaveLength(1);
  });
});
