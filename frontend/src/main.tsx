import "@fontsource-variable/archivo/wdth.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "@fontsource/ibm-plex-mono/600.css";
import "./theme/tokens.css";
import "./theme/app.css";
import "./i18n";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { AuthGate } from "./auth/AuthGate";
import { installWorkspaceHeader } from "./lib/workspace-header";

// The account-free share page (T14) is rendered on its own: no AuthGate (a share
// visitor has no account and must never be shown the sign-in form), no console
// shell, and no workspace header — the link in the URL names the workspace.
const shareToken = /^\/s\/([^/]+)\/?$/.exec(window.location.pathname)?.[1];

// The SME review page (T34): the same account-free, shell-free rendering.
const reviewToken = /^\/r\/([^/]+)\/?$/.exec(window.location.pathname)?.[1];

// The annotation page (Agent-DLC §7.4): an expert labels sessions for judge
// calibration without an account. Matched before `/r/:token` so its longer path wins.
const annotateToken = /^\/r\/annotate\/([^/]+)\/?$/.exec(window.location.pathname)?.[1];

if (annotateToken) {
  void import("./share/AnnotatePage").then(({ AnnotatePage }) => {
    createRoot(document.getElementById("root")!).render(
      <StrictMode>
        <AnnotatePage token={decodeURIComponent(annotateToken)} />
      </StrictMode>,
    );
  });
} else if (reviewToken) {
  void import("./share/ReviewPage").then(({ ReviewPage }) => {
    createRoot(document.getElementById("root")!).render(
      <StrictMode>
        <ReviewPage token={decodeURIComponent(reviewToken)} />
      </StrictMode>,
    );
  });
} else if (shareToken) {
  void import("./share/SharePage").then(({ SharePage }) => {
    createRoot(document.getElementById("root")!).render(
      <StrictMode>
        <SharePage token={decodeURIComponent(shareToken)} />
      </StrictMode>,
    );
  });
} else {
  // Before the first render, so no mount effect can reach the backend without
  // naming the workspace it means.
  installWorkspaceHeader();

  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <AuthGate>
        <App />
      </AuthGate>
    </StrictMode>,
  );
}
