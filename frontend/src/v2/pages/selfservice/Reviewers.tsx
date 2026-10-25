import { Copy } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage, reviewLinkApi, type ShareLinkCreated, type ShareLinkInfo, shareLinkApi } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Field, FilterSelect, Select, Table, Tag, type TagTone } from "../../ui";
import "./selfservice.css";
import { useAgents } from "./useAgents";

const STATE_TONE: Record<ShareLinkInfo["state"], TagTone> = {
  active: "green",
  expired: "orange",
  revoked: "gray",
  disabled: "gray",
};
const EXPIRY_CHOICES = ["7", "30", "90", "never"] as const;

/**
 * T34 — reviewer links. One link per reviewer (the label says who): an account-free page
 * where a domain expert rates real answers and writes the correct one. The raw link is
 * shown once, exactly like a chat share link; revoke ends it at once.
 */
export function Reviewers() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { agents, label } = useAgents();
  const [picked, setPicked] = useState("");
  const agentId = picked || agents[0]?.id || "";
  const [tick, setTick] = useState(0);
  const links = useLoad(
    () => (agentId ? reviewLinkApi.list(agentId) : Promise.resolve({ links: [] as ShareLinkInfo[] })),
    `review-links:${agentId}:${tick}`,
  );
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState<(typeof EXPIRY_CHOICES)[number]>("30");
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<ShareLinkCreated | null>(null);
  const [revoking, setRevoking] = useState<ShareLinkInfo | null>(null);

  const create = async () => {
    setBusy(true);
    try {
      setCreated(
        await reviewLinkApi.create(agentId, {
          label: name.trim(),
          expires_in_days: expiry === "never" ? null : Number(expiry),
        }),
      );
      setName("");
      setTick((n) => n + 1);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const fullUrl = created ? `${window.location.origin}${created.path}` : "";

  return (
    <>
      <Alert tone="info">{t("selfService.reviewers.explain")}</Alert>
      <Card
        title={t("selfService.reviewers.title")}
        end={
          <FilterSelect
            label={t("selfService.common.agent")}
            value={agentId}
            options={agents.map((a) => ({ value: a.id, label: label(a.id) }))}
            onChange={setPicked}
          />
        }
      >
        <div className="v2-row-actions">
          <input
            className="v2-input"
            maxLength={64}
            value={name}
            placeholder={t("selfService.reviewers.namePlaceholder")}
            onChange={(e) => setName(e.target.value)}
            aria-label={t("selfService.reviewers.name")}
            data-testid="v2-review-name"
          />
          <Select
            value={expiry}
            options={EXPIRY_CHOICES.map((c) => ({
              value: c,
              label: c === "never" ? t("selfService.reviewers.never") : t("selfService.reviewers.inDays", { count: Number(c) }),
            }))}
            onChange={(v) => setExpiry(v as (typeof EXPIRY_CHOICES)[number])}
            ariaLabel={t("selfService.reviewers.expiry")}
          />
          <Button kind="primary" disabled={busy || !agentId} onClick={() => void create()} testId="v2-review-create">
            {t("selfService.reviewers.create")}
          </Button>
        </div>
        {created && (
          <Alert tone="success">
            <Field label={t("selfService.reviewers.linkOnce")} full>
              <div className="v2-row-actions">
                <input className="v2-input" readOnly style={{ flex: 1 }} value={fullUrl} onFocus={(e) => e.target.select()} data-testid="v2-review-url" />
                <Button
                  onClick={() => {
                    void navigator.clipboard?.writeText(fullUrl);
                    toast("success", t("selfService.reviewers.copied"));
                  }}
                >
                  <Copy size={13} aria-hidden="true" /> {t("selfService.reviewers.copy")}
                </Button>
              </div>
            </Field>
          </Alert>
        )}
        <Table<ShareLinkInfo>
          rows={links.data?.links ?? []}
          rowKey={(r) => r.id}
          loading={links.loading}
          error={links.error}
          onRetry={() => setTick((n) => n + 1)}
          empty={t("selfService.reviewers.empty")}
          testId="v2-review-links"
          columns={[
            { key: "l", title: t("selfService.reviewers.name"), render: (r) => r.label || r.prefix },
            { key: "s", title: t("selfService.issues.status"), render: (r) => <Tag tone={STATE_TONE[r.state]}>{t(`selfService.reviewers.state.${r.state}`)}</Tag> },
            { key: "e", title: t("selfService.reviewers.expiry"), className: "nowrap", render: (r) => (r.expires_at ? fmtTime(r.expires_at) : t("selfService.reviewers.never")) },
            { key: "u", title: t("selfService.reviewers.lastUsed"), className: "nowrap", render: (r) => fmtTime(r.last_used_at) },
            {
              key: "a",
              title: t("v2.common.actions"),
              className: "right",
              render: (r) => (
                <Button size="sm" kind="danger" disabled={r.state !== "active"} onClick={() => setRevoking(r)}>
                  {t("selfService.reviewers.revoke")}
                </Button>
              ),
            },
          ]}
        />
      </Card>
      <Confirm
        open={revoking !== null}
        title={t("selfService.reviewers.revokeTitle")}
        body={t("selfService.reviewers.revokeBody")}
        confirmLabel={t("selfService.reviewers.revoke")}
        danger
        onClose={() => setRevoking(null)}
        onConfirm={async () => {
          const target = revoking;
          setRevoking(null);
          if (!target) return;
          try {
            await shareLinkApi.revoke(target.id);
            setTick((n) => n + 1);
          } catch (err) {
            toast("error", errorMessage(err));
          }
        }}
      />
    </>
  );
}
