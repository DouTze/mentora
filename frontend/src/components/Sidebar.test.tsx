import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Sidebar } from "./Sidebar";
import { mockFetch } from "../test/fixtures";
import type { Library } from "../types";
import type { Route } from "../route";

const library: Library = {
  categories: [{ id: "c1", name: "生物", position: 1 }],
  materials: [
    { id: "m1", category_id: "c1", title: "光合作用", filename: "光合作用.md", kind: "md", unit: "line", unit_count: 9, created_at: "", translate_zh: false },
    { id: "m2", category_id: null, title: "Newton laws", filename: "Newton laws.pdf", kind: "pdf", unit: "page", unit_count: 3, created_at: "", translate_zh: false },
  ],
};

function setup(over: Partial<Parameters<typeof Sidebar>[0]> = {}) {
  const visited: Route[] = [];
  let toggled = 0;
  let settings = 0;
  const props = {
    library, route: { page: "home" } as Route, active: null, hasSession: false, collapsed: false,
    onToggle: () => toggled++, onNavigate: (r: Route) => visited.push(r), onLibraryChange: async () => {}, onOpenSettings: () => settings++, ...over,
  };
  render(<Sidebar {...props} />);
  return { visited, toggled: () => toggled, settings: () => settings };
}

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("Sidebar", () => {
  test("groups materials by category and folds a category out of view", async () => {
    const { visited } = setup();
    await userEvent.click(screen.getByText("光合作用"));
    expect(visited).toEqual([{ page: "material", id: "m1" }]);
    expect(screen.getByText("Newton laws")).toBeTruthy(); // under 未分類

    await userEvent.click(screen.getByRole("button", { name: /^生物\s*1$/ }));
    expect(screen.queryByText("光合作用")).toBeNull();
    expect(JSON.parse(localStorage.getItem("mentor.foldedCategories")!)).toEqual(["c1"]); // stays folded after reload
  });

  test("collapsed rail keeps navigation and the expand control", async () => {
    const { toggled } = setup({ collapsed: true });
    expect(screen.queryByText("生物")).toBeNull();
    expect(screen.getByRole("button", { name: "學習紀錄" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "展開側邊欄" }));
    expect(toggled()).toBe(1);
  });

  test("creates a category from the inline field", async () => {
    const calls = mockFetch({ "POST /api/categories": () => ({ json: { id: "c2", name: "物理", position: 2 } }) });
    setup();
    await userEvent.click(screen.getByRole("button", { name: "新增分類" }));
    await userEvent.type(screen.getByLabelText("分類名稱"), "物理{Enter}");
    await waitFor(() => expect(calls[0]?.body).toEqual({ name: "物理" }));
  });

  test("the gear at the bottom opens settings, expanded or collapsed", async () => {
    const a = setup();
    await userEvent.click(screen.getByRole("button", { name: "設定" }));
    expect(a.settings()).toBe(1);
    cleanup();
    const b = setup({ collapsed: true });
    await userEvent.click(screen.getByRole("button", { name: "設定" }));
    expect(b.settings()).toBe(1);
  });
});
