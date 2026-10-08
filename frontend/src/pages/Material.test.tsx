import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MaterialPage } from "./Material";
import { mockFetch, session } from "../test/fixtures";
import { I18nContext, translate } from "../i18n";

const material = { id: "m1", category_id: null, title: "光合作用", filename: "光合作用.md", kind: "md", unit: "line", unit_count: 4, created_at: "", units: ["# 光合作用", "", "植物利用陽光製造葡萄糖。", "葉綠體是進行光合作用的地方。"] };
const study = { status: "ready", session_id: "study1", planner: "model", progress: 1, error: null, parts: [{ index: 0, from: 1, to: 4, heading: "光合作用" }] };

beforeEach(() => localStorage.clear());
afterEach(cleanup);

function setup(routes: Parameters<typeof mockFetch>[0] = {}) {
  const calls = mockFetch({
    "GET /api/materials/m1": () => ({ json: material }),
    "GET /api/materials/m1/study": () => ({ json: study }),
    "POST /api/materials/m1/study/focus": () => ({ json: study }),
    "POST /api/materials/m1/study/leave": () => ({ json: study }),
    "GET /api/materials/m1/questions": () => ({ json: [] }),
    ...routes,
  });
  const opened: { id: string; quiz?: boolean }[] = [];
  render(<MaterialPage id="m1" library={{ categories: [], materials: [] }} onLibraryChange={async () => {}} onNavigate={() => {}} onOpenSession={(id, o) => opened.push({ id, quiz: o?.quiz })} />);
  return { calls, opened };
}

describe("MaterialPage · reader", () => {
  test("sets Markdown as an article, without the parsed-text unit list or the range tool", async () => {
    setup();
    expect(await screen.findByRole("heading", { name: "光合作用" })).toBeTruthy();
    expect(screen.getByText("植物利用陽光製造葡萄糖。 葉綠體是進行光合作用的地方。".split(" ")[0], { exact: false })).toBeTruthy();
    expect(screen.queryByTestId("unit")).toBeNull();
    expect(screen.queryByRole("button", { name: "開始學習" })).toBeNull();
    expect(await screen.findByText("全書段落已整理好")).toBeTruthy();
  });

  test("a question is sent with the line being read and its answer shown above the box", async () => {
    const { calls } = setup({
      "POST /api/materials/m1/ask": () => ({
        json: { id: "rq1", unit_no: 1, question: "葡萄糖從哪來？", answer: "由**光合作用**產生。", label: "第 1–4 行", unit_ids: ["u1"], used_guide: true, created_at: "" },
      }),
    });
    const box = await screen.findByRole("textbox", { name: "向導師提問" });
    await userEvent.type(box, "葡萄糖從哪來？{Enter}");
    expect(await screen.findByText("產生。", { exact: false })).toBeTruthy();
    expect(screen.getByText("參考了這段的導讀", { exact: false })).toBeTruthy();
    expect(calls.find((c) => c.key === "POST /api/materials/m1/ask")?.body).toEqual({ at: 1, question: "葡萄糖從哪來？" });
    expect((box as HTMLTextAreaElement).value).toBe("");
  });

  test("an Enter that picks the IME's word (keyCode 229, as WebKit sends it) does not send the question", async () => {
    const { calls } = setup();
    const box = await screen.findByRole("textbox", { name: "向導師提問" });
    await userEvent.type(box, "葡萄糖");
    fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
    expect(calls.some((c) => c.key === "POST /api/materials/m1/ask")).toBe(false);
    expect((box as HTMLTextAreaElement).value).toBe("葡萄糖");
  });

  test("a failed question stays in the box with the reason", async () => {
    setup({ "POST /api/materials/m1/ask": () => ({ status: 503, json: { detail: "Ollama 連線失敗" } }) });
    const box = await screen.findByRole("textbox", { name: "向導師提問" });
    await userEvent.type(box, "這段在說什麼？{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Ollama 連線失敗");
    expect((box as HTMLTextAreaElement).value).toBe("這段在說什麼？");
  });

  test("導讀 and 出題 start the reading at the current position; 出題 asks for the questions", async () => {
    const { calls, opened } = setup({ "POST /api/materials/m1/learn": () => ({ json: session({ id: "s9" }) }) });
    await userEvent.click(await screen.findByRole("button", { name: /導讀/ }));
    await waitFor(() => expect(opened).toEqual([{ id: "s9", quiz: false }]));
    expect(calls.find((c) => c.key === "POST /api/materials/m1/learn")?.body).toEqual({ at: 1 });
    cleanup();
    const second = setup({ "POST /api/materials/m1/learn": () => ({ json: session({ id: "s9" }) }) });
    await userEvent.click(await screen.findByRole("button", { name: /出題/ }));
    await waitFor(() => expect(second.opened).toEqual([{ id: "s9", quiz: true }]));
  });

  test("the study's progress is shown while the companion reads", async () => {
    const reading = { ...study, status: "reading", progress: 0.42, parts: [] };
    setup({ "GET /api/materials/m1/study": () => ({ json: reading }), "POST /api/materials/m1/study/focus": () => ({ json: reading }) });
    expect(await screen.findByText("導師正在整理全書段落 · 42%")).toBeTruthy();
  });
});

