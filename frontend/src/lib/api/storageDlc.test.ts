import { describe, expect, it } from "vitest";

import { type StorageChange, changeDelta, hasStorage, mountLabel, proposalStorage, releaseStorage } from "./storageDlc";

const change = (over: Partial<StorageChange>): StorageChange => ({
  change: "added", requires_ack: false, mount_path: "/mnt/state", storage_id: "state", name: null,
  mode: "rw", data_class: null, from_storage_id: null, from_mode: null, from_data_class: null, ...over,
});

describe("release storage evidence", () => {
  it("reads the evidence of a record or report, and nothing else", () => {
    const ev = { candidate: [], live: [], live_known: true, changes: [], requires_ack: false, shared_state_writes: [] };
    expect(releaseStorage({ storage: ev })).toBe(ev);
    expect(releaseStorage({ storage: null })).toBeNull();
    expect(releaseStorage({ storage: { candidate: [] } })).toBeNull();
    expect(releaseStorage(null)).toBeNull();
  });

  it("shows a panel only when storage is involved", () => {
    const base = { live: [], live_known: true, requires_ack: false, shared_state_writes: [] };
    expect(hasStorage({ ...base, candidate: [], changes: [] })).toBe(false);
    expect(hasStorage({ ...base, candidate: [], changes: [change({ change: "removed" })] })).toBe(true);
    expect(hasStorage(null)).toBe(false);
  });

  it("says what changed", () => {
    expect(changeDelta(change({ change: "mode", from_mode: "ro", mode: "rw" }))).toBe("ro → rw");
    expect(changeDelta(change({ change: "storage", from_storage_id: "a", storage_id: "b" }))).toBe("a → b");
    expect(changeDelta(change({ change: "data_class", from_data_class: "reference", data_class: "shared_state" })))
      .toBe("reference → shared_state");
    expect(changeDelta(change({ change: "added" }))).toBe("");
  });

  it("labels catalog mounts by id and inline ones by path", () => {
    expect(mountLabel({ storage_id: "state", name: "Pipeline state", mount_path: "/mnt/state" })).toBe("Pipeline state (state)");
    expect(mountLabel({ storage_id: "state", name: "state", mount_path: "/mnt/state" })).toBe("state");
    expect(mountLabel({ storage_id: null, mount_path: "/mnt/legacy" })).toBe("/mnt/legacy");
  });
});

describe("proposal storage", () => {
  it("defaults to read-only and drops malformed entries", () => {
    expect(proposalStorage({ storage: [{ storage_id: "a", mode: "rw" }, { storage_id: "b" }, null, { mode: "rw" }] }))
      .toEqual([{ storage_id: "a", mode: "rw" }, { storage_id: "b", mode: "ro" }]);
    expect(proposalStorage({})).toEqual([]);
  });
});
