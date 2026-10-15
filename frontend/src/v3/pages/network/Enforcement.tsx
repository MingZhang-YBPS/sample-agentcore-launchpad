import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../components/ErrorCallout";
import { networkApi, type NetworkSettings } from "../../../lib/api/networkProfiles";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { Notice, Panel, Skeleton } from "../../ui";

/**
 * The workspace's network switches and the condition-key enforcement (§12.5): whether
 * agents may run PUBLIC, whether the workspace means to enforce, and the approved sets
 * as the spoke stack parameters / an SCP policy the operator applies — the platform
 * does not write the policy onto its own role.
 */
export function Enforcement({ version }: { version: number }) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const data = useLoad(() => networkApi.enforcement(), `v3-network-enforcement:${version}`);
  const [busy, setBusy] = useState(false);

  const flip = async (key: keyof NetworkSettings, value: boolean) => {
    setBusy(true);
    try {
      failed.clear();
      await networkApi.putSettings({ [key]: value });
      toast("ok", t("v3.network.enforce.saved"));
      data.reload();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title={t("v3.network.enforce.title")}>
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-network-enforce-error" />
      {data.loading && !data.data ? <Skeleton rows={3} /> : data.error || !data.data ? <Notice s="act">{data.error}</Notice> : (
        <div style={{ display: "grid", gap: 12 }} data-testid="v3-network-enforcement">
          <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input type="checkbox" disabled={busy} checked={data.data.settings.network_public_allowed}
              onChange={(e) => void flip("network_public_allowed", e.target.checked)} />
            <span>{t("v3.network.enforce.publicAllowed")}</span>
          </label>
          <small className="v3-hint" style={{ marginTop: -8 }}>{t("v3.network.enforce.publicHint")}</small>
          <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input type="checkbox" disabled={busy} checked={data.data.settings.network_enforcement}
              onChange={(e) => void flip("network_enforcement", e.target.checked)} />
            <span>{t("v3.network.enforce.enforce")}</span>
          </label>
          <small className="v3-hint" style={{ marginTop: -8 }}>{t("v3.network.enforce.enforceHint")}</small>
          <dl className="v3-kv">
            <dt>ApprovedSubnetIds</dt><dd className="mono">{data.data.stack_parameters.ApprovedSubnetIds || "—"}</dd>
            <dt>ApprovedSecurityGroupIds</dt><dd className="mono">{data.data.stack_parameters.ApprovedSecurityGroupIds || "—"}</dd>
            <dt>{t("v3.network.enforce.actions")}</dt><dd className="mono">{data.data.actions.join(", ")}</dd>
          </dl>
          {data.data.not_enforceable.map((line) => <Notice key={line} s="info">{line}</Notice>)}
          <details>
            <summary>{t("v3.network.enforce.policy")}</summary>
            <pre className="mono" style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{JSON.stringify(data.data.policy, null, 2)}</pre>
          </details>
        </div>
      )}
    </Panel>
  );
}