describe("MaterialPage · study progress and zoom", () => {
  test("hovering the study shows the pass and the batch the companion is on", async () => {
    const reading = { ...study, status: "reading", progress: 0.12, parts: [], step: { phase: "outline", current: 3, total: 12, approx: false } };
    setup({ "GET /api/materials/m1/study": () => ({ json: reading }), "POST /api/materials/m1/study/focus": () => ({ json: reading }) });
    await userEvent.hover(await screen.findByText("導師正在整理全書段落 · 12%"));
    const card = await screen.findByRole("tooltip");
    expect(card).toHaveTextContent("逐段摘要");
    expect(card).toHaveTextContent("第 3 段，共 12 段");
  });

  test("the companion's reading is paused from the card, and resumed", async () => {
    const reading = { ...study, status: "reading", progress: 0.3, parts: [], step: { phase: "outline", current: 2, total: 9, approx: false } };
    const { calls } = setup({
      "GET /api/materials/m1/study": () => ({ json: reading }),
      "POST /api/materials/m1/study/focus": () => ({ json: reading }),
      "POST /api/materials/m1/study/pause": () => ({ json: { ...reading, paused: true, by_user: true } }),
    });
    await userEvent.hover(await screen.findByText("導師正在整理全書段落 · 30%"));
    await userEvent.click(await screen.findByRole("button", { name: "暫停" }));
    expect((await screen.findAllByText("已暫停全書分段 · 30%")).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "繼續" })).toBeTruthy();
    expect(calls.some((c) => c.key === "POST /api/materials/m1/study/pause")).toBe(true);
  });

  test("opening the book tells the server the companion may read it", async () => {
    const { calls } = setup();
    await screen.findByText("全書段落已整理好");
    expect(calls.some((c) => c.key === "POST /api/materials/m1/study/focus")).toBe(true);
  });

  test("ctrl + wheel (a trackpad pinch) is taken by the reader, not the window", async () => {
    setup();
    await screen.findByRole("button", { name: "將這頁加入書籤" });
    // the test DOM drops ctrlKey from a WheelEvent's init, so it is set on the event itself
    const e = new WheelEvent("wheel", { deltaY: -40, clientX: 400, clientY: 300, cancelable: true });
    Object.defineProperty(e, "ctrlKey", { value: true });
    window.dispatchEvent(e);
    expect(e.defaultPrevented).toBe(true);
  });
});

describe("MaterialPage · toolbar", () => {
  test("the bookmark button marks the line being read with the heading it falls under, and unmarks it", async () => {
    const marks: unknown[] = [];
    const { calls } = setup({
      "PUT /api/materials/m1/bookmarks/1": () => ({ json: [{ unit_no: 1, chapter: "光合作用", created_at: "" }] }),
      "DELETE /api/materials/m1/bookmarks/1": () => ({ json: [] }),
    });
    cleanup();
    let current: { unit_no: number; chapter: string | null; created_at: string }[] = [];
    const view = () => (
      <MaterialPage id="m1" library={{ categories: [], materials: [] }} onLibraryChange={async () => {}} onNavigate={() => {}} onOpenSession={() => {}}
        bookmarks={current} onBookmarks={(_, m) => { current = m; marks.push(m); rerender(view()); }} />
    );
    const { rerender } = render(view());
    await userEvent.click(await screen.findByRole("button", { name: "將這頁加入書籤" }));
    await waitFor(() => expect(calls.find((c) => c.key === "PUT /api/materials/m1/bookmarks/1")?.body).toEqual({ chapter: "光合作用" }));
    const on = await screen.findByRole("button", { name: "移除這頁的書籤" });
    expect(on.getAttribute("aria-pressed")).toBe("true");
    await userEvent.click(on);
    await waitFor(() => expect(marks.at(-1)).toEqual([]));
  });

  test("the zoom lives in the ⋯ card, not on the toolbar; the placeholder does not repeat the position", async () => {
    setup();
    const box = await screen.findByRole("textbox", { name: "向導師提問" });
    expect(box.getAttribute("placeholder")).toBe("對這段有疑問？");
    expect(screen.queryByRole("group", { name: "縮放" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "教材選項" }));
    expect(screen.getByRole("group", { name: "縮放" })).toHaveTextContent("100%");
  });
});

describe("MaterialPage · a book just imported", () => {
  test("while its text is read in the background, asking and 導讀 wait; they open once it is ready", async () => {
    let reads = 0;
    setup({ "GET /api/materials/m1": () => ({ json: { ...material, status: reads++ === 0 ? "extracting" : "ready" } }) });
    const box = await screen.findByRole("textbox", { name: "向導師提問" });
    expect(box.getAttribute("placeholder")).toBe("正在整理這本書的文字，完成後就能提問與導讀…");
    expect((box as HTMLTextAreaElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: /導讀/ }) as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect((box as HTMLTextAreaElement).disabled).toBe(false), { timeout: 4000 });
    expect(box.getAttribute("placeholder")).toBe("對這段有疑問？");
  });
});

