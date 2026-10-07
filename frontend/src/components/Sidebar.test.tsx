import { afterEach, describe, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Sidebar } from "./Sidebar";
import { mockFetch } from "../test/fixtures";
import type { Library } from "../types";
import type { Route } from "../route";

const library: Library = {
  categories: [{ id: "c1", name: "生物", position: 1 }, { id: "c3", name: "化學", position: 2 }],
  materials: [
    { id: "m1", category_id: "c1", title: "光合作用", filename: "光合作用.md", kind: "md", unit: "line", unit_count: 9, created_at: "" },
    { id: "m2", category_id: null, title: "Newton laws", filename: "Newton laws.pdf", kind: "pdf", unit: "page", unit_count: 3, created_at: "" },
  ],
};

function setup(over: Partial<Parameters<typeof Sidebar>[0]> = {}) {
  const visited: Route[] = [];
  let toggled = 0;
  let settings = 0;
  const props = {
    library, route: { page: "home" } as Route, companion: "owl" as const, conn: { status: "ok" as const, active: { backend: "ollama", model: "qwen3-vl:4b", simulated: false, host: "http://127.0.0.1:11434" }, detail: "" },
    hasSession: false, collapsed: false,
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

  test("the companion at the bottom opens a menu with the connection and the way into settings", async () => {
    const a = setup();
    await userEvent.click(screen.getByRole("button", { name: /Moyu/ }));
    const menu = screen.getByRole("menu").textContent;
    expect(menu).toContain("已連線");
    expect(menu).toContain("qwen3-vl:4b");
    expect(menu).toContain("本地 · Ollama");
    await userEvent.click(screen.getByRole("menuitem", { name: "設定" }));
    expect(a.settings()).toBe(1);
    expect(screen.queryByRole("menu")).toBeNull();
    cleanup();
    const b = setup({ collapsed: true });
    await userEvent.click(screen.getByRole("button", { name: /Moyu/ }));
    await userEvent.click(screen.getByRole("menuitem", { name: "設定" }));
    expect(b.settings()).toBe(1);
  });

  test("a remote model that cannot be reached is described as such", async () => {
    setup({ conn: { status: "bad", active: { backend: "remote", model: "qwen3-vl:4b", simulated: false }, detail: "timeout" } });
    await userEvent.click(screen.getByRole("button", { name: /Moyu/ }));
    const menu = screen.getByRole("menu").textContent;
    expect(menu).toContain("無法連線");
    expect(menu).toContain("遠端 · Remote");
    expect(menu).toContain("伺服器沒有及時回應");   // the raw "timeout" is put in words; it stays in the tooltip
    expect(menu).not.toContain("timeout");
  });

  test("categories move up and down from their menu, and deleting asks once more", async () => {
    const calls = mockFetch({
      "PUT /api/categories/order": () => ({ json: [] }),
      "DELETE /api/categories/c3": () => ({ json: { ok: true } }),
    });
    setup();
    await userEvent.click(screen.getByRole("button", { name: "化學的更多操作" }));
    expect((screen.getByRole("menuitem", { name: "下移" }) as HTMLButtonElement).disabled).toBe(true);
    await userEvent.click(screen.getByRole("menuitem", { name: "上移" }));
    await waitFor(() => expect(calls.find((c) => c.key === "PUT /api/categories/order")?.body).toEqual({ ids: ["c3", "c1"] }));

    await userEvent.click(screen.getByRole("button", { name: "化學的更多操作" }));
    await userEvent.click(screen.getByRole("menuitem", { name: /刪除分類/ }));
    expect(calls.some((c) => c.key.startsWith("DELETE"))).toBe(false);
    await userEvent.click(screen.getByRole("menuitem", { name: /確定刪除/ }));
    await waitFor(() => expect(calls.some((c) => c.key === "DELETE /api/categories/c3")).toBe(true));
  });

  test("a material dragged onto a category moves there; the hand closes on press and the label follows", async () => {
    const calls = mockFetch({ "PATCH /api/materials/m2": () => ({ json: { ...library.materials[1], category_id: "c1" } }) });
    setup();
    const target = screen.getByRole("button", { name: /^生物\s*1$/ }).closest("[data-drop-key]")!;
    const original = document.elementFromPoint;
    document.elementFromPoint = () => target;
    try {
      const leaf = screen.getByText("Newton laws").closest("button")!;
      fireEvent.pointerDown(leaf, { button: 0, clientX: 10, clientY: 10, pointerType: "mouse" });
      expect(document.documentElement.classList.contains("grabbing")).toBe(true);   // pressed, not yet moved
      act(() => {
        document.dispatchEvent(new MouseEvent("pointermove", { clientX: 80, clientY: 40 }));
      });
      expect(document.querySelector(".drag-ghost")?.textContent).toContain("Newton laws");
      expect(target.classList.contains("drop")).toBe(true);
      act(() => {
        document.dispatchEvent(new MouseEvent("pointerup", { clientX: 80, clientY: 40 }));
      });
      await waitFor(() => expect(calls.find((c) => c.key === "PATCH /api/materials/m2")?.body).toEqual({ category_id: "c1" }));
      expect(document.documentElement.classList.contains("grabbing")).toBe(false);
      expect(document.querySelector(".drag-ghost")).toBeNull();
    } finally {
      document.elementFromPoint = original;
    }
  });

  test("a press without moving is still a click", async () => {
    const { visited } = setup();
    const leaf = screen.getByText("Newton laws").closest("button")!;
    fireEvent.pointerDown(leaf, { button: 0, clientX: 10, clientY: 10, pointerType: "mouse" });
    act(() => {
      document.dispatchEvent(new MouseEvent("pointerup", { clientX: 11, clientY: 10 }));
    });
    await userEvent.click(leaf);
    expect(visited).toEqual([{ page: "material", id: "m2" }]);
    expect(document.documentElement.classList.contains("grabbing")).toBe(false);
  });
});

describe("Sidebar · folded", () => {
  test("hovering the rail floats the whole sidebar; leaving folds it again", async () => {
    mockFetch({});
    render(
      <Sidebar library={{ categories: [], materials: [] }} route={{ page: "home" }} companion="owl" conn={{ status: "idle", active: null, detail: "" }} hasSession={false}
        collapsed onToggle={() => {}} onNavigate={() => {}} onLibraryChange={async () => {}} onOpenSettings={() => {}} />,
    );
    expect(document.querySelector(".sidebar.peek")).toBeNull();
    await userEvent.hover(document.querySelector(".sidebar.rail")!);
    await waitFor(() => expect(document.querySelector(".sidebar.peek")).not.toBeNull());
    expect(screen.getByText("Mentor")).toBeTruthy();
    fireEvent.mouseLeave(document.querySelector(".sidebar.peek")!);
    await waitFor(() => expect(document.querySelector(".sidebar.peek.leaving")).not.toBeNull());   // slides away first
    await waitFor(() => expect(document.querySelector(".sidebar.peek")).toBeNull(), { timeout: 2000 });
  });
});
