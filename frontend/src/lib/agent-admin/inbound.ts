import { useState } from "react";

import { api, type AgentInfo, type InboundAuth } from "../api";
import { bearerInvokeUrl, inboundCapable, issuerMismatch, jwtConfigFromForm, type JwtFormState } from "../inbound-auth";
import { useAdminLoad } from "./load";

type InboundMode = "iam" | "jwt";

/** What the agent page shows about inbound auth, read off the agent row. */
export function inboundFacts(agent: AgentInfo) {
  const spec = (agent.spec ?? {}) as Record<string, unknown>;
  const protocol = String(spec.protocol ?? "http");
  const pinned = ((spec.inbound_auth as InboundAuth | null | undefined)?.mode as InboundMode | undefined) ?? null;
  return {
    /** an own runtime (not an import) whose method and protocol can take a JWT authorizer */
    shown: !!agent.arn && agent.method !== "discovered_runtime" && inboundCapable(agent.method, protocol),
    mode: (agent.inbound_auth_mode ?? "iam") as InboundMode,
    pinned,
    jwt: agent.inbound_auth_config?.jwt ?? null,
    invokeUrl: bearerInvokeUrl(agent.arn),
  };
}

/**
 * The switch's direction and whether it can start: switching to JWT needs the
 * workspace defaults (the dialog's prefill) loaded, and nothing switches while
 * a deploy runs (the backend would 409). A JWT agent whose issuer is not the
 * workspace pool's gets the reachability warning.
 */
export function inboundSwitchState(input: {
  mode: InboundMode;
  jwtDiscoveryUrl: string | null;
  busy: boolean;
  defaults: { data: { cognito_issuer?: string | null } | null; loading: boolean; error: string | null };
}) {
  const { mode, jwtDiscoveryUrl, busy, defaults } = input;
  const target: InboundMode = mode === "jwt" ? "iam" : "jwt";
  return {
    target,
    mismatch: mode === "jwt" && jwtDiscoveryUrl ? issuerMismatch(jwtDiscoveryUrl, defaults.data?.cognito_issuer) : null,
    defaultsFailed: target === "jwt" && !defaults.data && !defaults.loading && !!defaults.error,
    switchBlocked: busy || (target === "jwt" && !defaults.data),
  };
}

/**
 * The in-place inbound-auth switch (`POST /api/agents/{id}/inbound-auth`, a new
 * version on the SAME runtime). Switching to JWT is prefilled from the workspace
 * default (else the Cognito preset), so it needs the defaults loaded.
 */
export function useInboundSwitch(
  input: { agentId: string; mode: InboundMode; jwtDiscoveryUrl: string | null; busy: boolean },
  callbacks: { onSwitched: (mode: InboundMode) => void; onError?: (err: unknown) => void },
) {
  const defaults = useAdminLoad(() => api.getInboundAuthDefault(), "inbound-default");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  // the deployed authorizer, not the dialog: a foreign issuer refuses Chat and /v1
  const { target, mismatch, defaultsFailed, switchBlocked } = inboundSwitchState({ ...input, defaults });

  const run = async (body: InboundAuth): Promise<boolean> => {
    setSaving(true);
    setError(null);
    try {
      await api.switchInboundAuth(input.agentId, body);
      callbacks.onSwitched(body.mode as InboundMode);
      return true;
    } catch (err) {
      setError(err);
      callbacks.onError?.(err);
      return false;
    } finally {
      setSaving(false);
    }
  };

  return {
    defaults,
    target,
    mismatch,
    defaultsFailed,
    switchBlocked,
    saving,
    error,
    clearError: () => setError(null),
    switchToIam: () => run({ mode: "iam" }),
    switchToJwt: (form: JwtFormState) => run({ mode: "jwt", jwt: jwtConfigFromForm(form) }),
  };
}
