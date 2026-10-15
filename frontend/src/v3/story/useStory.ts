// The storyline's reads for the V3 surfaces: one agent's facts (`/story`, cached 30 s
// server-side) and who is looking (their workshop roles in the current project and
// their key check), so a step that is someone else's shows "waiting on …".
import { useCallback, useMemo } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../auth/auth-context";
import type { AgentPermission } from "../../lib/api";
import { storyApi } from "../../lib/api/story";
import { useProject } from "../../workspace/project-context";
import { useWorkspace } from "../../workspace/workspace-context";
import { useLoad, useToast } from "../hooks";
import { storyViewer } from "./flow";
import type { AgentStoryState, StoryViewer } from "./stories";
import { workshopRoles } from "./stories";

/** One agent's storyline state; `null` id loads nothing. `nonce` re-reads after an action. */
export function useAgentStory(agentId: string | null | undefined, nonce: string | number = 0) {
  const { current } = useWorkspace();
  const loaded = useLoad<AgentStoryState | null>(
    () => (agentId ? storyApi.agentStory(agentId).then((p) => ({ agent: p.agent, facts: p.facts })) : Promise.resolve(null)),
    `v3-story:${current?.id ?? ""}:${agentId ?? ""}:${nonce}`,
  );
  return loaded;
}

/** The caller as the role checks see them (the backend's `viewer_roles` + `can`). */
export function useStoryViewer(): StoryViewer {
  const { isAdmin, can } = useAuth();
  const { current } = useProject();
  const roleKeys = (current?.role_sources ?? []).map((s) => s.role_key).join(",");
  const bypass = Boolean(current?.bypass);
  return useMemo(
    () => storyViewer(
      workshopRoles(roleKeys ? roleKeys.split(",") : [], isAdmin || bypass),
      isAdmin,
      (key) => can(key as AgentPermission),
    ),
    [roleKeys, bypass, isAdmin, can],
  );
}

/** Copy a step's console link for the person whose turn it is. */
export function useCopyStepLink() {
  const { t } = useTranslation();
  const toast = useToast();
  return useCallback((path: string) => {
    const url = `${window.location.origin}${path}`;
    const done = () => toast("ok", t("story.card.copied"));
    if (navigator.clipboard?.writeText) navigator.clipboard.writeText(url).then(done, () => toast("ok", url));
    else toast("ok", url);
  }, [t, toast]);
}
