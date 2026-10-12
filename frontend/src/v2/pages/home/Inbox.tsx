import { ArrowRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, type InboxItem } from "../../../lib/api";
import { useLoad } from "../../hooks";
import { Card, Spin, Tag, type TagTone } from "../../ui";

/**
 * T22 — the administrator's to-do list on the workbench.
 *
 * One place for everything waiting on a human: pending registrations, accounts about to
 * expire, workspaces that were never finished, failed jobs, promotions awaiting review,
 * agents nobody has evaluated. Every row is a link — an inbox whose items cannot be
 * acted on is just a wall of numbers.
 *
 * Renders nothing for a non-administrator (the endpoint is ADMIN) and nothing when the
 * inbox is empty, so a quiet workspace does not carry an empty card.
 */

const TONE: Record<InboxItem["severity"], TagTone> = {
  action: "red",
  warn: "orange",
  info: "gray",
};

export function Inbox({ isAdmin, tick }: { isAdmin: boolean; tick: number }) {
  const { t } = useTranslation();
  const inbox = useLoad(
    () => (isAdmin ? api.inbox() : Promise.resolve(null)),
    `inbox:${isAdmin}:${tick}`,
  );
  if (!isAdmin) return null;
  if (inbox.loading) {
    return (
      <Card title={t("v2.inbox.title")}>
        <Spin />
      </Card>
    );
  }
  // A failed read is not worth an error card on the landing page: the modules it
  // points at surface their own problems.
  const items = inbox.data?.items ?? [];
  if (items.length === 0) return null;

  return (
    <Card
      title={t("v2.inbox.title")}
      sub={t("v2.inbox.sub", { count: inbox.data?.total ?? 0 })}
      testId="v2-inbox"
    >
      {items.map((item) => (
        <div key={item.key} className="v2-inbox-item" data-testid={`v2-inbox-${item.key}`}>
          <Tag tone={TONE[item.severity]}>{t(`v2.inbox.severity.${item.severity}`)}</Tag>
          <span className="n">{item.count}</span>
          <span>{t(`v2.inbox.item.${item.key}`, { count: item.count })}</span>
          {item.sample.length > 0 && <span className="s">{item.sample.join(" · ")}</span>}
          <Link to={item.to} style={{ marginLeft: "auto", whiteSpace: "nowrap" }}>
            {t("v2.inbox.open")}
            <ArrowRight size={11} aria-hidden="true" />
          </Link>
        </div>
      ))}
    </Card>
  );
}
