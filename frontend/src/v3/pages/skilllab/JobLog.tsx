import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../../../lib/api";
import { JOB_POLL_MS } from "../../../lib/skillLab";

/**
 * Raw CLI log tail of a job. The backend serves the log by byte offset
 * (`{content, next_offset, eof}`), so chunks are appended rather than re-read —
 * a training log runs to tens of MB. Same contract as V2's JobLog.
 */
export function JobLog({ jobId, live }: { jobId: string; live: boolean }) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const offset = useRef(0);
  const box = useRef<HTMLPreElement>(null);
  // reset before the fetch effect runs, so a job switch never appends onto the old log
  useEffect(() => {
    offset.current = 0;
    setText("");
  }, [jobId]);
  useEffect(() => {
    let stale = false;
    const tick = async () => {
      try {
        // the server caps each chunk: one tick may read several to reach the end
        for (let guard = 0; guard < 40; guard += 1) {
          const chunk = await api.skillLabJobLog(jobId, offset.current);
          if (stale) return;
          offset.current = chunk.next_offset;
          if (chunk.content) setText((prev) => prev + chunk.content);
          if (chunk.eof) return;
        }
      } catch {
        /* transient — the next tick retries */
      }
    };
    void tick();
    if (!live) {
      return () => {
        stale = true;
      };
    }
    const timer = window.setInterval(() => void tick(), JOB_POLL_MS);
    return () => {
      stale = true;
      window.clearInterval(timer);
    };
  }, [jobId, live]);
  // follow the tail only while the job runs
  useEffect(() => {
    if (live && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [text, live]);
  return (
    <pre ref={box} className="v3-pre v3-sl-log">
      {text || <span style={{ color: "var(--v3-text-3)" }}>{t("skillLab.eval.log.waiting")}</span>}
    </pre>
  );
}
