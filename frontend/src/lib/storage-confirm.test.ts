import { describe, expect, it, vi } from "vitest";

import { ApiError } from "./api";
import { storageFlag, storageWritesOf, withStorageConfirm, writesLabel, WRITE_CONFIRM_CODE } from "./storage-confirm";

const refusal = () =>
  new ApiError(WRITE_CONFIRM_CODE, "writes", {
    action: "evaluation.run",
    storage: [{ storage_id: "state", name: "Orders state", mount_path: "/mnt/state" }, { storage_id: "x", name: null, mount_path: "/mnt/x" }],
  });

describe("the shared-state write confirmation (storage-mounts §8.2)", () => {
  it("reads the mounts of the refusal, and nothing from other errors", () => {
    expect(storageWritesOf(refusal())).toEqual([
      { storage_id: "state", name: "Orders state", mount_path: "/mnt/state" },
      { storage_id: "x", name: null, mount_path: "/mnt/x" },
    ]);
    expect(storageWritesOf(new ApiError(WRITE_CONFIRM_CODE, "m", null))).toEqual([]);
    expect(storageWritesOf(new ApiError("storage.blocked", "m", null))).toBeNull();
    expect(storageWritesOf(new Error("x"))).toBeNull();
    expect(writesLabel(storageWritesOf(refusal()) ?? [])).toBe("Orders state (state), x");
  });

  it("sends the request as before, and resends confirmed once the runner agrees", async () => {
    const call = vi.fn(async (confirm: boolean) => {
      if (!confirm) throw refusal();
      return "ok";
    });
    const ask = vi.fn(() => true);
    await expect(withStorageConfirm(call, ask)).resolves.toBe("ok");
    expect(call.mock.calls.map((c) => c[0])).toEqual([false, true]);
    expect(ask).toHaveBeenCalledWith(expect.arrayContaining([expect.objectContaining({ storage_id: "state" })]));
  });

  it("re-raises the refusal when declined, and other errors untouched", async () => {
    const err = refusal();
    const call = vi.fn(async () => {
      throw err;
    });
    await expect(withStorageConfirm(call, () => false)).rejects.toBe(err);
    expect(call).toHaveBeenCalledTimes(1);
    const other = new ApiError("eval.cost_confirm_required", "m", null);
    const ask = vi.fn(() => true);
    await expect(withStorageConfirm(async () => { throw other; }, ask)).rejects.toBe(other);
    expect(ask).not.toHaveBeenCalled();
  });

  it("adds the flag only when confirmed", () => {
    expect(storageFlag(false)).toEqual({});
    expect(storageFlag(true)).toEqual({ confirm_storage: true });
  });
});
