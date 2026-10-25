import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, errorMessage } from "../lib/api";
import { type ReviewItem, type ReviewQueue, reviewApi } from "../lib/review";
import { V2Lang } from "../v2/Lang";
import { Alert, Button, Tag } from "../v2/ui";
import "./share.css";
import "./review.css";

/**
 * The account-free page a review link opens (T34). Standalone like `SharePage`: no
 * AuthGate, no console shell, no workspace header. A domain expert reads real
 * question/answer pairs and gives a thumbs verdict, a note and -- for a wrong answer --
 * the answer it should have been. Submissions land in the same feedback store the
 * console thumbs write, so the issue box and the evaluation datasets pick them up.
 */
export function ReviewPage({ token }: { token: string }) {
  const { t } = useTranslation();
  const [queue, setQueue] = useState<ReviewQueue | null>(null);
  const [gone, setGone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    document.body.classList.add("v2-body");
    return () => document.body.classList.remove("v2-body");
  }, []);

  useEffect(() => {
    let live = true;
    reviewApi
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
      <div className="v2 share-page share-gone" data-testid="review-gone">
        <h1>{t("reviewPage.notFoundTitle")}</h1>
        <p>{t("reviewPage.notFoundBody")}</p>
      </div>
    );
  }
  if (!queue) {
    return (
      <div className="v2 share-page share-gone" role="status">
        {error ? <Alert tone="error">{error}</Alert> : t("reviewPage.loading")}
      </div>
    );
  }
  const reviewed = queue.items.filter((item) => item.verdict).length;
  return (
    <div className="v2 share-page review-page" data-testid="review-page">
      <header className="share-head">
        <h1>{t("reviewPage.title", { agent: queue.agent.display_name })}</h1>
        <div className="share-head-end">
          <span className="share-expiry">
            {t("reviewPage.progress", { done: reviewed, total: queue.items.length })}
          </span>
          <V2Lang />
        </div>
      </header>
      <p className="review-intro">{t("reviewPage.intro")}</p>
      <main className="review-list">
        {queue.items.length === 0 && <p>{t("reviewPage.empty")}</p>}
        {queue.items.map((item) => (
          <ReviewCard key={item.message_id} token={token} item={item} />
        ))}
      </main>
    </div>
  );
}

function ReviewCard({ token, item }: { token: string; item: ReviewItem }) {
  const { t } = useTranslation();
  const [verdict, setVerdict] = useState<"up" | "down" | null>(item.verdict);
  const [comment, setComment] = useState(item.comment ?? "");
  const [correction, setCorrection] = useState(item.correction ?? "");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(item.verdict !== null);
  const [error, setError] = useState<string | null>(null);

  const submit = async (next: "up" | "down") => {
    setBusy(true);
    setError(null);
    try {
      await reviewApi.rate(token, {
        message_id: item.message_id,
        verdict: next,
        comment: comment.trim() || undefined,
        correction: next === "down" ? correction.trim() || undefined : undefined,
      });
      setVerdict(next);
      setSaved(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className="review-card" data-testid="review-card">
      <div className="review-q">
        <span className="review-label">{t("reviewPage.question")}</span>
        <p>{item.question || "—"}</p>
      </div>
      <div className="review-a">
        <span className="review-label">
          {t("reviewPage.answer")}
          {item.curated && <Tag tone="orange">{t("reviewPage.curated")}</Tag>}
        </span>
        <p>{item.answer}</p>
      </div>
      <div className="review-actions">
        <button
          type="button"
          className={verdict === "up" ? "v2-chat-thumb on up" : "v2-chat-thumb"}
          aria-pressed={verdict === "up"}
          disabled={busy}
          onClick={() => void submit("up")}
          data-testid="review-up"
        >
          <ThumbsUp size={16} aria-hidden="true" /> {t("reviewPage.good")}
        </button>
        <button
          type="button"
          className={verdict === "down" ? "v2-chat-thumb on down" : "v2-chat-thumb"}
          aria-pressed={verdict === "down"}
          disabled={busy}
          onClick={() => setVerdict("down")}
          data-testid="review-down"
        >
          <ThumbsDown size={16} aria-hidden="true" /> {t("reviewPage.wrong")}
        </button>
        {saved && verdict && <span className="review-saved">{t("reviewPage.saved")}</span>}
      </div>
      {verdict === "down" && (
        <div className="review-correction">
          <label>
            {t("reviewPage.correctionLabel")}
            <textarea
              className="v2-textarea"
              rows={3}
              maxLength={4000}
              value={correction}
              onChange={(e) => setCorrection(e.target.value)}
              placeholder={t("reviewPage.correctionPlaceholder")}
            />
          </label>
          <label>
            {t("reviewPage.noteLabel")}
            <input
              className="v2-input"
              maxLength={1000}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
            />
          </label>
          <Button kind="primary" disabled={busy} onClick={() => void submit("down")} testId="review-submit">
            {t("reviewPage.submit")}
          </Button>
        </div>
      )}
      {error && <Alert tone="error">{error}</Alert>}
    </article>
  );
}
