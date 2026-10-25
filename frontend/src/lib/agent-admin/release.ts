import { useState } from "react";

import { api, type ReleaseBundleInfo, type Workspace } from "../api";
import { useAdminLoad } from "./load";

/** Promotion targets: every workspace but this one. */
export function releaseTargets(workspaces: Workspace[], currentWorkspaceId: string | null): Workspace[] {
  return workspaces.filter((row) => row.id !== currentWorkspaceId);
}

/** Nothing to release unless the agent runs, and a system preset is never released from here. */
export function releasable(agent: { status: string; system?: unknown }): boolean {
  return !agent.system && agent.status === "active";
}

/**
 * T20/T21 — bundle the agent's publish, publish it as a shared template, and ask
 * for a promotion. Bundling freezes what was tested (idempotent); a promotion is
 * the request a second person reviews.
 */
export function useRelease(
  input: { agentId: string; agentTitle: string; currentWorkspaceId: string | null },
  callbacks: {
    onBundled: (bundle: ReleaseBundleInfo) => void;
    onPublished: () => void;
    onRequested: (promotionId: string) => void;
    onError?: (err: unknown) => void;
  },
) {
  const { agentId, agentTitle, currentWorkspaceId } = input;
  const [tick, setTick] = useState(0);
  const bundles = useAdminLoad(() => api.agentReleaseBundles(agentId), `bundles:${agentId}:${tick}`);
  const workspaces = useAdminLoad(() => api.listWorkspaces(), "release-workspaces");
  const [asking, setAsking] = useState<ReleaseBundleInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [changeNote, setChangeNote] = useState("");
  const [rollbackNote, setRollbackNote] = useState("");
  const [target, setTarget] = useState("");

  const targets = releaseTargets(workspaces.data?.workspaces ?? [], currentWorkspaceId);

  const guarded = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(err);
      callbacks.onError?.(err);
    } finally {
      setBusy(false);
    }
  };

  const bundle = () =>
    guarded(async () => {
      const created = await api.createReleaseBundle(agentId);
      callbacks.onBundled(created);
      setTick((n) => n + 1);
    });

  // T38: environment ids and secrets are stripped server-side
  const publish = () =>
    guarded(async () => {
      await api.publishTemplate({ agent_id: agentId, title: agentTitle });
      callbacks.onPublished();
    });

  const request = () =>
    guarded(async () => {
      if (!asking) return;
      const created = await api.createPromotion({
        bundle_id: asking.id,
        target_workspace_id: target,
        change_note: changeNote.trim(),
        rollback_note: rollbackNote.trim(),
      });
      setAsking(null);
      callbacks.onRequested(created.id);
    });

  return {
    bundles,
    rows: bundles.data?.bundles ?? [],
    targets,
    busy,
    error,
    bundle,
    publish,
    asking,
    ask: (row: ReleaseBundleInfo) => {
      setError(null);
      setAsking(row);
      setTarget(targets[0]?.id ?? "");
      setChangeNote("");
      setRollbackNote("");
    },
    cancelAsk: () => {
      setAsking(null);
      setError(null);
    },
    target,
    setTarget,
    changeNote,
    setChangeNote,
    rollbackNote,
    setRollbackNote,
    canRequest: !busy && !!changeNote.trim() && !!rollbackNote.trim() && !!target,
    request,
  };
}
