// The Agent-DLC term cards (guided-console §5.2, storyline-ux §4): each word with its
// one sentence, who is responsible, the related words, and — the point of the card —
// the storyline steps it is used in, each a link through the step's action. Copy is
// `story.term.<key>.{name,short,owner}` (en + zh-CN); `terms.test.ts` checks every
// step key exists in `AGENT_STORY`.

export const DLC_TERMS = [
  "criteria",
  "criteriaSet",
  "criteriaTier",
  "goldenSet",
  "goldenSplit",
  "caseTier",
  "admission",
  "judgeCalibration",
  "gate",
  "passK",
  "waiver",
  "releaseRecord",
  "driftWatch",
  "repairLadder",
  "scorecard",
] as const;

export type DlcTerm = (typeof DLC_TERMS)[number];

export interface DlcTermCard {
  /** `AGENT_STORY` step keys, in story order */
  steps: readonly string[];
  related: readonly DlcTerm[];
}

export const DLC_TERM_CARDS: Record<DlcTerm, DlcTermCard> = {
  criteria: {
    steps: ["evaluate.criteria.draft", "evaluate.criteria.sign", "evaluate.gate", "feedback.loop"],
    related: ["criteriaSet", "criteriaTier", "goldenSet"],
  },
  criteriaSet: {
    steps: ["evaluate.criteria.draft", "evaluate.criteria.sign", "release.sign"],
    related: ["criteria", "gate"],
  },
  criteriaTier: { steps: ["evaluate.criteria.draft", "evaluate.gate"], related: ["criteria", "waiver"] },
  goldenSet: {
    steps: ["evaluate.golden.seal", "evaluate.dev_run", "evaluate.gate", "feedback.loop"],
    related: ["goldenSplit", "caseTier", "admission"],
  },
  goldenSplit: { steps: ["evaluate.golden.seal", "evaluate.dev_run", "evaluate.gate"], related: ["goldenSet", "gate"] },
  caseTier: { steps: ["evaluate.golden.seal", "evaluate.dev_run"], related: ["goldenSet"] },
  admission: { steps: ["feedback.triage", "feedback.loop"], related: ["goldenSet", "repairLadder"] },
  judgeCalibration: { steps: ["evaluate.calibrate", "evaluate.gate"], related: ["criteria", "scorecard"] },
  gate: { steps: ["evaluate.gate", "release.sign"], related: ["criteriaSet", "waiver", "releaseRecord"] },
  passK: { steps: ["evaluate.criteria.draft", "evaluate.dev_run"], related: ["criteria", "gate"] },
  waiver: { steps: ["release.sign"], related: ["gate", "criteriaTier"] },
  releaseRecord: { steps: ["release.sign", "observe.traces"], related: ["gate", "waiver"] },
  driftWatch: { steps: ["observe.traces", "observe.alerts"], related: ["criteria", "scorecard"] },
  repairLadder: { steps: ["observe.traces", "feedback.triage"], related: ["admission", "driftWatch"] },
  scorecard: { steps: ["evaluate.dev_run", "observe.traces"], related: ["criteria", "driftWatch"] },
};

export const isDlcTerm = (key: string): key is DlcTerm => (DLC_TERMS as readonly string[]).includes(key);
