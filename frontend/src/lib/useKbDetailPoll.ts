/**
 * The knowledge-base detail page's polling and ingestion automation, shared by
 * the V2 (`v2/pages/knowledge/KbDetail.tsx`) and V3 (`v3/pages/Knowledge.tsx`)
 * consoles so the two cannot drift.
 *
 * - Polls `GET /api/knowledge-bases/{id}` every {@link KB_POLL_MS} while anything
 *   is in flight. A failed read (throttling, a proxy hiccup) no longer ends the
 *   loop: it retries with a growing delay, up to {@link KB_POLL_ERROR_RETRIES}
 *   times in a row, and only a "gone" answer stops it for good.
 * - Starts the FIRST ingestion of a data source that is AVAILABLE with no jobs
 *   (the create flow), retrying a refused start on later ticks.
 * - After an upload, follows the backend's `sync` verdict: a sync that was
 *   already running when the files arrived is followed by one more sync once it
 *   finishes, so the new files are indexed without a manual "Sync now".
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import i18n from "../i18n";
import { ApiError, errorMessage, v2KnowledgeApi } from "./api";
import {
  type DataSource,
  jobRunning,
  kbInFlight,
  type KnowledgeBaseDetail,
  type UploadResult,
} from "./knowledgeBases";

export const KB_POLL_MS = 5000;
/** Consecutive failed reads the loop rides out before it waits for a manual refresh. */
export const KB_POLL_ERROR_RETRIES = 6;
/** ~3.5 min at the 5 s poll: how long an ACTIVE KB without a source waits for the
 *  backend completion thread before only the manual repair is offered. */
const SOURCE_WAIT_TICKS = 40;
/** Attempts per data source for an automatic (first or follow-up) sync. */
const AUTO_SYNC_ATTEMPTS = 3;
/** Codes meaning the linked id does not resolve in this workspace. */
export const KB_GONE_CODES = new Set([
  "kb.not_found", "aws.not_found", "aws.validation", "aws.access_denied", "http.404",
]);

export type KbNotify = (tone: "ok" | "warn", text: string) => void;

/** Delay before the next read after `streak` failed reads in a row (0 = healthy). */
export function kbPollDelay(streak: number): number {
  return KB_POLL_MS * Math.min(1 + streak, 4);
}

/** A data source an automatic sync may start on right now. */
function syncable(ds: DataSource): boolean {
  return ds.status.toUpperCase() === "AVAILABLE" && !(ds.ingestion_jobs ?? []).some((j) => jobRunning(j, true));
}

/** A slow create has its data source finished by a backend thread: keep polling for
 *  a bounded window so it appears on its own; past that, only the repair is offered. */
function awaitingBackendSource(d: KnowledgeBaseDetail, ticks: { current: number }): boolean {
  if (String(d.status).toUpperCase() !== "ACTIVE" || d.data_sources.length > 0) {
    ticks.current = 0;
    return false;
  }
  ticks.current += 1;
  return ticks.current <= SOURCE_WAIT_TICKS;
}

/**
 * A console message for an upload the proxy in front of the backend refused or
 * timed out — the generic "request too large" / "gateway timeout" copy says
 * nothing about files. `null` for every other failure (show it as usual).
 */
export function kbUploadFailure(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  if (err.code === "http.413") return i18n.t("knowledge.upload.tooLarge");
  if (err.code === "http.504") return i18n.t("knowledge.upload.timedOut");
  return null;
}

