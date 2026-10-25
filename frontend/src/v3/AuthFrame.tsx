import "./v3.css";
import "./auth.css";

import { type ReactNode, useEffect } from "react";

import { Lang } from "./Lang";
import { ThemeSwitch } from "./ThemeSwitch";

/** The dark V3 ground on <body> while a pre-shell page is mounted. */
function useV3Body() {
  useEffect(() => {
    document.body.classList.add("v3-body");
    return () => document.body.classList.remove("v3-body");
  }, []);
}

/**
 * The V3 chrome of the pages shown before the console shell (sign-in / register):
 * the shell's top bar (brand + language) over the command-center grid, no rail.
 */
export function V3AuthFrame({ end, children, testId }: {
  end?: ReactNode;
  children: ReactNode;
  testId?: string;
}) {
  useV3Body();
  return (
    <div className="v3 v3-auth" data-testid={testId}>
      <header className="v3-top">
        <span className="v3-brand">
          <span className="v3-brand-mark" aria-hidden="true" />
          Launchpad
          <sup>V3</sup>
        </span>
        <div className="v3-top-right">
          <ThemeSwitch />
          <Lang />
          {end}
        </div>
      </header>
      <main className="v3-auth-main">{children}</main>
    </div>
  );
}

/** Centered spinner on the V3 ground while a session / workspace resolves. */
export function V3AuthLoading({ label }: { label: string }) {
  useV3Body();
  return (
    <div className="v3 v3-auth-loading" role="status">
      <span className="v3-auth-spinner" aria-hidden="true" />
      <span className="sr-only">{label}</span>
    </div>
  );
}
