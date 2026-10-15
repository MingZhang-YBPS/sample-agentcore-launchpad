// The console's local ESLint plugin (`launchpad/*`), loaded by eslint.config.js.
import disabledReason from "./disabled-reason.js";

export default {
  meta: { name: "eslint-plugin-launchpad" },
  rules: { "disabled-reason": disabledReason },
};
