// Why a control is off (guided-console §4, storyline-ux §4): a disabled V3 button or
// field carries a `disabledReason`, shown on hover and focus and wired to the control
// with aria-describedby. The reason points at the missing story step or permission,
// with a link through the action table when there is somewhere to go.
//
// `Btn` (ui.tsx) takes the prop directly; native controls are wrapped in
// `<Reasoned disabledReason=…>`. A button stays focusable (aria-disabled instead of
// disabled) so a keyboard user can reach the reason; a select or input keeps its
// native lock and gets the description. The `launchpad/disabled-reason` ESLint rule
// flags a disabled control with neither (eslint-rules/disabled-reason.js).
import "./story/story.css";

import { ArrowRight } from "lucide-react";
import {
  cloneElement, isValidElement, type MouseEvent, type ReactElement, type ReactNode, useId,
} from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { type DisabledReason, reasonText } from "./reasons";
import { type ActionParams, resolveNext } from "./story/actions";

/** The reason's flag: the words, and the step to go to. Always in the DOM so the id resolves. */
export function ReasonTip({ id, reason, params }: { id: string; reason: DisabledReason; params?: ActionParams }) {
  const { t } = useTranslation();
  const next = typeof reason === "string" || !reason.next ? null : resolveNext([reason.next], params)[0] ?? null;
  return (
    <span className="v3-why-tip" role="tooltip" id={id} data-testid="v3-why-tip">
      <span className="v3-why-tag">{t("story.reason.tag")}</span>
      <span className="v3-why-text">{reasonText(reason)}</span>
      {next && (
        <Link className="v3-why-go" to={next.to} data-action={next.key}>
          {t(next.label)} <ArrowRight size={12} aria-hidden="true" />
        </Link>
      )}
    </span>
  );
}

type ControlProps = {
  disabled?: boolean;
  onClick?: (e: MouseEvent) => void;
  "aria-describedby"?: string;
  "aria-disabled"?: boolean | "true" | "false";
};

/**
 * A native control with a reason: `<Reasoned disabledReason={…}><select disabled={…} /></Reasoned>`.
 * Only a disabled child shows the reason; an enabled one renders as it is.
 */
export function Reasoned({
  disabledReason,
  children,
}: {
  disabledReason: DisabledReason | null | undefined;
  children: ReactElement<ControlProps>;
}) {
  const id = useId();
  if (!isValidElement<ControlProps>(children) || !children.props.disabled || !disabledReason) return children;
  const own = children.props["aria-describedby"];
  const describedBy = own ? `${own} ${id}` : id;
  const isButton = children.type === "button";
  const control = isButton
    ? cloneElement(children, {
        disabled: false,
        "aria-disabled": "true",
        "aria-describedby": describedBy,
        onClick: (e: MouseEvent) => e.preventDefault(),
      })
    : cloneElement(children, { "aria-describedby": describedBy });
  return <Why id={id} reason={disabledReason}>{control}</Why>;
}

/** The wrapper both Btn and Reasoned render: the control, then its flag. */
export function Why({ id, reason, children }: { id: string; reason: DisabledReason; children: ReactNode }) {
  return (
    <span className="v3-why" data-testid="v3-why">
      {children}
      <ReasonTip id={id} reason={reason} />
    </span>
  );
}
