import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HistoryPage } from "./History";
import { mockFetch } from "../test/fixtures";
import type { SessionListItem } from "../types";

afterEach(cleanup);

const item = (id: string, title: string): SessionListItem => ({
  id, status: "completed", created_at: "2026-10-05T10:00:00", updated_at: "", content_title: title, content_version: "1",
  strategy_id: "rules-v1", source: null, backend: "mock", simulated: true, verdict: "passed",
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

    await userEvent.click(screen.getAllByRole("button", { name: "刪除" })[0]);
    await userEvent.click(screen.getByRole("button", { name: "取消" })); // cancelling keeps it
    expect(calls.some((c) => c.key.startsWith("DELETE"))).toBe(false);

    await userEvent.click(screen.getAllByRole("button", { name: "刪除" })[0]);
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
    await userEvent.click(await screen.findByRole("button", { name: "刪除" }));
    await userEvent.click(screen.getByRole("button", { name: "確定刪除" }));
    expect(await screen.findByText("這段學習還有操作正在進行，請等它完成後再刪除")).toBeTruthy();
    expect(screen.getByText("光合作用")).toBeTruthy();
  });
});
