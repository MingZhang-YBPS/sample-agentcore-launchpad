import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

import launchpad from "./eslint-rules/index.js";

export default [
  // dist = build output; src/studio/lib = pure generator/validator files ported
  // verbatim from upstream strands_studio_ui (kept byte-faithful for re-syncing —
  // do not restyle). tsc still type-checks them under the project tsconfig.
  { ignores: ["dist", "src/studio/lib/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    // node-side evidence tooling (playwright screenshot runs) and the local
    // ESLint plugin, not app code
    files: ["scripts/**/*.mjs", "eslint-rules/**/*.js"],
    languageOptions: { ecmaVersion: 2022, globals: { ...globals.node, ...globals.browser } },
  },
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
    },
  },
  {
    // a disabled V3 control says why (guided-console §4): the places that predate
    // the rule are counted in the baseline, which only shrinks (scripts/story_checks.mjs)
    files: ["src/v3/**/*.tsx"],
    ignores: ["**/*.test.tsx"],
    plugins: { launchpad },
    rules: {
      "launchpad/disabled-reason": ["error", { baseline: "eslint-rules/disabled-reason-baseline.json" }],
    },
  },
];
