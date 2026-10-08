import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MaterialPage } from "./Material";
import { mockFetch, session } from "../test/fixtures";

const material = { id: "m1", category_id: null, title: "光合作用", filename: "光合作用.md", kind: "md", unit: "line", unit_count: 4, created_at: "", translate_zh: false, units: ["# 光合作用", "", "植物利用陽光製造葡萄糖。", "葉綠體是進行光合作用的地方。"] };

afterEach(cleanup);

function setup(routes: Parameters<typeof mockFetch>[0] = {}) {
  const calls = mockFetch({ "GET /api/materials/m1": () => ({ json: material }), "GET /api/sessions": () => ({ json: [] }), ...routes });
  let opened = "";
  render(<MaterialPage id="m1" library={{ categories: [], materials: [] }} onLibraryChange={async () => {}} onNavigate={() => {}} onOpenSession={(id) => (opened = id)} />);
  return { calls, opened: () => opened };
}

describe("MaterialPage", () => {
  test("two clicks mark a line range, and learning starts on exactly that range", async () => {
    const { calls, opened } = setup({ "POST /api/sessions": () => ({ json: session({ id: "s9" }) }) });
    const units = await screen.findAllByTestId("unit");
    expect(screen.getByRole("button", { name: "開始學習" })).toHaveProperty("disabled", true); // nothing selected yet

    await userEvent.click(units[0]);
    await userEvent.click(units[2]);
    expect(screen.getByTestId("range-summary")).toHaveTextContent("第 1–3 行");
    expect(units.map((u) => u.className.includes("selected"))).toEqual([true, true, true, false]);

    await userEvent.click(screen.getByRole("button", { name: "開始學習" }));
    await waitFor(() => expect(opened()).toBe("s9"));
    expect(calls.find((c) => c.key === "POST /api/sessions")?.body).toEqual({ material_id: "m1", start: 1, end: 3, strategy_id: "rules-v1" });
  });

  test("shows the backend's reason when questions cannot be generated", async () => {
    setup({ "POST /api/sessions": () => ({ status: 502, json: { code: "model_unavailable", detail: "Ollama 連線失敗", retryable: true } }) });
    await userEvent.click((await screen.findAllByTestId("unit"))[3]);
    await userEvent.click(screen.getByRole("button", { name: "開始學習" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Ollama 連線失敗");
    expect(screen.getByTestId("range-summary")).toHaveTextContent("第 4 行"); // selection kept for retry
  });
});

describe("MaterialPage · 範圍輸入", () => {
  test("typing a smaller end value does not drag the start along until the field is committed", async () => {
    setup();
    const units = await screen.findAllByTestId("unit");
    await userEvent.click(units[1]);
    await userEvent.click(units[3]);
    expect(screen.getByTestId("range-summary")).toHaveTextContent("第 2–4 行");
    const end = screen.getByLabelText("結束行") as HTMLInputElement;
    await userEvent.clear(end);
    await userEvent.type(end, "1"); // half-typed: start must stay 2
    expect(screen.getByTestId("range-summary")).toHaveTextContent("第 2–4 行");
    await userEvent.type(end, "{Enter}"); // committed as 1 → the two ends are simply sorted
    expect(screen.getByTestId("range-summary")).toHaveTextContent("第 1–2 行");
    const start = screen.getByLabelText("起始行") as HTMLInputElement;
    await userEvent.clear(start);
    await userEvent.type(start, "3");
    await userEvent.tab(); // blur commits; 3 > end 2 → sorted to 2–3
    expect(screen.getByTestId("range-summary")).toHaveTextContent("第 2–3 行");
  });
});
