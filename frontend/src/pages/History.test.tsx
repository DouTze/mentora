import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HistoryPage } from "./History";
import { mockFetch, session } from "../test/fixtures";
import type { SessionListItem } from "../types";

afterEach(cleanup);

const item = (id: string, title: string): SessionListItem => ({
  id, status: "completed", created_at: "2026-10-05T10:00:00", updated_at: "", content_title: title, content_version: "1",
  strategy_id: "rules-v1", source: null, backend: "mock", simulated: true, verdict: "passed", progress: { stage: "quiz", read: 0, total: 0 },
});

describe("HistoryPage", () => {
  test("a record is deleted only after confirming, and the list reloads", async () => {
    let list = [item("s1", "光合作用"), item("s2", "呼吸作用")];
    const calls = mockFetch({
      "GET /api/sessions": () => ({ json: list }),
      "DELETE /api/sessions/s1": () => {
        list = list.filter((s) => s.id !== "s1");
        return { json: { ok: true } };
      },
    });
    render(<HistoryPage onOpen={() => {}} />);
    await screen.findByText("光合作用");

    // 刪除 sits in the record's ⋯ menu
    await userEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    await userEvent.click(screen.getByRole("menuitem", { name: "刪除" }));
    await userEvent.click(screen.getByRole("button", { name: "取消" })); // cancelling keeps it
    expect(calls.some((c) => c.key.startsWith("DELETE"))).toBe(false);

    await userEvent.click(screen.getAllByRole("button", { name: "更多操作" })[0]);
    await userEvent.click(screen.getByRole("menuitem", { name: "刪除" }));
    expect(screen.getByText("刪除後無法復原")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "確定刪除" }));
    await waitFor(() => expect(screen.queryByText("光合作用")).toBeNull());
    expect(screen.getByText("呼吸作用")).toBeTruthy();
    expect(screen.getByText("已刪除這筆學習紀錄")).toBeTruthy();
  });

  test("shows the backend's reason when the record is busy", async () => {
    mockFetch({
      "GET /api/sessions": () => ({ json: [item("s1", "光合作用")] }),
      "DELETE /api/sessions/s1": () => ({ status: 409, json: { code: "busy", detail: "這段學習還有操作正在進行，請等它完成後再刪除" } }),
    });
    render(<HistoryPage onOpen={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "更多操作" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "刪除" }));
    await userEvent.click(screen.getByRole("button", { name: "確定刪除" }));
    expect(await screen.findByText("這段學習還有操作正在進行，請等它完成後再刪除")).toBeTruthy();
    expect(screen.getByText("光合作用")).toBeTruthy();
  });

  test("records are grouped by day and named without repeating the book; one opens in place of the list and a back link returns", async () => {
    const tour = { ...item("w1", "Reinforcement Learning 第 134–138 頁"), status: "active", verdict: null, tour: true, progress: { stage: "reading" as const, read: 3, total: 6 },
      source: { material_id: "m1", material_title: "Reinforcement Learning", unit: "page" as const, start: 134, end: 138, label: "第 134–138 頁" } };
    mockFetch({
      "GET /api/sessions": () => ({ json: [tour, item("s1", "光合作用")] }),
      "GET /api/sessions/w1": () => ({ json: session({ id: "w1", content_title: tour.content_title, source: { ...tour.source, text: "" }, workflow: { waiting: "tour" } as never }) }),
    });
    const opened: string[] = [];
    render(<HistoryPage onOpen={(id) => opened.push(id)} />);
    expect(await screen.findByText("第 134–138 頁")).toBeTruthy();
    expect(screen.getByText("3/6")).toBeTruthy();
    await userEvent.click(screen.getAllByRole("button", { name: "繼續" })[0]);
    expect(opened).toEqual(["w1"]);
    await userEvent.click(screen.getByText("第 134–138 頁"));
    expect(await screen.findByRole("heading", { level: 1, name: "第 134–138 頁" })).toBeTruthy();
    expect(screen.getByText("技術細節")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "學習紀錄" }));
    expect(await screen.findByText("光合作用")).toBeTruthy();
  });
});
