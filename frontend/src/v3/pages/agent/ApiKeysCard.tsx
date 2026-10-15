import { BarChart3, Copy, KeyRound, Plus } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { type ApiKeyInfo, type ApiKeyUsage, chatApi } from "../../../lib/api";
import { canWriteKeys, keysForAgent, keyState, type KeyState, v1Sample } from "../../../lib/api/invokeAccess";
import { apiKeyMeta, apiKeyOffboardedNote } from "../../../v2/pages/chat/apiKeys";
import { useProject } from "../../../workspace/project-context";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Lamp, Notice, Panel, type Signal, Skeleton, Spark } from "../../ui";
import "./invokeAccess.css";

function without<T>(map: Record<string, T>, id: string): Record<string, T> {
  const next = { ...map };
  delete next[id];
  return next;
}

const STATE_SIGNAL: Record<KeyState, Signal> = { enabled: "ok", disabled: "off", expired: "wait", offboarded: "wait" };

function useKeys() {
  const { current: ws } = useWorkspace();
  const { current: project } = useProject();
  return useLoad(() => chatApi.apiKeys(), `v3-apikeys:${ws?.id ?? ""}:${project?.id ?? ""}`);
}

/**
 * API keys (IA-2): the `/v1` keys that can call this agent — list, create (scoped to
 * this agent), disable / enable, 14-day usage and why an offboarded creator's key
 * stays off. Same `/api/apikeys` and row logic as V2's chat Inspector. A key is shown
 * once, right after it is minted, together with the `/v1` call it unlocks.
 */
