import { afterEach, describe, expect, test } from "bun:test";
import { cleanup, render } from "@testing-library/react";
import { RichText } from "./RichText";

afterEach(cleanup);

describe("RichText", () => {
  test("renders inline and display KaTeX", () => {
    const { container } = render(<RichText text={"能量 $E = mc^2$ 守恆。\n\n$$\n\\int_0^1 x\\,dx = \\tfrac12\n$$"} />);
    const katex = container.querySelectorAll(".katex");
    expect(katex.length).toBe(2);
    expect(container.querySelector(".math-display")).toBeTruthy();
    expect(container.textContent).toContain("能量");
  });

  test("renders the markdown subset without letting HTML through", () => {
    const { container } = render(<RichText text={"**粗體** 與 `code` <b>x</b>\n\n- 一\n- 二\n\n1. 甲\n2. 乙\n\n| a | b |\n|---|---|\n| 1 | 2 |"} />);
    expect(container.querySelector("strong")?.textContent).toBe("粗體");
    expect(container.querySelector("code")?.textContent).toBe("code");
    expect(container.querySelector("b")).toBeNull(); // raw HTML stays text
    expect(container.textContent).toContain("<b>x</b>");
    expect(container.querySelectorAll("ul li").length).toBe(2);
    expect(container.querySelectorAll("ol li").length).toBe(2);
    expect(container.querySelectorAll("td").length).toBe(2);
  });

  test("leaves a lone dollar sign alone", () => {
    const { container } = render(<RichText text="價格是 $5 而已" />);
    expect(container.querySelector(".katex")).toBeNull();
    expect(container.textContent).toBe("價格是 $5 而已");
  });
});
