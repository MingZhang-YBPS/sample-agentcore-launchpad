import { describe, expect, it } from "vitest";

import { hideableGroups, hostedGroups, withoutHidden } from "./nav";

const tos = (groups: ReturnType<typeof hostedGroups>) => groups.flatMap((g) => g.items.map((i) => i.railKey));

describe("withoutHidden", () => {
  it("drops a hidden entry for a member by its rail key, even when the rail opens its V3 twin", () => {
    const member = hostedGroups(false);
    // /v2/skill-lab is listed at /v3/skill-lab: the key is the rail path, not the twin
    const skill = member.flatMap((g) => g.items).find((i) => i.railKey === "/v2/skill-lab");
    expect(skill?.to).toBe("/v3/skill-lab");
    const filtered = withoutHidden(member, new Set(["/v2/skill-lab"]), false);
    expect(tos(filtered)).not.toContain("/v2/skill-lab");
    expect(tos(filtered).length).toBe(tos(member).length - 1);
  });

  it("keeps every entry for an administrator", () => {
    const admin = hostedGroups(true);
    expect(withoutHidden(admin, new Set(["/v2/skill-lab"]), true)).toEqual(admin);
  });

  it("drops a group whose every entry is hidden", () => {
    const member = hostedGroups(false);
    const group = member[member.length - 1];
    const filtered = withoutHidden(member, new Set(group.items.map((i) => i.railKey)), false);
    expect(filtered.map((g) => g.key)).not.toContain(group.key);
  });

  it("ignores keys that name no entry", () => {
    const member = hostedGroups(false);
    expect(withoutHidden(member, new Set(["/v2/nope"]), false)).toEqual(member);
  });
});

describe("hideableGroups", () => {
  it("offers only entries a member could see, and not the settings page itself", () => {
    const keys = tos(hideableGroups());
    expect(keys).toContain("/v2/skill-lab");
    expect(keys).not.toContain("/v3/menu-settings");
    expect(keys).not.toContain("/v3/models");
  });
});
