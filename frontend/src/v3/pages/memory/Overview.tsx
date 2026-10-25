import { RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { MemoryOverview } from "../../../lib/api";
import { shortId } from "../../../v2/pages/memory/common";
import { ago } from "../../format";
import { Btn, Chip, Empty, Lamp, Notice, Panel, Stat } from "../../ui";
import { memSignal } from "./signal";

const resourceHref = (id: string) => `/v2/memory?view=resource&id=${encodeURIComponent(id)}`;

/** The platform memory resource and the long-term strategies that drive extraction. */
export function Overview({ overview, onReload }: { overview: MemoryOverview; onReload: () => void }) {
  const { t } = useTranslation();
  const mem = overview.memory;
  if (!mem) return null;
  const s = memSignal(mem.status);
  const days = mem.event_expiry_days != null ? t("v3.memory.days", { count: mem.event_expiry_days }) : "—";
  return (
    <>
      <div className="v3-grid c4">
        <Panel>
          <Stat
            label={t("v3.memory.actors")}
            value={overview.actor_count_truncated ? `${overview.actor_count}+` : overview.actor_count}
            foot={overview.actor_count_truncated ? t("v3.memory.actorsTruncated") : t("v3.memory.actorsFoot")}
          />
        </Panel>
        <Panel><Stat label={t("v3.memory.strategies")} value={overview.strategies.length} foot={t("v3.memory.strategiesFoot")} /></Panel>
        <Panel><Stat label={t("v3.memory.expiry")} value={days} foot={t("v3.memory.expiryFoot")} /></Panel>
        <Panel signal={s === "off" ? undefined : s}>
          <Stat label={t("v3.memory.status")} value={mem.status ?? "—"} signal={s === "off" ? undefined : s} foot={mem.failure_reason ?? t("v3.memory.statusFoot")} />
        </Panel>
      </div>
      {mem.failure_reason && <Notice s="act">{mem.failure_reason}</Notice>}

      <Panel
        title={t("v3.memory.resourceTitle")}
        signal={s === "off" ? undefined : s}
        end={
          <span style={{ display: "inline-flex", gap: 8 }}>
            <Btn size="sm" kind="ghost" onClick={onReload}><RefreshCw size={13} /></Btn>
            <Link className="v3-btn sm" to={resourceHref(mem.id)}>{t("v3.memory.detail")}</Link>
          </span>
        }
      >
        <dl className="v3-kv">
          <dt>{t("v3.memory.name")}</dt><dd>{mem.name ?? "—"}</dd>
          <dt>ID</dt><dd className="mono">{mem.id}</dd>
          <dt>ARN</dt><dd className="mono" title={mem.arn ?? ""}>{shortId(mem.arn, 22)}</dd>
          <dt>{t("v3.memory.description")}</dt><dd>{mem.description || "—"}</dd>
          <dt>{t("v3.memory.encryption")}</dt>
          <dd className="mono" title={mem.encryption_key_arn ?? ""}>{mem.encryption_key_arn ? shortId(mem.encryption_key_arn, 16) : t("v3.memory.awsKey")}</dd>
          <dt>{t("v3.memory.role")}</dt><dd className="mono" title={mem.execution_role_arn ?? ""}>{shortId(mem.execution_role_arn, 18)}</dd>
          <dt>{t("v3.memory.updated")}</dt><dd>{ago(mem.updated_at)}</dd>
        </dl>
      </Panel>

      <Panel title={t("v3.memory.strategiesTitle")} flush>
        {overview.strategies.length === 0 ? (
          <Empty title={t("v3.memory.noStrategies")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr><th /><th>{t("v3.memory.strategy")}</th><th>{t("v3.memory.type")}</th><th>{t("v3.memory.namespace")}</th><th className="num">{t("v3.memory.created")}</th></tr>
            </thead>
            <tbody>
              {overview.strategies.map((st) => (
                <tr key={st.strategy_id ?? st.name ?? ""}>
                  <td style={{ width: 30 }}><Lamp s={memSignal(st.status)} /></td>
                  <td>
                    <div className="v3-name"><div><b>{st.name ?? "—"}</b><small>{st.description || st.strategy_id || "—"}</small></div></div>
                  </td>
                  <td><Chip>{st.type ?? "—"}</Chip></td>
                  <td className="mono" style={{ color: "var(--v3-text-2)" }}>
                    {(st.namespace_templates.length ? st.namespace_templates : st.namespaces).map((ns) => <div key={ns}>{ns}</div>)}
                  </td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(st.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel title={t("v3.memory.siblingsTitle")} flush>
        {overview.other_memories.length === 0 ? (
          <Empty title={t("v3.memory.noSiblings")} />
        ) : (
          <table className="v3-table">
            <tbody>
              {overview.other_memories.map((m) => (
                <tr key={m.id ?? m.arn ?? ""} style={m.is_platform ? { background: "var(--v3-ink-3)" } : undefined}>
                  <td style={{ width: 30 }}><Lamp s={memSignal(m.status)} /></td>
                  <td className="mono" title={m.arn ?? ""}>{m.id ?? "—"}</td>
                  <td>{m.is_platform ? <Chip s="info">{t("v3.memory.platform")}</Chip> : <span style={{ color: "var(--v3-text-3)" }}>{t("v3.memory.external")}</span>}</td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(m.created_at)}</td>
                  <td style={{ width: 1 }}>{m.id && <Link className="v3-btn sm ghost" to={resourceHref(m.id)}>{t("v3.memory.open")}</Link>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </>
  );
}