export function ApiKeysCard({ agentId, agentName }: { agentId: string; agentName: string }) {
  const { t } = useTranslation();
  const toast = useToast();
  const { isAdmin } = useAuth();
  const { current: project } = useProject();
  const keys = useKeys();
  const rows = keysForAgent(keys.data?.keys ?? [], agentId);
  const writable = canWriteKeys(isAdmin, project);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [minted, setMinted] = useState<ApiKeyInfo | null>(null);
  const [confirm, setConfirm] = useState<ApiKeyInfo | null>(null);
  const [usage, setUsage] = useState<Record<string, ApiKeyUsage | "loading">>({});

  const copy = (text: string) =>
    navigator.clipboard
      .writeText(text)
      .then(() => toast("ok", t("v3.apiKeys.copied")))
      .catch(() => toast("act", t("v3.apiKeys.copyFailed")));

  const create = async () => {
    setBusy("create");
    try {
      const fallback = `${agentName}-${(keys.data?.keys.length ?? 0) + 1}`.slice(0, 64);
      setMinted(await chatApi.createApiKey(name.trim() || fallback, { agent_ids: [agentId] }));
      setName("");
      keys.reload();
    } catch (err) {
      toast("act", t("v3.apiKeys.createFailed"), err);
    } finally {
      setBusy(null);
    }
  };

  const toggle = async (key: ApiKeyInfo) => {
    setBusy(key.id);
    try {
      await chatApi.setApiKeyEnabled(key.id, !key.enabled);
      toast("ok", t(key.enabled ? "v3.apiKeys.disabledOk" : "v3.apiKeys.enabledOk", { name: key.name }));
      keys.reload();
    } catch (err) {
      toast("act", t("v3.apiKeys.toggleFailed"), err);
    } finally {
      setBusy(null);
    }
  };

  const showUsage = async (key: ApiKeyInfo) => {
    if (usage[key.id]) {
      setUsage((prev) => without(prev, key.id));
      return;
    }
    setUsage((prev) => ({ ...prev, [key.id]: "loading" }));
    try {
      const u = await chatApi.apiKeyUsage(key.id, 14);
      setUsage((prev) => ({ ...prev, [key.id]: u }));
    } catch (err) {
      setUsage((prev) => without(prev, key.id));
      toast("act", t("v3.apiKeys.usageFailed"), err);
    }
  };

  return (
    <Panel title={t("v3.apiKeys.title")} end={<span className="mono v3-hint">{rows.length}</span>}>
      <div className="v3-keys" data-testid="v3-api-keys">
        <span className="v3-hint">{t("v3.apiKeys.sub")}</span>
        {writable ? (
          <div className="v3-keys-create">
            <input className="v3-input" value={name} maxLength={64} placeholder={t("v3.apiKeys.namePlaceholder")}
              aria-label={t("v3.apiKeys.name")} onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && busy == null && void create()} />
            <Btn kind="primary" size="sm" disabled={busy != null} onClick={() => void create()}>
              <Plus size={13} /> {t("v3.apiKeys.create")}
            </Btn>
            <span className="v3-hint">{t("v3.apiKeys.scoped")}</span>
          </div>
        ) : (
          <span className="v3-hint">{t("v3.apiKeys.readOnly")}</span>
        )}

        {minted?.key && (
          <Notice s="ok">
            <div className="v3-keys-once" data-testid="v3-api-key-once">
              <strong>{t("v3.apiKeys.once")}</strong>
              <code>{minted.key}</code>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Btn size="sm" onClick={() => void copy(minted.key ?? "")}><Copy size={13} /> {t("v3.apiKeys.copyKey")}</Btn>
                <Btn size="sm" kind="ghost" onClick={() => setMinted(null)}>{t("v3.apiKeys.dismiss")}</Btn>
              </div>
              <span className="v3-hint">{t("v3.apiKeys.sampleTitle")}</span>
              <pre className="v3-pre" data-testid="v3-api-key-sample">{v1Sample(window.location.origin, agentId, minted.key)}</pre>
              <div>
                <Btn size="sm" kind="ghost" onClick={() => void copy(v1Sample(window.location.origin, agentId, minted.key ?? ""))}>
                  <Copy size={13} /> {t("v3.apiKeys.copySample")}
                </Btn>
              </div>
            </div>
          </Notice>
        )}

        {keys.loading && !keys.data && <Skeleton rows={2} />}
        {keys.error && <Notice s="act">{keys.error} <Btn size="sm" kind="ghost" onClick={keys.reload}>{t("v3.apiKeys.retry")}</Btn></Notice>}
        {!keys.loading && !keys.error && rows.length === 0 && <span className="v3-hint">{t("v3.apiKeys.none")}</span>}
        {rows.length > 0 && (
          <ul className="v3-keys-list">
            {rows.map((key) => {
              const state = keyState(key);
              const u = usage[key.id];
              return (
                <li key={key.id} className="v3-keys-row" data-testid="v3-api-key-row">
                  <Lamp s={STATE_SIGNAL[state]} />
                  <div className="who">
                    <b>{key.name} <span className="mono" style={{ fontWeight: 400 }}>{key.prefix}…</span></b>
                    <span>
                      {apiKeyMeta(t, key)}
                      {key.use_count != null && ` · ${t("v3.apiKeys.calls", { count: key.use_count })}`}
                      {key.agent_ids && key.agent_ids.length === 0 && ` · ${t("v3.apiKeys.allAgents")}`}
                    </span>
                  </div>
                  <Chip s={STATE_SIGNAL[state]}>{t(`v3.apiKeys.state.${state}`)}</Chip>
                  <span className="acts">
                    <Btn size="sm" kind="ghost" onClick={() => void showUsage(key)} title={t("v3.apiKeys.usage")}>
                      <BarChart3 size={13} /> {t("v3.apiKeys.usage")}
                    </Btn>
                    {writable && state !== "offboarded" && (
                      <Btn size="sm" kind={key.enabled ? "ghost" : undefined} disabled={busy === key.id}
                        onClick={() => (key.enabled ? setConfirm(key) : void toggle(key))}>
                        {t(key.enabled ? "v3.apiKeys.disable" : "v3.apiKeys.enable")}
                      </Btn>
                    )}
                  </span>
                  {state === "offboarded" && <span className="reason">{apiKeyOffboardedNote(t, key)}</span>}
                  {u === "loading" && <div className="usage"><Skeleton rows={1} /></div>}
                  {u && u !== "loading" && (
                    <div className="usage" data-testid="v3-api-key-usage">
                      <span className="v3-hint">{t("v3.apiKeys.usageSummary", { total: u.total, days: u.days.length })}</span>
                      {u.days.length >= 2 ? <Spark values={u.days.map((d) => d.count)} /> : null}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
      {confirm && (
        <Confirm
          title={t("v3.apiKeys.confirmTitle")}
          confirmLabel={t("v3.apiKeys.disable")}
          cancelLabel={t("v3.apiKeys.cancel")}
          danger
          busy={busy === confirm.id}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            const key = confirm;
            setConfirm(null);
            void toggle(key);
          }}
        >
          {t("v3.apiKeys.confirmBody", { name: confirm.name })}
        </Confirm>
      )}
    </Panel>
  );
}

/** The chat side panel's compact entry: how many keys can call this agent, the
 *  first few with their state, and a jump to the full card on the agent page. */
export function ApiKeysEntry({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const keys = useKeys();
  const rows = keysForAgent(keys.data?.keys ?? [], agentId);
  return (
    <div className="v3-keys-entry" data-testid="v3-chat-api-keys">
      <div className="top">
        <KeyRound size={14} aria-hidden="true" />
        <span className="v3-rail-label">{t("v3.apiKeys.title")}</span>
        <span className="n">{keys.data ? rows.length : "…"}</span>
      </div>
      {rows.length > 0 ? (
        <ul>
          {rows.slice(0, 3).map((key) => (
            <li key={key.id}>
              <Lamp s={STATE_SIGNAL[keyState(key)]} />
              <span>{key.name}</span>
              <span className="mono">{key.prefix}…</span>
            </li>
          ))}
        </ul>
      ) : (
        keys.data && <span className="v3-hint" style={{ fontSize: 12 }}>{t("v3.apiKeys.none")}</span>
      )}
      <Link to={`/v3/agents?id=${encodeURIComponent(agentId)}&tab=access`} className="v3-btn ghost sm">
        {t("v3.apiKeys.manage")}
      </Link>
    </div>
  );
}
