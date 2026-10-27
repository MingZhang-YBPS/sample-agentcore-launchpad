import { useTranslation } from "react-i18next";

import { type IngestionJob, type ResourceState, jobState, resourceState } from "../../../lib/knowledgeBases";
import { Tag, type TagTone } from "../../ui";
import { KB_STATUS } from "./status";

/** KB lifecycle status → tag (translated); anything else renders raw. */
export function KbStatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  const meta = KB_STATUS[status];
  return (
    <Tag tone={meta?.tone ?? "gray"} dot>
      {meta ? t(meta.labelKey) : status}
    </Tag>
  );
}

const STATE_TONE: Record<ResourceState, TagTone> = {
  good: "green",
  crit: "red",
  warn: "blue",
  muted: "gray",
};

/** Data-source / ingestion-job / document status (raw AWS enum) → tag. */
export function ResourceTag({ status, title }: { status: string; title?: string }) {
  return (
    <Tag tone={STATE_TONE[resourceState(status)]} dot title={title}>
      {status}
    </Tag>
  );
}


/** Ingestion-job status → tag; a COMPLETE job that left documents failed is orange. */
export function JobTag({ job }: { job: IngestionJob }) {
  const partial = resourceState(job.status) === "good" && jobState(job) === "warn";
  return (
    <Tag tone={partial ? "orange" : STATE_TONE[jobState(job)]} dot title={job.failure_reasons?.join("; ") || undefined}>
      {job.status}
    </Tag>
  );
}
