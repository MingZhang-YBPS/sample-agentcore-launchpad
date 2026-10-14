import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { isImported, needsTypedName, typedNameMatches, type useAgentActions } from "../../../lib/agent-admin/actions";
import { Confirm, Notice } from "../../ui";

type Actions = ReturnType<typeof useAgentActions>;

/**
 * The delete / convert confirmation. Deleting a platform agent asks for its
 * resource name typed back (§2.1, D-3); removing an imported runtime does not —
 * it only drops the platform record and says so.
 */
export function ActionDialog({ actions }: { actions: Actions }) {
  const { t } = useTranslation();
  const { pending, busy, error } = actions;
  const [typed, setTyped] = useState("");
  if (!pending) return null;
  const { kind, agent } = pending;
  const name = agent.display_name || agent.name;
  const imported = isImported(agent);
  const typing = needsTypedName(kind, agent);
  const ready = !busy && (!typing || typedNameMatches(agent, typed));
  const key = kind === "convert" ? "convert" : imported ? "remove" : "delete";
  const close = () => {
    setTyped("");
    actions.cancel();
  };
  return (
    <Confirm
      title={t(`v3.agent.action.${key}Title`)}
      confirmLabel={kind === "convert" ? t("v3.agent.convert") : imported ? t("v3.agent.remove") : t("v3.agent.delete")}
      cancelLabel={t("v3.agent.action.cancel")}
      danger={kind !== "convert"}
      busy={busy}
      confirmDisabled={!ready}
      onConfirm={() => void actions.confirm()}
      onCancel={close}
    >
      <div style={{ display: "grid", gap: 12 }} data-testid={`v3-agent-${key}-dialog`}>
        <p style={{ margin: 0 }}>{t(`v3.agent.action.${key}Body`, { name })}</p>
        {typing && (
          <label className="v3-field">
            <span>{t("v3.agent.action.typeName", { name: agent.name })}</span>
            <input
              className="v3-input mono"
              value={typed}
              autoFocus
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && ready) void actions.confirm();
              }}
              aria-label={t("v3.agent.action.typeName", { name: agent.name })}
              data-testid="v3-agent-delete-name"
            />
          </label>
        )}
        {error != null && (
          <Notice s="act">
            <ApiErrorDetail error={error} data-testid="v3-agent-action-error" />
          </Notice>
        )}
      </div>
    </Confirm>
  );
}