export function useKbDetailPoll(id: string, opts: { onGone: () => void; notify: KbNotify }) {
  const { t } = useTranslation();
  const [kb, setKb] = useState<KnowledgeBaseDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  const optsRef = useRef(opts);
  optsRef.current = opts;
  const autoSyncTries = useRef<Map<string, number>>(new Map());
  const resyncAfter = useRef<Set<string>>(new Set());
  const sourceWaitTicks = useRef(0);
  const errorStreak = useRef(0);

  const load = useCallback(async (): Promise<KnowledgeBaseDetail | "gone" | null> => {
    try {
      const fresh = await v2KnowledgeApi.get(id);
      setKb(fresh);
      setError(null);
      errorStreak.current = 0;
      return fresh;
    } catch (err) {
      setError(err);
      if (err instanceof ApiError && KB_GONE_CODES.has(err.code)) {
        optsRef.current.onGone();
        return "gone";
      }
      errorStreak.current += 1;
      return null;
    }
  }, [id]);

  /** One automatic sync attempt; true while the source still needs one. */
  const autoSync = useCallback(
    async (ds: DataSource, okText: string): Promise<boolean> => {
      const tries = autoSyncTries.current.get(ds.ds_id) ?? 0;
      if (tries >= AUTO_SYNC_ATTEMPTS) return false;
      autoSyncTries.current.set(ds.ds_id, tries + 1);
      try {
        await v2KnowledgeApi.sync(id, ds.ds_id);
        autoSyncTries.current.set(ds.ds_id, AUTO_SYNC_ATTEMPTS); // done
        optsRef.current.notify("ok", okText);
        return false;
      } catch (err) {
        if (tries + 1 >= AUTO_SYNC_ATTEMPTS) {
          optsRef.current.notify("warn", t("knowledge.upload.autoSyncFailed", { msg: errorMessage(err) }));
          return false;
        }
        return true; // refused (still provisioning, a racing sync): try again next tick
      }
    },
    [id, t],
  );

  const automate = useCallback(
    async (d: KnowledgeBaseDetail): Promise<boolean> => {
      let pending = false;
      for (const ds of d.data_sources) {
        if (resyncAfter.current.has(ds.ds_id)) {
          pending = true;
          if (syncable(ds)) {
            resyncAfter.current.delete(ds.ds_id);
            autoSyncTries.current.delete(ds.ds_id);
            if (await autoSync(ds, t("knowledge.upload.resyncStarted"))) {
              resyncAfter.current.add(ds.ds_id);
            }
          }
        } else if (syncable(ds) && (ds.ingestion_jobs ?? []).length === 0) {
          if (await autoSync(ds, t("knowledge.detail.sources.syncStarted"))) pending = true;
        }
      }
      return pending;
    },
    [autoSync, t],
  );

  // Poll while anything is in flight; stop once quiescent. A mutation bumps
  // refreshKey, which restarts the loop.
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      const d = await load();
      if (cancelled || d === "gone") return;
      if (d === null) {
        if (errorStreak.current <= KB_POLL_ERROR_RETRIES) {
          timer = window.setTimeout(() => void tick(), kbPollDelay(errorStreak.current));
        }
        return;
      }
      const waiting = awaitingBackendSource(d, sourceWaitTicks);
      const automating = await automate(d);
      if (cancelled) return;
      if (kbInFlight(d) || waiting || automating) timer = window.setTimeout(() => void tick(), KB_POLL_MS);
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [load, refreshKey, automate]);

  /** Tell the user what happened to indexing after an upload, and follow up. */
  const afterUpload = useCallback(
    (result: UploadResult, count: number) => {
      const sync = result.sync;
      const notify = optsRef.current.notify;
      switch (sync?.status) {
        case "started":
          notify("ok", t("knowledge.upload.indexing", { n: count }));
          break;
        case "running":
          if (sync.ds_id) resyncAfter.current.add(sync.ds_id);
          notify("ok", t("knowledge.upload.queued", { n: count }));
          break;
        case "not_ready":
        case "pending_source":
          notify("ok", t("knowledge.upload.pending", { n: count }));
          break;
        case "failed":
          notify("warn", t("knowledge.upload.syncFailed", { n: count, msg: sync.message ?? "" }));
          break;
        default: // an older backend that does not sync on upload
          notify("ok", t("knowledge.detail.sources.uploadedFiles", { n: count }));
      }
      refresh();
    },
    [refresh, t],
  );

  const resetSourceWait = useCallback(() => {
    sourceWaitTicks.current = 0;
  }, []);

  return { kb, setKb, error, refresh, afterUpload, resetSourceWait };
}
