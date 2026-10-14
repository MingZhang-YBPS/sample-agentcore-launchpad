// Agent management logic shared by the V2 and V3 agent pages (v3-agent-management §2.3):
// data loading and actions live here, each console draws its own cards.
export { useAdminLoad, type AdminLoaded } from "./load";
export { agentPermissions, useAgentPermissions, type AgentPermissions, type EditBlocked } from "./permissions";
export { classicDetailPath, editKind, editPath, identityPath, type EditKind } from "./edit";
export {
  isImported,
  needsTypedName,
  performAgentAction,
  typedNameMatches,
  useAgentActions,
  type AgentActionKind,
} from "./actions";
export { comparePair, showValue, togglePick, useSnapshotDiff, useSnapshots } from "./snapshots";
export { inboundFacts, inboundSwitchState, useInboundSwitch } from "./inbound";
export { useIntegration } from "./integration";
export { releasable, releaseTargets, useRelease } from "./release";
export { deployPhase, useAgentDetail, useDeployProgress } from "./deploy";
