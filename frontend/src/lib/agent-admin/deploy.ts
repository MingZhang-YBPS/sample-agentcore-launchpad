import { useCallback, useEffect, useState } from "react";

import { api, type AgentInfo, type DeploymentInfo, errorMessage, type JobInfo } from "../api";
import { fmtElapsed, parseUtc, progressKey, runningStage } from "./deployStatus";

/**
 * One agent and its latest deploy job, followed while a deploy is in flight
 * (every 2.5 s) until it settles. `reload()` reads both again.
 */
export function useAgentDetail(id: string, pollMs = 2500) {
  const [agent, setAgent] = useState<AgentInfo | null>(null);
  const [job, setJob] = useState<JobInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const deploying = agent?.status === "deploying";

  useEffect(() => {
    let live = true;
    const load = async () => {
      setLoading(true);
      try {
        const fresh = await api.getAgent(id);
        if (!live) return;
        setAgent(fresh);
        setError(null);
        const latestJob = fresh.deployments?.[0]?.job_id;
        if (latestJob) {
          const j = await api.getJob(latestJob).catch(() => null);
          if (live) setJob(j);
        }
      } catch (err) {
        if (live) setError(errorMessage(err));
      } finally {
        if (live) setLoading(false);
      }
    };
    void load();
    const timer = deploying ? window.setInterval(() => void load(), pollMs) : undefined;
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [id, deploying, nonce, pollMs]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const jobId = agent?.deployments?.[0]?.job_id ?? null;
  return {
    agent,
    /** the job of the latest deployment only — never an older job's log */
    job: jobId ? job : null,
    error,
    loading,
    deploying,
    reload,
  };
}

/** The deploy's state in plain terms: failed / in flight, and the elapsed time. */
export function deployPhase(deployment: DeploymentInfo, now: number) {
  const failed = deployment.status === "failed" || deployment.stages.some((s) => s.status === "failed");
  const inFlight = !failed && deployment.status === "running";
  const started = parseUtc(deployment.started_at);
  const ended = parseUtc(deployment.ended_at);
  const elapsed = started != null ? fmtElapsed((inFlight ? now : (ended ?? now)) - started) : null;
  return { failed, inFlight, succeeded: !inFlight && !failed && deployment.status === "succeeded", elapsed };
}

/**
 * Live progress of a deploy: a once-a-second clock while it runs, the phase,
 * and the key of the plain-language line for the stage now running
 * (`v2.agents.progress.<key>` in V2, `v3.agent.progress.line.<key>` in V3).
 */
export function useDeployProgress(deployment: DeploymentInfo, method: string) {
  const [now, setNow] = useState(() => Date.now());
  const phase = deployPhase(deployment, now);
  const { inFlight } = phase;
  useEffect(() => {
    if (!inFlight) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [inFlight]);
  return { ...phase, progressKey: progressKey(method, runningStage(deployment)) };
}
