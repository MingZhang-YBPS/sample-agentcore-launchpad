/**
 * The criteria a golden set serves, as the import checks and the templates need them.
 * `known` mirrors the server's `_criteria_keys` (`backend/app/dlc/golden.py`): the
 * latest published version plus a newer draft, since a key about to be published may
 * already have items written for it. The template lists the published table only —
 * that is the one a gate run judges against.
 */
import { api, dlcApi } from "../../../../lib/api";
import type { Criterion, GoldenItem, GoldenSet, GoldenSplit } from "../../../../lib/dlc";

export interface ServedCriteria {
  /** `null`: the set names no criteria table, or it could not be read — ids are not checked here (the server still does). */
  known: Set<string> | null;
  published: Pick<Criterion, "key" | "text">[];
}

export const NO_CRITERIA: ServedCriteria = { known: null, published: [] };

export async function loadServedCriteria(lineageId: string | null): Promise<ServedCriteria> {
  if (!lineageId) return NO_CRITERIA;
  const newest = await dlcApi.getSet(lineageId);
  let published = newest.set.status === "published" ? newest : null;
  if (!published) {
    const version = Math.max(0, ...newest.versions.filter((v) => v.status === "published").map((v) => v.version));
    if (version > 0) published = await dlcApi.getSet(lineageId, version);
  }
  const known = new Set([...newest.criteria, ...(published?.criteria ?? [])].map((c) => c.key));
  return { known, published: (published?.criteria ?? []).map(({ key, text }) => ({ key, text })) };
}

/** The set's active items, each tagged with its split, for "export as a table". The
 *  golden payload carries counts only, so the split rows come from the dataset list. */
export async function loadGoldenItems(goldenSet: GoldenSet): Promise<(GoldenItem & { split: GoldenSplit })[]> {
  const ids = new Map(Object.entries(goldenSet.splits).map(([name, info]) => [info!.id, name as GoldenSplit]));
  const { datasets } = await api.v2Datasets();
  const order: GoldenSplit[] = ["dev", "regression", "holdout"];
  return datasets
    .filter((d) => ids.has(d.id))
    .sort((a, b) => order.indexOf(ids.get(a.id)!) - order.indexOf(ids.get(b.id)!))
    .flatMap((d) =>
      (d.items as unknown as GoldenItem[])
        .filter((item) => !item.metadata?.dlc?.retired)
        .map((item) => ({ ...item, split: ids.get(d.id)! })),
    );
}
