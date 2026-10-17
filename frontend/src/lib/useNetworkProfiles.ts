import { useEffect, useState } from "react";

import { errorMessage } from "./api";
import { type NetworkProfile, networkProfilesApi } from "./api/projects";

/** The profiles this project may pick (`GET /api/network-profiles`); null while loading. */
export function useNetworkProfiles(): { profiles: NetworkProfile[] | null; error: string | null } {
  const [profiles, setProfiles] = useState<NetworkProfile[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    networkProfilesApi
      .list()
      .then((res) => live && setProfiles(res.profiles))
      .catch((e: unknown) => {
        if (!live) return;
        setProfiles([]);
        setError(errorMessage(e));
      });
    return () => {
      live = false;
    };
  }, []);
  return { profiles, error };
}
