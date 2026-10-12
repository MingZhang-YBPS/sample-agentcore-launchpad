import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, errorMessage } from "../lib/api";
import { type AnnotateItem, type AnnotateQueue, annotateApi } from "../lib/annotate";
import { V2Lang } from "../v2/Lang";
import { Alert, Button, Segmented, Tag } from "../v2/ui";
import "./share.css";
import "./review.css";

/**
 * The account-free page an annotation link opens (Agent-DLC §7.4). Standalone like
 * `ReviewPage`: no AuthGate, no console shell, no workspace header.
 *
 * The person here is deciding what the *correct* outcome would have been, which is
 * only worth anything if they have not been shown the judge's verdict first — so the
 * server withholds it and this page has nowhere to display it. They also cannot see
 * the other annotator's labels or the agreement numbers: those are the console's, and
 * seeing them would turn independent labelling into agreement-seeking.
 */
export function AnnotatePage({ token }: { token: string }) {
  const { t } = useTranslation();
  const [queue, setQueue] = useState<AnnotateQueue | null>(null);
  const [gone, setGone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    document.body.classList.add("v2-body");
    return () => document.body.classList.remove("v2-body");
  }, []);

  useEffect(() => {
    let live = true;
    annotateApi
      .queue(token)
      .then((result) => live && setQueue(result))
      .catch((err: unknown) => {
        if (!live) return;
        // every unusable link is the same 404: do not distinguish expired from unknown
        if (err instanceof ApiError && err.code === "share.not_found") setGone(true);
        else setError(errorMessage(err));
      });
    return () => {
      live = false;
    };
  }, [token]);

  if (gone) {
    return (
      <div className="v2 share-page share-gone" data-testid="annotate-gone">
        <h1>{t("annotatePage.notFoundTitle")}</h1>
        <p>{t("annotatePage.notFoundBody")}</p>
      </div>
    );
  }
  if (!queue) {
    return (
      <div className="v2 share-page share-gone" role="status">
        {error ? <Alert tone="error">{error}</Alert> : t("annotatePage.loading")}
      </div>
    );
  }
  const closed = queue.status === "closed";
  return (
    <div className="v2 share-page review-page" data-testid="annotate-page">
      <header className="share-head">
        <h1>{t("annotatePage.title")}</h1>
        <div className="share-head-end">
          <span className="share-expiry">
            {t("annotatePage.progress", { done: queue.labelled, total: queue.total })}
          </span>
          <V2Lang />
        </div>
      </header>
      <p className="review-intro">{t("annotatePage.intro", { criterion: queue.criterion_key })}</p>
      <Alert>{t("annotatePage.blindHint")}</Alert>
      {closed && <Alert tone="warn">{t("annotatePage.closed")}</Alert>}
      <main className="review-list">
        {queue.items.length === 0 && <p>{t("annotatePage.empty")}</p>}
        {queue.items.map((item) => (
          <AnnotateCard
            key={item.ref}
            token={token}
            item={item}
            purpose={queue.purpose}
            disabled={closed}
            onSaved={setQueue}
          />
        ))}
      </main>
    </div>
  );
}

const LABELS = ["pass", "fail", "inconclusive"] as const;

function AnnotateCard({
  token,
  item,
  purpose,
  disabled,
  onSaved,
}: {
  token: string;
  item: AnnotateItem;
  purpose: AnnotateQueue["purpose"];
  disabled: boolean;
  onSaved: (queue: AnnotateQueue) => void;
}) {
  const { t } = useTranslation();
  const [rationale, setRationale] = useState(item.my_rationale ?? "");
  const [expected, setExpected] = useState(item.my_answer ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const send = (label: string) => {
    setBusy(true);
    setError(null);
    annotateApi
      .label(token, { item_ref: item.ref, label, rationale, answer: expected })
      .then(onSaved)
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setBusy(false));
  };

  return (
    <section className="review-card">
      <div className="review-q">
        <h3>{t("annotatePage.question")}</h3>
        <p>{item.input || t("annotatePage.noQuestion")}</p>
      </div>
      <div className="review-a">
        <h3>{t("annotatePage.answer")}</h3>
        <p>{item.answer || "—"}</p>
      </div>
      <div className="review-actions">
        {item.my_label && <Tag tone="green">{t("annotatePage.saved")}</Tag>}
        <Segmented
          value={item.my_label ?? ""}
          onChange={(value) => send(value)}
          options={LABELS.map((l) => ({ value: l, label: t(`annotatePage.label.${l}`) }))}
        />
      </div>
      <label className="review-note">
        {t("annotatePage.rationale")}
        <textarea
          rows={2}
          value={rationale}
          maxLength={4000}
          disabled={disabled || busy}
          onChange={(e) => setRationale(e.target.value)}
        />
      </label>
      {purpose === "golden_answer" && (
        <label className="review-note">
          {t("annotatePage.expected")}
          <textarea
            rows={3}
            value={expected}
            maxLength={16000}
            disabled={disabled || busy}
            onChange={(e) => setExpected(e.target.value)}
          />
        </label>
      )}
      {(rationale !== (item.my_rationale ?? "") || expected !== (item.my_answer ?? "")) &&
        item.my_label && (
          <div className="review-actions">
            <Button size="sm" disabled={disabled || busy} onClick={() => send(item.my_label as string)}>
              {t("annotatePage.saveNote")}
            </Button>
          </div>
        )}
      {error && <Alert tone="error">{error}</Alert>}
    </section>
  );
}