describe("MaterialPage · English", () => {
  test("labels the box and actions in English", async () => {
    mockFetch({
      "GET /api/materials/m1": () => ({ json: material }),
      "GET /api/materials/m1/study": () => ({ json: study }),
      "POST /api/materials/m1/study/focus": () => ({ json: study }),
      "POST /api/materials/m1/study/leave": () => ({ json: study }),
      "GET /api/materials/m1/questions": () => ({ json: [] }),
    });
    render(
      <I18nContext.Provider value={{ locale: "en", t: (key, vars) => translate("en", key, vars) }}>
        <MaterialPage id="m1" library={{ categories: [], materials: [] }} onLibraryChange={async () => {}} onNavigate={() => {}} onOpenSession={() => {}} />
      </I18nContext.Provider>,
    );
    expect(await screen.findByPlaceholderText("Questions about this passage?")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Guide me/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Quiz me/ })).toBeTruthy();
  });
});

describe("MaterialPage · 閱讀模式", () => {
  const quotes = [
    { id: "quote-01", index: 0, label: "第 1 行", heading: "光合作用", excerpt: "植物 … 葡萄糖。", text: "植物利用陽光製造葡萄糖。", chars: 12, part: 0, unit_id: "u1" },
    { id: "quote-02", index: 1, label: "第 4 行", heading: "葉綠體", excerpt: "葉綠體 … 地方。", text: "葉綠體是進行光合作用的地方。", chars: 14, part: 0, unit_id: "u4" },
  ];
  const guided = session({
    id: "s9",
    source: { material_id: "m1", material_title: "光合作用", unit: "line", start: 1, end: 4, label: "第 1–4 行", text: "…" },
    reading: {
      index: 1, total: 2, done: false, quotes, parts: [{ index: 0, label: "第 1–4 行", quote_from: 0, quote_to: 1, chars: 26 }],
      guides: { "0": { chinese_text: null, guide: "原料是水與二氧化碳。", topic_summary: "本主題探討植物如何製造養分。接著談葉綠體。", key_points: [], check: "" } },
      qa: {}, compacts: {}, quiz_source: null,
    },
    concepts: [],
    current_question: null,
    state: { concept_order: [], concept_index: 0, phase: "reading", concepts: {} },
  });

  test("導讀 switches the pages for the guides in place; the TOC lists its parts; pressed again it switches back", async () => {
    let load = () => {};
    const loaded = new Promise<void>((r) => (load = r));
    mockFetch({
      "GET /api/materials/m1": () => ({ json: material }),
      "GET /api/materials/m1/study": () => ({ json: study }),
      "POST /api/materials/m1/study/focus": () => ({ json: study }),
      "POST /api/materials/m1/study/leave": () => ({ json: study }),
      "GET /api/materials/m1/questions": () => ({ json: [] }),
      "GET /api/sessions/s9": () => loaded.then(() => ({ json: guided })),
      "GET /api/labels/part": () => ({ json: { "m1:u4": "葉綠體在哪裡" } }),
    });
    const went: unknown[] = [];
    render(<MaterialPage id="m1" view={{ kind: "guide", session: "s9" }} library={{ categories: [], materials: [] }} onLibraryChange={async () => {}} onNavigate={(r) => went.push(r)} onOpenSession={() => {}} />);
    // until the reading is in, the pages stay and the button shows it is working, keeping its label
    const working = await screen.findByRole("button", { name: /導讀/, busy: true });
    expect(working.className).toContain("filling");
    expect(Number(working.style.getPropertyValue("--fill"))).toBeLessThan(1);
    expect(working.textContent).toBe("導讀");
    expect(document.querySelector(".reader-body.off")).toBeNull();
    load();
    // prepared: the fill runs to the end, and the mode takes the screen no sooner than half a second after the press
    await waitFor(() => expect(working.style.getPropertyValue("--fill")).toBe("1"));
    expect(document.querySelector(".reader-body.off")).toBeNull();
    await waitFor(() => expect(document.querySelector(".reader-body.off")).toBeTruthy());
    expect(await screen.findByText("原料是水與二氧化碳。")).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("button", { name: /導讀/, pressed: true }).className).toContain("on"));
    expect(screen.getByRole("button", { name: /導讀/, pressed: true }).className).not.toContain("filling");
    // the bar and the box stay; the pages are kept underneath, out of sight, and the range is not named again
    expect(screen.getByRole("textbox", { name: "向導師提問" })).toBeTruthy();
    expect(document.querySelector(".reader-body.off")).toBeTruthy();
    expect(screen.queryByText(/第 2 段，共 2 段/)).toBeNull();
    const guide = screen.getByRole("button", { name: /導讀/, pressed: true });
    await userEvent.click(screen.getByRole("button", { name: "導讀段落" }));
    const items = screen.getAllByRole("menuitem");
    expect(items.map((x) => x.textContent)).toEqual(["1植物如何製造養分", "2葉綠體在哪裡"]);
    await userEvent.click(guide);
    expect(went).toEqual([{ page: "material", id: "m1" }]);
  });
});
