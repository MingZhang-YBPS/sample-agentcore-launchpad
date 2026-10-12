import "./issues.css";

import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { Filters, PageHead } from "../ui";
import { AnswerRules } from "./issues/AnswerRules";
import { IssueBox } from "./issues/IssueBox";
import { Reviewers } from "./issues/Reviewers";

const VIEWS = ["box", "answers", "reviewers"] as const;
type View = (typeof VIEWS)[number];

/**
 * The business self-service loop on one page: the issue box (find, fix, verify,
 * close), the curated answers a fix can create, and the reviewer links that feed
 * the box. Same `?view=` states as V2 (`answers`, `reviewers`; the box is bare).
 */
export function V3Issues() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const raw = params.get("view");
  const view: View = (VIEWS as readonly string[]).includes(raw ?? "") ? (raw as View) : "box";

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.issues.eyebrow")}
        title={t("selfService.issues.title")}
        sub={t("v3.issues.sub")}
        end={
          <Filters
            value={view}
            onChange={(next) => setParams(next === "box" ? {} : { view: next })}
            options={VIEWS.map((value) => ({ value, label: t(`selfService.tab.${value}`) }))}
          />
        }
      />
      {view === "box" ? <IssueBox /> : view === "answers" ? <AnswerRules /> : <Reviewers />}
    </div>
  );
}
