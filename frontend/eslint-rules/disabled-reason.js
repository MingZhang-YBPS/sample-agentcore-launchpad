// `launchpad/disabled-reason` — a disabled V3 control says why (guided-console §4,
// storyline-ux §4). A greyed-out button with no reason leaves the user stuck; with
// one it points at the missing story step or permission.
//
// Flagged: any JSX element with a `disabled` attribute and no `disabledReason` (the
// V3 `Btn` prop) whose direct parent is not a reason wrapper (`<Reasoned
// disabledReason=…>`, for native selects and inputs); likewise `confirmDisabled` without `confirmReason` (the `Confirm` dialog).
// Not flagged — a lock that only lasts while a request is in flight, which needs no
// reason: `disabled={busy}`, `disabled={saving || loading}`, `disabled={busy === id}`,
// `disabled={ready && loading}`
// (every operand names in-flight state, see IN_FLIGHT); a literal `false`; and
// `<option disabled>` (the list shows it in place).
//
// `baseline` (a JSON file of `{ "<path from frontend/>": count }`) holds the places
// that predate the rule: a file is reported only when it has more than its
// baseline count, so `npm run lint` fails on new ones. `scripts/story_checks.mjs`
// runs the rule without the baseline and fails when a count grows or when a
// fixed place is still listed (the baseline only shrinks).
import { readFileSync } from "node:fs";
import { relative, resolve } from "node:path";

/** Names that only ever mean "a request is running". */
export const IN_FLIGHT =
  /^(is)?(busy|saving|loading|pending|submitting|working|running|deleting|removing|creating|sending|uploading|importing|exporting|refreshing|fetching|applying|syncing|starting|stopping|publishing|signing|testing|probing|deploying|downloading|retrying|inflight|in_flight|posting|toggling|revoking|rotating|claiming|resetting|repairing|sharing|adding|approving|rejecting|archiving|restoring|checking|scanning|launching|generating|verifying|inviting|transferring|updating|converting|seeding|sealing|reloading)$|^busy|busy$|inflight$/i;

/** Each lock prop and the props that answer it; `Confirm`'s held confirm button too. */
const DEFAULTS = {
  props: { disabled: ["disabledReason"], confirmDisabled: ["confirmReason"] },
  wrappers: ["Reasoned"],
  ignore: ["option"],
};

function elementName(name) {
  if (name.type === "JSXIdentifier") return name.name;
  if (name.type === "JSXMemberExpression") return `${elementName(name.object)}.${name.property.name}`;
  if (name.type === "JSXNamespacedName") return `${name.namespace.name}:${name.name.name}`;
  return "";
}

function attr(opening, key) {
  return opening.attributes.find((a) => a.type === "JSXAttribute" && a.name.type === "JSXIdentifier" && a.name.name === key);
}

/** The last name in `busy`, `state.busy`, `mutation.isPending`, `isBusy()`. */
function tailName(node) {
  if (node.type === "Identifier") return node.name;
  if (node.type === "MemberExpression" && !node.computed && node.property.type === "Identifier") return node.property.name;
  if (node.type === "ChainExpression") return tailName(node.expression);
  if (node.type === "CallExpression") return tailName(node.callee);
  return null;
}

/** Whether a `disabled` expression only says a request is running. */
export function inFlightOnly(node, pattern = IN_FLIGHT) {
  if (!node) return false;
  switch (node.type) {
    case "TSNonNullExpression":
    case "TSAsExpression":
    case "ChainExpression":
      return inFlightOnly(node.expression, pattern);
    case "UnaryExpression":
      // `!!busy`
      return node.operator === "!" && node.argument.type === "UnaryExpression" && inFlightOnly(node.argument.argument, pattern);
    case "LogicalExpression":
      // `busy || saving`: every side in flight; `ready && loading`: true only while loading
      if (node.operator === "&&") return inFlightOnly(node.left, pattern) || inFlightOnly(node.right, pattern);
      return inFlightOnly(node.left, pattern) && inFlightOnly(node.right, pattern);
    case "BinaryExpression":
      // `busy === row.id`, `busy !== null`: the lock is still the running request
      return ["===", "!==", "==", "!="].includes(node.operator) && (inFlightOnly(node.left, pattern) || inFlightOnly(node.right, pattern));
    case "CallExpression":
      // `Boolean(busy)`
      if (node.callee.type === "Identifier" && node.callee.name === "Boolean") return inFlightOnly(node.arguments[0], pattern);
      return false;
    default: {
      const name = tailName(node);
      return Boolean(name && pattern.test(name));
    }
  }
}

function readBaseline(file, cwd) {
  if (!file) return null;
  try {
    return JSON.parse(readFileSync(resolve(cwd, file), "utf-8"));
  } catch {
    return {};
  }
}

const baselines = new Map();

export default {
  meta: {
    type: "suggestion",
    docs: { description: "a disabled V3 control carries disabledReason (or busy while a request runs)" },
    schema: [
      {
        type: "object",
        properties: {
          baseline: { type: "string" },
          props: { type: "object", additionalProperties: { type: "array", items: { type: "string" } } },
          wrappers: { type: "array", items: { type: "string" } },
          ignore: { type: "array", items: { type: "string" } },
        },
        additionalProperties: false,
      },
    ],
    messages: {
      missing:
        "<{{name}}> is disabled without a reason: it needs a disabledReason that names the missing story step or permission (Btn disabledReason=…, or wrap it in <Reasoned disabledReason=…>); when it is only locked while a request runs, say so with Btn `busy` instead of `disabled`.",
      overBaseline:
        "<{{name}}> is disabled without a reason: this file has {{count}}, its baseline allows {{allowed}} (eslint-rules/disabled-reason-baseline.json only shrinks).",
    },
  },
  create(context) {
    const opts = { ...DEFAULTS, ...(context.options[0] ?? {}) };
    const cwd = context.cwd ?? process.cwd();
    let allowed = null;
    if (opts.baseline) {
      const key = resolve(cwd, opts.baseline);
      if (!baselines.has(key)) baselines.set(key, readBaseline(opts.baseline, cwd));
      const file = relative(cwd, context.filename ?? context.getFilename()).split("\\").join("/");
      allowed = baselines.get(key)[file] ?? 0;
    }
    const found = [];
    return {
      JSXOpeningElement(node) {
        const name = elementName(node.name);
        if (opts.ignore.includes(name)) return;
        for (const [lock, answers] of Object.entries(opts.props)) {
          const locked = attr(node, lock);
          if (!locked || answers.some((p) => attr(node, p))) continue;
          const value = locked.value;
          if (value && value.type === "JSXExpressionContainer") {
            const expr = value.expression;
            if (expr.type === "Literal" && expr.value === false) continue;
            if (inFlightOnly(expr)) continue;
          }
          if (lock === "disabled") {
            const parent = node.parent?.parent;
            if (parent?.type === "JSXElement") {
              const wrapper = parent.openingElement;
              if (opts.wrappers.includes(elementName(wrapper.name)) && attr(wrapper, "disabledReason")) continue;
            }
          }
          found.push({ node, name: lock === "disabled" ? name : `${name} ${lock}` });
        }
      },
      "Program:exit"() {
        if (allowed !== null && found.length <= allowed) return;
        for (const { node, name } of found) {
          if (allowed === null) context.report({ node, messageId: "missing", data: { name } });
          else context.report({ node, messageId: "overBaseline", data: { name, count: found.length, allowed } });
        }
      },
    };
  },
};
