import { describe, expect, it } from "vitest";

import { displayPath, REPO_ROOT, repoPath } from "./paths";

describe("repoPath", () => {
  it("strips the repo root from codex's absolute paths", () => {
    expect(repoPath("/mnt/workspace/project/README.md")).toBe("README.md");
    expect(repoPath("/mnt/workspace/project/src/app/main.py")).toBe("src/app/main.py");
    expect(repoPath("/mnt/workspace/project//src/./main.py")).toBe("src/main.py");
    expect(repoPath("/mnt/workspace/project/src/../main.py")).toBe("main.py");
    expect(repoPath(`${REPO_ROOT}/a b/ü.md`)).toBe("a b/ü.md");
  });

  it("leaves already-relative paths alone (codex's cwd is the repo)", () => {
    expect(repoPath("README.md")).toBe("README.md");
    expect(repoPath("src/main.py")).toBe("src/main.py");
    expect(repoPath("./src/main.py")).toBe("src/main.py");
    expect(repoPath("src/../main.py")).toBe("main.py");
    expect(repoPath(".env")).toBe(".env");
  });

  it("returns null outside the repo", () => {
    expect(repoPath("/tmp/x.txt")).toBeNull();
    expect(repoPath("/mnt/workspace/.codex/config.toml")).toBeNull();
    expect(repoPath("/mnt/workspace/project-other/a.py")).toBeNull();
    expect(repoPath("/mnt/workspace/projectx")).toBeNull();
    expect(repoPath("/mnt/workspace/project/../.launchpad/state.json")).toBeNull();
    expect(repoPath("../outside.py")).toBeNull();
    expect(repoPath("src/../../outside.py")).toBeNull();
    expect(repoPath("~/notes.md")).toBeNull();
    expect(repoPath("/../mnt/workspace/project/a.py")).toBeNull();
  });

  it("returns null for the root itself and for nothing", () => {
    expect(repoPath(REPO_ROOT)).toBeNull();
    expect(repoPath(`${REPO_ROOT}/`)).toBeNull();
    expect(repoPath(".")).toBeNull();
    expect(repoPath("")).toBeNull();
    expect(repoPath(null)).toBeNull();
    expect(repoPath(undefined)).toBeNull();
  });

  it("displays a repo path relative and anything else as reported", () => {
    expect(displayPath("/mnt/workspace/project/main.py")).toBe("main.py");
    expect(displayPath("main.py")).toBe("main.py");
    expect(displayPath("/tmp/x.txt")).toBe("/tmp/x.txt");
  });
});
