/**
 * An agent's network (projects §12.6, §12.11): the wizard's choice between PUBLIC and
 * a workspace network profile, and `GET /api/agents/{id}/network` — the placement as
 * it stands, with the ENIs in the profile's subnets. The profile list itself comes
 * from `networkProfilesApi` (`./projects`).
 */
import { getJson } from "../api";
import type { NetworkProfile } from "./projects";

const enc = encodeURIComponent;

export type NetworkMode = "PUBLIC" | "VPC";
export type ProfileStatus = "unchecked" | "ok" | "warn" | "blocked";

export interface AgentNetworkProfile {
  id: string;
  /** the spec names a profile this workspace no longer has */
  missing?: boolean;
  name?: string;
  vpc_id?: string | null;
  subnet_ids?: string[];
  security_group_ids?: string[];
  internet?: string;
  status?: ProfileStatus;
  checked_at?: string | null;
  archived?: boolean;
  blocking?: number;
  warnings?: number;
}

export interface AgentEni {
  id: string | null;
  subnet_id: string | null;
  availability_zone: string | null;
  status: string | null;
  private_ip: string | null;
  description: string | null;
}

export interface AgentEnis {
  /** `unavailable`: the workspace role cannot read ENIs (older spoke template) */
  status: "ok" | "unavailable";
  error?: string;
  /** ENIs are listed per profile: every agent on the same subnets + SGs shares them */
  scope?: "profile";
  count?: number;
  in_use?: number;
  interfaces: AgentEni[];
}

export interface AgentNetwork {
  agent_id: string;
  mode: NetworkMode;
  profile: AgentNetworkProfile | null;
  vpc: { subnets: string[]; security_groups: string[] } | null;
  enis: AgentEnis | null;
}

export const agentNetworkPath = (agentId: string) => `/api/agents/${enc(agentId)}/network`;

export const getAgentNetwork = (agentId: string) => getJson<AgentNetwork>(agentNetworkPath(agentId));

/** One profile option of the wizard's network step. */
export interface NetworkChoice {
  profile: NetworkProfile;
  /** a blocked profile cannot be chosen (design §12.3) */
  disabled: boolean;
  /** warn / unchecked: choosable, with a warning */
  caution: boolean;
}

/**
 * The profiles the wizard offers: the project's allowed set (an administrator's list
 * carries the whole workspace — only `allowed` ones are usable by this project's
 * agents), live only, the default first. The stored `currentId` stays listed even if
 * it is blocked, so a re-publish shows where the agent runs.
 */
export function networkChoices(profiles: NetworkProfile[], currentId = ""): NetworkChoice[] {
  return profiles
    .filter((p) => !p.archived_at && (p.allowed || p.id === currentId))
    .sort((a, b) => Number(b.is_default) - Number(a.is_default) || a.name.localeCompare(b.name))
    .map((profile) => ({
      profile,
      disabled: profile.status === "blocked" && profile.id !== currentId,
      caution: profile.status === "warn" || profile.status === "unchecked",
    }));
}
