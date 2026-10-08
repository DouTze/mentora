import { afterEach, describe, expect, test } from "bun:test";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Toaster } from "./components/Toaster";
import { translate } from "./i18n";
import { importFiles } from "./library";

afterEach(cleanup);

const t = ((key: string, vars?: Record<string, string | number>) => translate("zh-Hant", key as never, vars)) as never;
const book = (id: string, title: string) => ({ id, title, category_id: null, filename: `${title}.pdf`, kind: "pdf", unit: "page", unit_count: 3, created_at: "" });

describe("importing into the library", () => {
  test("a file already in the library is named in a notice with a way to open it, and the next file goes on", async () => {
    const answers = [
      { status: 409, json: { code: "duplicate", detail: "dup", retryable: false, material: book("m1", "線性代數") } },
      { status: 200, json: book("m2", "機率論") },
    ];
    globalThis.fetch = (async () => {
      const a = answers.shift()!;
      return new Response(JSON.stringify(a.json), { status: a.status, headers: { "content-type": "application/json" } });
    }) as unknown as typeof fetch;
    render(<Toaster />);
    const opened: string[] = [];
    let last: { id: string } | null = null;
    await act(async () => {
      last = await importFiles([new File(["a"], "a.pdf"), new File(["b"], "b.pdf")], null, t, (id) => opened.push(id));
    });
    expect(last!.id).toBe("m2");
    expect(await screen.findByText("「線性代數」已經在書庫裡，沒有重複匯入。")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "開啟" }));
    expect(opened).toEqual(["m1"]);
    expect(screen.queryByText(/已經在書庫裡/)).toBeNull();          // the notice closes once acted on
  });
});
