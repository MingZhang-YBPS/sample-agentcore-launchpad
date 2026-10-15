import { Check, Unlink } from "lucide-react";
import { type CSSProperties, useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../../../lib/api";
import { type ShareNotice, shareNoticesApi, unreadNoticesFor } from "../../../lib/api/sharedFrom";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip } from "../../ui";

/**
 * The loans that ended on this project (projects §11.2, SHARE-2): a revoked or expired
 * share tells the grantee's owners once, here, until one of them acknowledges it. Each
 * notice is the loan drawn cut — owner, a severed wire, the resource — with the agents
 * that still pin it, since those are what breaks. Shows nothing when there is nothing
 * unread, or when the notices cannot be read (the ledgers below still say it all).
 */
export function ShareNotices({ projectId }: { projectId: string }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [tick, setTick] = useState(0);
  const [acked, setAcked] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const loaded = useLoad(() => shareNoticesApi.list(), `v3-share-notices:${projectId}:${tick}`);
  const rows = unreadNoticesFor(loaded.data?.items ?? [], projectId).filter((n) => !acked.has(n.id));
  if (rows.length === 0) return null;

  const ack = async (notice: ShareNotice) => {
    setBusy(notice.id);
    try {
      await shareNoticesApi.acknowledge(notice.id);
      setAcked((prev) => new Set(prev).add(notice.id));
      setTick((n) => n + 1);
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="v3-shn" aria-label={t("v3.projects.shares.notices.title")} data-testid="share-notices">
      <header className="v3-shn-head">
        <span className="dir">{t("v3.projects.shares.notices.eyebrow")}</span>
        <b>{t("v3.projects.shares.notices.title")}</b>
        <span className="count">{rows.length}</span>
      </header>
      <ul className="v3-shn-list">
        {rows.map((n, i) => (
          <li key={n.id} className="v3-shn-row" data-kind={n.kind} style={{ "--i": Math.min(i, 6) } as CSSProperties}
            data-testid={`share-notice-${n.id}`}>
            <span className="v3-shn-glyph" aria-hidden="true"><Unlink size={15} /></span>
            <div className="v3-shn-body">
              <p className="v3-shn-what">
                {t(`v3.projects.shares.notices.${n.kind}`, {
                  owner: n.owner_project_name, name: n.resource_name,
                  access: t(`v3.projects.shares.access.${n.access}`),
                })}
              </p>
              <div className="v3-shn-cut" aria-hidden="true">
                <span className="end">{n.owner_project_name}</span>
                <span className="wire" />
                <span className="gap" />
                <span className="wire" />
                <span className="end">{n.project_name}</span>
              </div>
              {n.pinned_by.length > 0 && (
                <div className="v3-shn-pins">
                  <span>{t("v3.projects.shares.notices.pinned")}</span>
                  {n.pinned_by.map((a) => <Chip key={a} s="wait">{a}</Chip>)}
                </div>
              )}
              <small className="v3-shn-meta mono">
                {[
                  t(`v3.projects.shares.kind.${n.resource_kind}`, { defaultValue: n.resource_kind }),
                  n.resource_id,
                  n.kind === "revoked" && n.actor ? t("v3.projects.shares.notices.by", { who: n.actor }) : null,
                  ago(n.created_at),
                ].filter(Boolean).join(" · ")}
              </small>
            </div>
            <Btn size="sm" kind="ghost" disabled={busy != null} onClick={() => void ack(n)}>
              <Check size={13} /> {t("v3.projects.shares.notices.ack")}
            </Btn>
          </li>
        ))}
      </ul>
    </section>
  );
}
