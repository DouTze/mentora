import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { cleanup, fireEvent, render, within, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MaterialPage } from "./Material";
import { mockFetch, session } from "../test/fixtures";
import { I18nContext, translate } from "../i18n";

const material = { id: "m1", category_id: null, title: "光合作用", filename: "光合作用.md", kind: "md", unit: "line", unit_count: 4, created_at: "", translate_zh: false, units: ["# 光合作用", "", "植物利用陽光製造葡萄糖。", "葉綠體是進行光合作用的地方。"] };
const topicA = { index: 0, unit_id: "u1", label: "第 1–3 行", heading: "光合作用", from: 1, to: 3, excerpt: "植物利用陽光…", blocks: 1, state: "done", error: null,
  guide: { guide: "", key_points: [], check: "葉綠體在哪裡？", topic_summary: "本主題探討植物如何製造養分。", blocks: [{ index: 0, source: "植物利用陽光製造葡萄糖。", status: "accepted", guide: "原料是水與二氧化碳。" }] } };
const topicB = { index: 1, unit_id: "u4", label: "第 4 行", heading: "葉綠體", from: 4, to: 4, excerpt: "葉綠體是…", blocks: 1, state: "queued", error: null, guide: null };
const tourView = (topics: unknown[] = [topicA, topicB]) => ({
  material_id: "m1", active: true, focus: { session_id: "w1", index: null, at: 1 }, creating: null, first: true, last: true,
  windows: [{ session_id: "w1", start: 1, end: 4, label: "第 1–4 行", status: "ready", state: "done", error: null, topics, skipped: [] }],
});

beforeEach(() => localStorage.clear());
afterEach(cleanup);

function setup(routes: Parameters<typeof mockFetch>[0] = {}) {
  const calls = mockFetch({
    "GET /api/materials/m1": () => ({ json: material }),
    "GET /api/materials/m1/questions": () => ({ json: [] }),
    ...routes,
  });
  const opened: { id: string; quiz?: boolean }[] = [];
  const went: unknown[] = [];
  render(<MaterialPage id="m1" library={{ categories: [], materials: [] }} onLibraryChange={async () => {}} onNavigate={(r) => went.push(r)} onOpenSession={(id, o) => opened.push({ id, quiz: o?.quiz })} />);
  return { calls, opened, went };
}

