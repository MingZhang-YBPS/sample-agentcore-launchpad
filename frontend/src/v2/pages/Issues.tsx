import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { PageHeader, SubTabs } from "../ui";
import { AnswerRules } from "./selfservice/AnswerRules";
import { IssueBox } from "./selfservice/IssueBox";
import { Reviewers } from "./selfservice/Reviewers";

const VIEWS = ["box", "answers", "reviewers"] as const;
type View = (typeof VIEWS)[number];

/**
 * T34–T36 — the business self-service loop on one page: the issue box (find, fix,
 * verify, close), the curated answers a fix can create, and the reviewer links that feed
 * the box. Sub-surfaces use `?view=` like the other complex pages.
 */
export function V2Issues() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const raw = params.get("view");
  const view: View = (VIEWS as readonly string[]).includes(raw ?? "") ? (raw as View) : "box";

  return (
    <>
      <PageHeader
        title={t("selfService.issues.title")}
        desc={t("selfService.issues.desc")}
        tabs={
          <SubTabs
            value={view}
            onChange={(next) => setParams(next === "box" ? {} : { view: next })}
            tabs={VIEWS.map((value) => ({ value, label: t(`selfService.tab.${value}`) }))}
          />
        }
      />
      {view === "box" ? <IssueBox /> : view === "answers" ? <AnswerRules /> : <Reviewers />}
    </>
  );
}
