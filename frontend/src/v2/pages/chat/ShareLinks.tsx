import { Copy } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type ChannelPlatform,
  errorMessage,
  shareLinkApi,
  type ShareLinkCreated,
  type ShareLinkInfo,
} from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Confirm, Field, LinkButton, Modal, Select, Table, Tag, type TagTone } from "../../ui";

const STATE_TONE: Record<ShareLinkInfo["state"], TagTone> = {
  active: "green",
  expired: "orange",
  revoked: "gray",
  disabled: "gray",
};

/** "never" is an explicit choice: an account-free link should expire by default. */
const EXPIRY_CHOICES = ["7", "30", "90", "never"] as const;

type Target = "chat" | ChannelPlatform;

/** The credential fields each chat app needs (T30): key sent to the API + i18n label key. */
const CREDENTIAL_FIELDS: Record<ChannelPlatform, { key: string; labelKey: string }[]> = {
  slack: [
    { key: "signing_secret", labelKey: "shareLinks.slackSigningSecret" },
    { key: "bot_token", labelKey: "shareLinks.slackBotToken" },
  ],
  feishu: [
    { key: "verification_token", labelKey: "shareLinks.feishuVerificationToken" },
    { key: "app_id", labelKey: "shareLinks.feishuAppId" },
    { key: "app_secret", labelKey: "shareLinks.feishuAppSecret" },
  ],
};

/**
 * Share links for one agent (T14): mint an account-free chat link, copy it (the
 * raw token is only ever shown right after creation), and revoke. The link-level
 * controls are label, expiry and revoke; there is no per-user ACL on a link.
 */
