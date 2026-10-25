/**
 * The run-time confirmation of storage-mounts §8.2: in a gated workspace, an
 * evaluation replay or a canary of an agent that mounts shared-state storage
 * read-write writes into production storage, so the backend answers 409
 * `storage.write_confirm_required` (with the mounts) until the runner confirms.
 *
 * The console cannot tell beforehand (the check needs the workspace's gating and
 * the storage catalog), so a page sends its request as before and, on that answer,
 * asks once and resends with `confirm_storage: true`. Declining re-raises the 409.
 */
import i18n from "../i18n";
import { ApiError } from "./api";

export const WRITE_CONFIRM_CODE = "storage.write_confirm_required";

export interface StorageWrite {
  storage_id: string;
  name: string | null;
  mount_path: string;
}

/** The mounts a `storage.write_confirm_required` refusal names; null for any other error. */
export function storageWritesOf(err: unknown): StorageWrite[] | null {
  if (!(err instanceof ApiError) || err.code !== WRITE_CONFIRM_CODE) return null;
  const raw = (err.detail as { storage?: unknown } | null | undefined)?.storage;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((m): m is Record<string, unknown> => Boolean(m) && typeof m === "object")
    .map((m) => ({
      storage_id: String(m.storage_id ?? ""),
      name: typeof m.name === "string" ? m.name : null,
      mount_path: String(m.mount_path ?? ""),
    }));
}

/** `name (id)` per mount, comma-separated, for the question. */
export function writesLabel(writes: readonly StorageWrite[]): string {
  return writes
    .map((w) => (w.name && w.name !== w.storage_id ? `${w.name} (${w.storage_id})` : w.storage_id || w.mount_path))
    .join(", ");
}

function askInBrowser(writes: StorageWrite[]): boolean {
  return window.confirm(i18n.t("storage.writeConfirm.question", { storage: writesLabel(writes) }));
}

/**
 * Run `call(false)`; when the backend asks for the storage confirmation and `ask`
 * agrees, run `call(true)` once. `call` adds `confirm_storage: true` to its body
 * only when told to, so a workspace without such storage sends what it always did.
 */
export async function withStorageConfirm<T>(
  call: (confirmStorage: boolean) => Promise<T>,
  ask: (writes: StorageWrite[]) => boolean | Promise<boolean> = askInBrowser,
): Promise<T> {
  try {
    return await call(false);
  } catch (err) {
    const writes = storageWritesOf(err);
    if (writes === null || !(await ask(writes))) throw err;
    return call(true);
  }
}

/** `{confirm_storage: true}` when confirmed, else nothing. */
export const storageFlag = (confirmStorage: boolean): { confirm_storage?: true } =>
  confirmStorage ? { confirm_storage: true } : {};
