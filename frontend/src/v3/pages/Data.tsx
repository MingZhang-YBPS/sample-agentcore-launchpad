import "./data.css";

import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { Filters, PageHead } from "../ui";
import { DatasetDetail, DatasetsTab } from "./data/Datasets";
import { PipelinesTab } from "./data/Pipelines";
import { DataTrace, TracesTab } from "./data/Traces";

type Tab = "traces" | "datasets" | "pipelines";
const TABS: Tab[] = ["traces", "datasets", "pipelines"];

/**
 * Data center — observed trajectories, evaluation datasets and the pipelines
 * that turn the first into the second. Same URL vocabulary as V2 (`?tab=`,
 * `?range=`, `?view=trace|dataset&id=`); the dataset and pipeline editors are
 * V2's, hosted (`view=dataset-new|dataset-edit|pipeline|pipeline-new`).
 */
export function V3Data() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: Tab = (TABS as string[]).includes(raw ?? "") ? (raw as Tab) : "traces";
  const view = params.get("view");
  const id = params.get("id");

  if (view === "trace" && id) return <DataTrace key={id} traceId={id} />;
  if (view === "dataset" && id) return <DatasetDetail key={id} id={id} />;

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.data.eyebrow")}
        title={t("v3.data.title")}
        sub={t("v3.data.sub")}
        end={
          <Filters
            value={tab}
            onChange={(next) => setParams({ tab: next })}
            options={TABS.map((value) => ({ value, label: t(`v2.data.tab.${value}`) }))}
          />
        }
      />
      {tab === "traces" && <TracesTab />}
      {tab === "datasets" && <DatasetsTab />}
      {tab === "pipelines" && <PipelinesTab />}
    </div>
  );
}