export function ShareLinksModal({
  agentId,
  agentName,
  onClose,
}: {
  agentId: string;
  agentName: string;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const links = useLoad(() => shareLinkApi.list(agentId), `share-links:${agentId}`);
  const [label, setLabel] = useState("");
  const [expiry, setExpiry] = useState<(typeof EXPIRY_CHOICES)[number]>("7");
  const [target, setTarget] = useState<Target>("chat");
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const [domain, setDomain] = useState<"feishu" | "lark">("feishu");
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<ShareLinkCreated | null>(null);
  const [revoking, setRevoking] = useState<ShareLinkInfo | null>(null);

  const fullUrl = (link: ShareLinkCreated) => `${window.location.origin}${link.path}`;

  const create = async () => {
    setBusy(true);
    try {
      const common = {
        label: label.trim(),
        expires_in_days: expiry === "never" ? null : Number(expiry),
      };
      const result =
        target === "chat"
          ? await shareLinkApi.create(agentId, common)
          : await shareLinkApi.createChannel(agentId, {
              ...common,
              platform: target,
              credentials: target === "feishu" ? { ...credentials, domain } : credentials,
            });
      setCreated(result);
      setLabel("");
      setCredentials({}); // the secrets leave the page as soon as they are sent
      links.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast("success", t("shareLinks.copied"));
    } catch {
      toast("error", t("shareLinks.copyFailed"));
    }
  };

  const revoke = async (link: ShareLinkInfo) => {
    setBusy(true);
    try {
      await shareLinkApi.revoke(link.id);
      toast("success", t("shareLinks.revoked"));
      links.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
      setRevoking(null);
    }
  };

  return (
    <>
      <Modal
        open
        wide
        title={t("shareLinks.title", { name: agentName })}
        onClose={onClose}
        testId="v2-share-links"
        footer={<Button onClick={onClose}>{t("v2.common.close")}</Button>}
      >
        <div className="v2-stack">
          <Alert tone="info">{t("shareLinks.explain")}</Alert>
          <div style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap" }}>
            <Field label={t("shareLinks.platform")}>
              <Select
                value={target}
                options={(["chat", "slack", "feishu"] as const).map((choice) => ({
                  value: choice,
                  label: t(`shareLinks.platforms.${choice}`),
                }))}
                onChange={(v) => {
                  setTarget(v as Target);
                  setCredentials({});
                }}
                testId="share-target"
              />
            </Field>
            <Field label={t("shareLinks.label")}>
              <input
                className="v2-input"
                value={label}
                maxLength={64}
                onChange={(e) => setLabel(e.target.value)}
                placeholder={t("shareLinks.labelPlaceholder")}
                data-testid="share-label"
              />
            </Field>
            <Field label={t("shareLinks.expiry")}>
              <Select
                value={expiry}
                options={EXPIRY_CHOICES.map((choice) => ({
                  value: choice,
                  label: choice === "never" ? t("shareLinks.never") : t("shareLinks.days", { count: Number(choice) }),
                }))}
                onChange={(v) => setExpiry(v as (typeof EXPIRY_CHOICES)[number])}
                testId="share-expiry"
              />
            </Field>
            <Button kind="primary" disabled={busy} onClick={() => void create()} testId="share-create">
              {target === "chat" ? t("shareLinks.create") : t("shareLinks.connect")}
            </Button>
          </div>
          {target !== "chat" && (
            <>
              <Alert tone="info">{t("shareLinks.channelsHelp")}</Alert>
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                {CREDENTIAL_FIELDS[target].map((field) => (
                  <Field key={field.key} label={t(field.labelKey)}>
                    <input
                      className="v2-input"
                      type={field.key === "app_id" ? "text" : "password"}
                      autoComplete="off"
                      value={credentials[field.key] ?? ""}
                      maxLength={512}
                      onChange={(e) => setCredentials({ ...credentials, [field.key]: e.target.value })}
                      data-testid={`share-cred-${field.key}`}
                    />
                  </Field>
                ))}
                {target === "feishu" && (
                  <Field label={t("shareLinks.feishuDomain")}>
                    <Select
                      value={domain}
                      options={(["feishu", "lark"] as const).map((choice) => ({
                        value: choice,
                        label: t(`shareLinks.feishuDomains.${choice}`),
                      }))}
                      onChange={(v) => setDomain(v as "feishu" | "lark")}
                    />
                  </Field>
                )}
              </div>
            </>
          )}
          {created && (
            <Alert tone="success">
              <div>
                {created.channel ? t("shareLinks.webhookCreatedOnce") : t("shareLinks.createdOnce")}
              </div>
              <div className="v2-row" style={{ marginTop: 6 }}>
                <code className="mono" style={{ wordBreak: "break-all" }} data-testid="share-url">
                  {fullUrl(created)}
                </code>
                <Button size="sm" onClick={() => void copy(fullUrl(created))}>
                  <Copy size={12} aria-hidden="true" />
                  {t("shareLinks.copy")}
                </Button>
              </div>
              {created.embed_snippet && (
                <div style={{ marginTop: 10 }}>
                  <strong>{t("shareLinks.embedTitle")}</strong>
                  <div className="v2-muted">{t("shareLinks.embedHelp")}</div>
                  <div className="v2-row" style={{ marginTop: 6 }}>
                    <code className="mono" style={{ wordBreak: "break-all" }} data-testid="share-embed">
                      {created.embed_snippet}
                    </code>
                    <Button size="sm" onClick={() => void copy(created.embed_snippet ?? "")}>
                      <Copy size={12} aria-hidden="true" />
                      {t("shareLinks.copyEmbed")}
                    </Button>
                  </div>
                </div>
              )}
            </Alert>
          )}
          <Table
            rows={(links.data?.links ?? []).filter((link) => link.kind !== "review")}
            rowKey={(l) => l.id}
            loading={links.loading}
            error={links.error}
            onRetry={links.reload}
            empty={t("shareLinks.empty")}
            testId="share-links-table"
            columns={[
              {
                key: "label",
                title: t("shareLinks.label"),
                render: (l) => (
                  <span>
                    {l.label || "—"} <span className="mono v2-muted">{l.prefix}</span>
                  </span>
                ),
              },
              {
                key: "channel",
                title: t("shareLinks.channel"),
                render: (l) => (
                  <>
                    {t(`shareLinks.platforms.${l.channel?.platform ?? "chat"}`)}
                    {l.channel && l.channel.secrets_set.length > 0 && (
                      <span className="sub">{t("shareLinks.secretsSet")}</span>
                    )}
                  </>
                ),
              },
              {
                key: "state",
                title: t("shareLinks.state"),
                render: (l) => <Tag tone={STATE_TONE[l.state]}>{t(`shareLinks.states.${l.state}`)}</Tag>,
              },
              { key: "expires", title: t("shareLinks.expires"), render: (l) => (l.expires_at ? fmtTime(l.expires_at) : t("shareLinks.never")) },
              { key: "uses", title: t("shareLinks.uses"), className: "num", render: (l) => l.use_count },
              { key: "last", title: t("shareLinks.lastUsed"), render: (l) => (l.last_used_at ? fmtTime(l.last_used_at) : "—") },
              {
                key: "ops",
                title: t("v2.common.actions"),
                className: "right",
                render: (l) =>
                  l.state === "active" ? (
                    <LinkButton danger onClick={() => setRevoking(l)}>
                      {t("shareLinks.revoke")}
                    </LinkButton>
                  ) : null,
              },
            ]}
          />
        </div>
      </Modal>
      <Confirm
        open={revoking !== null}
        title={t("shareLinks.revokeTitle")}
        body={t("shareLinks.revokeBody")}
        confirmLabel={t("shareLinks.revoke")}
        danger
        busy={busy}
        onConfirm={() => revoking && void revoke(revoking)}
        onClose={() => setRevoking(null)}
      />
    </>
  );
}