describe("MaterialPage · reader", () => {
  test("sets Markdown as an article, without the parsed-text unit list or the range tool", async () => {
    setup();
    expect(await screen.findByRole("heading", { name: "光合作用" })).toBeTruthy();
    expect(screen.getByText("植物利用陽光製造葡萄糖。 葉綠體是進行光合作用的地方。".split(" ")[0], { exact: false })).toBeTruthy();
    expect(screen.queryByTestId("unit")).toBeNull();
    expect(screen.queryByRole("button", { name: "開始學習" })).toBeNull();
  });

  test("opening a book no longer segments the whole of it: nothing is asked of the companion's study", async () => {
    const { calls } = setup();
    await screen.findByRole("textbox", { name: "向導師提問" });
    await new Promise((r) => setTimeout(r, 50));
    expect(calls.some((c) => c.key.includes("/study"))).toBe(false);
    expect(screen.queryByText(/全書段落/)).toBeNull();
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

  test("the book takes the guide language of 設定 › 導讀 (中文 by default here), so new readings are written in it", async () => {
    const { calls } = setup({ "PATCH /api/materials/m1": (b) => ({ json: { ...material, ...(b as object) } }) });
    await waitFor(() => expect(calls.find((c) => c.key === "PATCH /api/materials/m1")?.body).toEqual({ translate_zh: true }));
  });

  test("the reader's ⋯ no longer carries the guide language", async () => {
    setup();
    await userEvent.click(await screen.findByRole("button", { name: "教材選項" }));
    expect(screen.getByRole("dialog", { name: "教材選項" })).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
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

  test("導讀 opens the 導讀 mode at the line being read, with the pages around it (設定 › 導讀; 2 by default) and no range to choose; 出題 starts at the current position", async () => {
    const { calls, went } = setup({ "POST /api/materials/m1/tour/open": () => ({ json: tourView() }) });
    await userEvent.click(await screen.findByRole("button", { name: /^導讀$/ }));
    await waitFor(() => expect(went).toEqual([{ page: "material", id: "m1", view: { kind: "tour", session: "w1", at: 1 } }]));
    expect(calls.find((c) => c.key === "POST /api/materials/m1/tour/open")?.body).toEqual({ at: 1, span: 2 });
    expect(screen.queryByRole("group", { name: "導讀範圍" })).toBeNull();
    cleanup();
    const second = setup({ "POST /api/materials/m1/learn": () => ({ json: session({ id: "s9" }) }) });
    await userEvent.click(await screen.findByRole("button", { name: /出題/ }));
    await waitFor(() => expect(second.opened).toEqual([{ id: "s9", quiz: true }]));
  });

  test("導讀 pressed again while the mode is prepared stops preparing it", async () => {
    let answer = () => {};
    const { calls, went } = setup({ "POST /api/materials/m1/tour/open": () => new Promise((r) => (answer = () => r({ json: tourView() }))), "POST /api/materials/m1/tour/pause": () => ({ json: tourView() }) });
    const guide = await screen.findByRole("button", { name: /^導讀$/ });
    await userEvent.click(guide);
    await waitFor(() => expect(guide.className).toContain("filling"));
    await userEvent.click(guide);
    expect(guide.className).not.toContain("filling");
    answer();
    await new Promise((r) => setTimeout(r, 30));
    expect(went).toEqual([]);
    expect(calls.some((c) => c.key === "POST /api/materials/m1/tour/pause")).toBe(true);
  });
});

describe("MaterialPage · zoom", () => {
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

  test("a 導讀 from before the 導讀 mode, still being read, opens the 導讀 mode at its first page instead", async () => {
    const calls = mockFetch({
      "GET /api/materials/m1": () => ({ json: material }),
      "GET /api/materials/m1/questions": () => ({ json: [] }),
      "GET /api/sessions/s9": () => ({ json: guided }),
      "POST /api/materials/m1/tour/open": () => ({ json: tourView() }),
    });
    const went: unknown[] = [];
    render(<MaterialPage id="m1" view={{ kind: "guide", session: "s9" }} library={{ categories: [], materials: [] }} onLibraryChange={async () => {}} onNavigate={(r) => went.push(r)} onOpenSession={() => {}} />);
    await waitFor(() => expect(went).toEqual([{ page: "material", id: "m1", view: { kind: "tour", session: "w1", at: 1 } }]));
    expect(calls.find((c) => c.key === "POST /api/materials/m1/tour/open")?.body).toEqual({ at: 1, span: 2 });
    // the old reading panel, with its own box and 下一段, never shows
    expect(screen.queryByRole("button", { name: /下一段/ })).toBeNull();
  });
});

describe("MaterialPage · 導讀模式", () => {
  test("the mode takes the screen once the 主題 of the page is written; the others follow in the column; the TOC lists them; pressed again it fades out and stops the work", async () => {
    let write = () => {};
    const written = new Promise<void>((r) => (write = r));
    const calls = mockFetch({
      "GET /api/materials/m1": () => ({ json: material }),
      "GET /api/materials/m1/questions": () => ({ json: [] }),
      "POST /api/materials/m1/tour/focus": () => written.then(() => ({ json: tourView() })),
      "GET /api/materials/m1/tour": () => written.then(() => ({ json: tourView() })),
      "POST /api/materials/m1/tour/pause": () => ({ json: { ...tourView(), active: false } }),
      "GET /api/labels/part": () => ({ json: {} }),
    });
    const went: unknown[] = [];
    const page = (view: { kind: "tour"; session: string } | null) => (
      <MaterialPage id="m1" view={view} library={{ categories: [], materials: [] }} onLibraryChange={async () => {}} onNavigate={(r) => went.push(r)} onOpenSession={() => {}} />
    );
    const { rerender } = render(page({ kind: "tour", session: "w1" }));
    const working = await screen.findByRole("button", { name: /導讀/, busy: true });
    expect(working.className).toContain("filling");
    expect(!!document.querySelector(".tour-layer.loading")).toBe(true);
    write();
    await waitFor(() => expect(!!document.querySelector(".tour-layer:not(.loading)")).toBe(true), { timeout: 3000 });
    expect(screen.getByText("原料是水與二氧化碳。")).toBeTruthy();
    expect(screen.getByText("排隊中，稍後撰寫導讀")).toBeTruthy();
    // no question box or buttons of its own: the box below asks about the 主題 on screen
    expect(screen.queryByRole("button", { name: /下一段/ })).toBeNull();
    expect(!!document.querySelector(".tour-layer textarea, .tour-layer input")).toBe(false);
    await waitFor(() => expect(!!document.querySelector(".reader-body.off")).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "導讀段落" }));
    expect(screen.getAllByRole("menuitem").map((x) => x.textContent)).toEqual(["1植物如何製造養分", "2葉綠體"]);
    await userEvent.click(screen.getByRole("button", { name: /導讀/, pressed: true }));
    expect(went).toEqual([{ page: "material", id: "m1" }]);
    rerender(page(null));
    // the pages come back at once, the mode fades out over them, and the work stops
    expect(!!document.querySelector(".reader-body.off")).toBe(false);
    expect(!!document.querySelector(".tour-layer.leaving")).toBe(true);
    await waitFor(() => expect(!!document.querySelector(".tour-layer")).toBe(false));
    expect(calls.some((c) => c.key === "POST /api/materials/m1/tour/pause")).toBe(true);
  });

  test("a 主題 that could not be written says so and is tried again on request", async () => {
    const failed = { ...topicB, state: "failed", error: { code: "model_unavailable", detail: "Ollama 連線失敗" } };
    const calls = mockFetch({
      "GET /api/materials/m1": () => ({ json: material }),
      "GET /api/materials/m1/questions": () => ({ json: [] }),
      "POST /api/materials/m1/tour/focus": () => ({ json: tourView([topicA, failed]) }),
      "GET /api/materials/m1/tour": () => ({ json: tourView([topicA, failed]) }),
      "POST /api/materials/m1/tour/retry": () => ({ json: tourView([topicA, { ...topicB, state: "working" }]) }),
      "POST /api/materials/m1/tour/pause": () => ({ json: tourView() }),
    });
    render(<MaterialPage id="m1" view={{ kind: "tour", session: "w1" }} library={{ categories: [], materials: [] }} onLibraryChange={async () => {}} onNavigate={() => {}} onOpenSession={() => {}} />);
    expect(await screen.findByText("這段導讀沒有完成：Ollama 連線失敗")).toBeTruthy();
    await userEvent.click(await screen.findByRole("button", { name: "重試" }, { timeout: 3000 }));
    expect(await screen.findByText("正在撰寫導讀…")).toBeTruthy();
    expect(calls.find((c) => c.key === "POST /api/materials/m1/tour/retry")?.body).toEqual({ session_id: "w1", index: 1 });
  });
});
