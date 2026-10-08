import { describe, expect, test } from "bun:test";
import { FINDING, LOADED, prepared, progressOf } from "./readerMode";
import { session } from "./test/fixtures";
import type { ReadingView, SessionView, WorkflowView } from "./types";

const quote = { id: "quote-01", index: 0, label: "第 1 行", heading: null, excerpt: "…", text: "…", chars: 1, part: 0 };
const wf = (over: Partial<WorkflowView> = {}): WorkflowView => ({ waiting: null, stage: null, active_command: null, error: null, failed_command_id: null, last_failed: null, failed_node: null, runner_active: true, ...over });
function graph(reading: Partial<ReadingView>, w: Partial<WorkflowView> = {}, over: Partial<SessionView> = {}): SessionView {
  return session({
    workflow_runner: "langgraph",
    workflow: wf(w),
    reading: { index: 0, total: 1, done: false, quotes: [quote], parts: [], guides: {}, qa: {}, compacts: {}, quiz_source: null, ...reading },
    current_question: null,
    state: { concept_order: [], concept_index: 0, phase: "reading", concepts: {} },
    ...over,
  });
}

describe("readerMode · the fill of a switch", () => {
  test("finding, then loaded, then the guide's stages in order, full once the guide is written", () => {
    expect(progressOf("guide", null)).toBe(FINDING);
    const early = progressOf("guide", graph({ job: { stage: "guide", detail_stage: "context", error: null } }));
    const late = progressOf("guide", graph({ job: { stage: "guide", detail_stage: "review", error: null } }));
    expect(early).toBeGreaterThan(LOADED);
    expect(late).toBeGreaterThan(early);
    expect(late).toBeLessThan(1);
    expect(prepared("guide", graph({ guides: { "0": { chinese_text: null, guide: "…", key_points: [], check: "" } } }, { waiting: "student", runner_active: false }))).toBe(true);
  });

  test("出題 waits past the guide for the questions; an error or a stalled workflow shows what there is", () => {
    const guided = graph({ guides: { "0": { chinese_text: null, guide: "…", key_points: [], check: "" } } }, { waiting: "student", runner_active: false });
    expect(prepared("quiz", guided)).toBe(false);
    const writing = graph({ guides: guided.reading!.guides, job: { stage: "generate", detail_stage: "quiz_generate", error: null } });
    expect(progressOf("quiz", writing)).toBeGreaterThan(progressOf("quiz", graph({ job: { stage: "guide", detail_stage: "review", error: null } })));
    expect(prepared("quiz", graph({ done: true }, { waiting: "answer" }))).toBe(true);
    expect(prepared("guide", graph({ job: { stage: "guide", error: { code: "x", detail: "" } } }))).toBe(true);
    expect(prepared("guide", graph({}, { runner_active: false }))).toBe(true);
  });
});
