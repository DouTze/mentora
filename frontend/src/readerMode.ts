// How far a switch into the reader's 導讀 / 出題 has come, for the fill of its button: the reading found, loaded,
// then the workflow's stages until the mode has something to show (the current guide written, or the questions).
import type { SessionView } from "./types";
import { isGraph } from "./workflow";

export type ModeKind = "guide" | "quiz";

/** The fill while the reading is being found or started (before there is a session to read). */
export const FINDING = 0.12;
/** The fill once the session is read, before its own stages. */
export const LOADED = 0.28;
/** A switch takes at least this long on screen, so even a ready mode is felt arriving. */
export const MIN_SWITCH_MS = 500;

// the stages of writing one guide, in the order the reading workflow goes through them (reading-graph v1 and v2)
const GUIDE_STAGES = ["segment", "context", "analyze", "symbols", "understand", "source_audit", "revise_source", "plan_bridges",
  "guide", "draft", "body_audit", "review", "verify", "revise", "revise_body"];
// then the questions: the notes they are drawn from, writing and checking them
const QUIZ_STAGES = ["compact", "notes", "notes_audit", "notes_revise", "generate", "quiz_generate", "quiz_audit"];

function stageOf(s: SessionView): string | null {
  const rd = s.reading;
  if (rd?.segmentation?.status === "pending") return "segment";
  return rd?.job?.detail_stage ?? rd?.job?.stage ?? s.workflow?.stage ?? null;
}

function within(stages: string[], stage: string | null): number {
  const i = stage ? stages.indexOf(stage) : -1;
  return i < 0 ? 0 : (i + 1) / (stages.length + 1);
}

/** Nothing is under way and nothing will come by waiting: show what there is. */
function idle(s: SessionView): boolean {
  const wf = s.workflow;
  return !s.reading?.job && s.reading?.segmentation?.status !== "pending" && !wf?.runner_active && !wf?.active_command;
}

function guideShown(s: SessionView): boolean {
  const rd = s.reading;
  return !rd || rd.done || !!rd.guides[String(rd.index)];
}

/** The mode has something to show, or something to say (an error, a closed record): switch to it. */
export function prepared(kind: ModeKind, s: SessionView): boolean {
  if (s.status !== "active" || !isGraph(s) || s.reading?.job?.error || s.workflow?.error) return true;
  const waiting = s.workflow?.waiting;
  if (kind === "guide") return guideShown(s) || waiting === "student" || waiting === "retry" || idle(s);
  if (s.state.phase !== "reading" && (s.current_question || s.summary)) return true;
  if (waiting === "answer" || waiting === "retry" || waiting === "completed") return true;
  // 出題 waits for the questions; a guide still being written comes first (the questions follow it)
  return guideShown(s) && waiting !== "student" && idle(s);
}

/** 0–1: how far the mode is from being shown (1 once prepared). */
export function progressOf(kind: ModeKind, s: SessionView | null): number {
  if (!s) return FINDING;
  if (prepared(kind, s)) return 1;
  const stage = stageOf(s);
  const room = 0.95 - LOADED;
  if (kind === "guide") return LOADED + room * within(GUIDE_STAGES, stage);
  // 出題: the guide's stages take the first part of the way, the questions' the rest
  return guideShown(s) || QUIZ_STAGES.includes(stage ?? "")
    ? LOADED + room * (0.4 + 0.6 * within(QUIZ_STAGES, stage))
    : LOADED + room * 0.4 * within(GUIDE_STAGES, stage);
}
