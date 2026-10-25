import { useTranslation } from "react-i18next";

import type { AgentVersionsInfo } from "../../../lib/api";
import { ago } from "../../format";
import type { Loaded } from "../../hooks";
import { Chip, Empty, Notice, Panel, Skeleton } from "../../ui";

/** AWS runtime versions and the endpoints serving them. */
export function VersionsCard({ versions }: { versions: Loaded<AgentVersionsInfo> }) {
  const { t } = useTranslation();
  return (
    <Panel title={t("v3.agent.versions")} flush>
      {versions.error ? (
        <div style={{ padding: 20 }}>
          <Notice s="act">
            {versions.error}
            <button type="button" className="v3-btn sm" onClick={versions.reload}>{t("v3.ux.retry")}</button>
          </Notice>
        </div>
      ) : !versions.data ? (
        <div style={{ padding: 20 }}>{versions.loading ? <Skeleton rows={3} /> : <Empty title={t("v3.agent.noVersions")} />}</div>
      ) : (
        <table className="v3-table">
          <thead><tr><th>{t("v3.agents.version")}</th><th>{t("v3.agent.endpoints")}</th><th className="num">{t("v3.agents.updated")}</th></tr></thead>
          <tbody>
            {versions.data.versions.slice(0, 8).map((v) => {
              const serving = versions.data!.endpoints.filter((e) => e.live_version === v.version).map((e) => e.name);
              return (
                <tr key={v.version ?? "?"}>
                  <td className="mono">v{v.version}</td>
                  <td style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {serving.length ? serving.map((n) => <Chip key={n} s={n === "live" || n === "DEFAULT" ? "ok" : "info"}>{n}</Chip>) : <span style={{ color: "var(--v3-text-3)" }}>—</span>}
                  </td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(v.last_updated_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
