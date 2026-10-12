import { Copy, Link2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage, reviewLinkApi, type ShareLinkCreated, type ShareLinkInfo, shareLinkApi } from "../../../lib/api";
import { useWorkspace } from "../../../workspace/workspace-context";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Empty, Lamp, Notice, Panel, type Signal, Skeleton } from "../../ui";
import { useAgents } from "./common";

const STATE_SIGNAL: Record<ShareLinkInfo["state"], Signal> = { active: "ok", expired: "wait", revoked: "off", disabled: "off" };
const EXPIRY_CHOICES = ["7", "30", "90", "never"] as const;

/**
 * Reviewer links — one per reviewer (the label says who): an account-free page
 * where a domain expert rates real answers and writes the right one. The raw
 * link is shown once, like a chat share link; revoke ends it at once.
 */
export function Reviewers() {
  const { t } = useTranslation();
  const toast = useToast();
  const { current } = useWorkspace();
  const { agents, label } = useAgents(current?.id ?? "");
  const [picked, setPicked] = useState("");
  const agentId = picked || agents[0]?.id || "";
  const [tick, setTick] = useState(0);
  const links = useLoad(
    () => (agentId ? reviewLinkApi.list(agentId) : Promise.resolve({ links: [] as ShareLinkInfo[] })),
    `v3-review-links:${agentId}:${tick}`,
  );
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState<(typeof EXPIRY_CHOICES)[number]>("30");
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<ShareLinkCreated | null>(null);
  const [revoking, setRevoking] = useState<ShareLinkInfo | null>(null);

  const create = async () => {
    setBusy(true);
    try {
      setCreated(await reviewLinkApi.create(agentId, { label: name.trim(), expires_in_days: expiry === "never" ? null : Number(expiry) }));
      setName("");
      setTick((n) => n + 1);
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const fullUrl = created ? `${window.location.origin}${created.path}` : "";
  const rows = links.data?.links ?? [];

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <select className="v3-select v3-iss-select" value={agentId} onChange={(e) => setPicked(e.target.value)} aria-label={t("selfService.common.agent")}>
          {agents.map((a) => <option key={a.id} value={a.id}>{label(a.id)}</option>)}
        </select>
        <span style={{ color: "var(--v3-text-3)", fontSize: 13 }}>{t("selfService.reviewers.explain")}</span>
      </div>

      <Panel title={t("v3.issues.newLink")}>
        <form
          style={{ display: "flex", gap: 8, flexWrap: "wrap" }}
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <input className="v3-input" style={{ flex: "1 1 240px" }} maxLength={64} value={name}
            placeholder={t("selfService.reviewers.namePlaceholder")} aria-label={t("selfService.reviewers.name")} onChange={(e) => setName(e.target.value)} />
          <select className="v3-select" style={{ width: 150 }} value={expiry} aria-label={t("selfService.reviewers.expiry")}
            onChange={(e) => setExpiry(e.target.value as (typeof EXPIRY_CHOICES)[number])}>
            {EXPIRY_CHOICES.map((c) => (
              <option key={c} value={c}>{c === "never" ? t("selfService.reviewers.never") : t("selfService.reviewers.inDays", { count: Number(c) })}</option>
            ))}
          </select>
          <Btn kind="primary" type="submit" disabled={busy || !agentId}><Link2 size={14} /> {t("selfService.reviewers.create")}</Btn>
        </form>
        {created && (
          <div style={{ marginTop: 14 }}>
            <Notice s="ok">
              <div style={{ display: "grid", gap: 8 }}>
                <span>{t("selfService.reviewers.linkOnce")}</span>
                <div style={{ display: "flex", gap: 8 }}>
                  <input className="v3-input mono" readOnly value={fullUrl} onFocus={(e) => e.target.select()} aria-label={t("selfService.reviewers.linkOnce")} />
                  <Btn onClick={() => {
                    void navigator.clipboard?.writeText(fullUrl);
                    toast("ok", t("selfService.reviewers.copied"));
                  }}><Copy size={13} /> {t("selfService.reviewers.copy")}</Btn>
                </div>
              </div>
            </Notice>
          </div>
        )}
      </Panel>

      <Panel flush title={t("selfService.reviewers.title")} end={<span className="mono">{rows.filter((r) => r.state === "active").length} / {rows.length}</span>}>
        {links.loading && !links.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
        ) : links.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{links.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("selfService.reviewers.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("selfService.reviewers.name")}</th>
                <th>{t("v3.issues.state")}</th>
                <th className="num">{t("selfService.reviewers.expiry")}</th>
                <th className="num">{t("selfService.reviewers.lastUsed")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td style={{ width: 30 }}><Lamp s={STATE_SIGNAL[r.state]} live={r.state === "active"} /></td>
                  <td>
                    <div className="v3-name"><div><b>{r.label || r.prefix}</b><small>{r.prefix} · {r.created_by}</small></div></div>
                  </td>
                  <td><Chip s={STATE_SIGNAL[r.state] === "off" ? undefined : STATE_SIGNAL[r.state]}>{t(`selfService.reviewers.state.${r.state}`)}</Chip></td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{r.expires_at ? new Date(r.expires_at).toLocaleDateString() : t("selfService.reviewers.never")}</td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(r.last_used_at)}</td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    <Btn size="sm" kind="danger" disabled={r.state !== "active"} onClick={() => setRevoking(r)}>{t("selfService.reviewers.revoke")}</Btn>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      {revoking && (
        <Confirm
          title={t("selfService.reviewers.revokeTitle")}
          confirmLabel={t("selfService.reviewers.revoke")}
          cancelLabel={t("v3.common.cancel")}
          danger
          onCancel={() => setRevoking(null)}
          onConfirm={async () => {
            const target = revoking;
            setRevoking(null);
            try {
              await shareLinkApi.revoke(target.id);
              setTick((n) => n + 1);
            } catch (err) {
              toast("act", errorMessage(err));
            }
          }}
        >
          {t("selfService.reviewers.revokeBody")}
        </Confirm>
      )}
    </div>
  );
}
