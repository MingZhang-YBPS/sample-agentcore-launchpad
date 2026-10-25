/**
 * Typed client of the storyline reads (storyline-ux §2.3, `routers/agent_story.py`):
 * one agent's storyline facts (cached 30 s server-side) and the caller's turns on
 * every agent of the current project. Both are read-only.
 */
import type { AgentStoryFacts, Chapter, StoryAgent, WorkshopRole } from "../../v3/story/stories";
import { getJson } from "../api";

export interface AgentStoryPayload {
  agent: StoryAgent;
  facts: AgentStoryFacts;
  cached: boolean;
  ttl_seconds: number;
}

export interface NextStepRow {
  agent: StoryAgent;
  step: string;
  chapter: Chapter;
  perm: string;
  roles: WorkshopRole[];
}

export interface MyNextSteps {
  workspace_id: string;
  project_id: string;
  roles: WorkshopRole[];
  /** the current step of each agent where it is the caller's turn */
  mine: NextStepRow[];
  /** the others' turns the caller is waiting on */
  waiting: NextStepRow[];
  computed_at: string;
}

export const storyApi = {
  agentStory: (agentId: string) => getJson<AgentStoryPayload>(`/api/agents/${encodeURIComponent(agentId)}/story`),
  myNextSteps: () => getJson<MyNextSteps>("/api/me/next-steps"),
};
