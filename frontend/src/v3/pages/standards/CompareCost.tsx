/**
 * The compare page's cost change (eval-cost §1.3): what the latest run on the fix
 * ladder cost against the first one. Two measured totals joined by a track, the
 * signed difference as a pill, and each part's own difference drawn on a zero axis
 * — left is cheaper, right is dearer — so the part that moved the bill stands out.
 */
import { useTranslation } from "react-i18next";

import { usd } from "../../../lib/api/evalCost";
import type { CostDelta } from "../../../lib/dlc";
import { Panel } from "../../ui";
import "../tasks/cost.css";
import "./compare-cost.css";
import { costDeltaRows, costDeltaSignal } from "./costDelta";

const signed = (value: number) => `${value > 0 ? "+" : value < 0 ? "−" : "±"}${usd(Math.abs(value))}`;

export function CompareCost({ delta }: { delta: CostDelta | undefined }) {
  const { t } = useTranslation();
  if (!delta) return null;
  const measured = delta.before_usd !== null || delta.after_usd !== null;
  const signal = costDeltaSignal(delta.delta_usd);
  const rows = costDeltaRows(delta);
  return (
    <Panel title={t("v3.evalCost.compareTitle")} signal={signal}>
      {!measured ? (
        <p className="v3-std-muted" style={{ margin: 0 }}>{t("v3.evalCost.compareNone")}</p>
      ) : (
        <div className="v3-cmpcost" data-testid="v3-compare-cost">
          <div className="v3-cmpcost-span">
            <div className="v3-cost-fig">
              <span className="k">{t("v3.evalCost.compareFirst")}</span>
              <span className="v">{usd(delta.before_usd)}</span>
            </div>
            <div className="v3-cmpcost-track" aria-hidden="true"><i /></div>
            <div className="v3-cost-fig">
              <span className="k">{t("v3.evalCost.compareLatest")}</span>
              <span className="v">{usd(delta.after_usd)}</span>
            </div>
            {delta.delta_usd !== null ? (
              <span className="v3-cost-delta" data-s={signal}>
                {signed(delta.delta_usd)}
                {delta.delta_pct !== null && ` · ${delta.delta_pct > 0 ? "+" : ""}${delta.delta_pct}%`}
              </span>
            ) : (
              <span className="v3-std-muted">{t("v3.evalCost.compareOneSided")}</span>
            )}
          </div>
          {rows.length > 0 && (<>
            <ul className="v3-cmpcost-parts" aria-label={t("v3.evalCost.compareParts")}>
              {rows.map((row) => {
                const side = (row.usd ?? 0) > 0 ? "up" : (row.usd ?? 0) < 0 ? "down" : "flat";
                return (
                  <li key={row.key} data-part={row.part} data-side={side}>
                    <span className="nm">{t(`v3.evalCost.part.${row.part}`)}</span>
                    <span className="axis" aria-hidden="true">
                      <i style={{ width: `${(row.reach * 50).toFixed(2)}%` }} />
                    </span>
                    <span className="amt">{signed(row.usd ?? 0)}</span>
                  </li>
                );
              })}
            </ul>
            <p className="v3-cmpcost-legend">
              <span>{t("v3.evalCost.compareCheaper")}</span>
              <span>{t("v3.evalCost.compareDearer")}</span>
            </p>
          </>)}
        </div>
      )}
    </Panel>
  );
}
