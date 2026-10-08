import { afterEach, describe, expect, mock, test } from "bun:test";
import { cleanup, fireEvent, render } from "@testing-library/react";
import type { PdfPageLayer } from "../types";
import { PdfLayer } from "./PdfLayer";

afterEach(cleanup);

const layer: PdfPageLayer = {
  page: 1,
  label: "i",
  size: [300, 400],
  quarter: 0,
  lines: [
    { block: 0, runs: [{ t: "Hello", rect: [10, 10, 40, 21] }, { t: "world", rect: [45, 10, 75, 21], sp: true }] },
    { block: 0, runs: [{ t: "Next", rect: [10, 30, 34, 41] }] },
  ],
  links: [
    { rect: [10, 10, 40, 21], kind: "page", page: 3, y: 120 },
    { rect: [45, 10, 75, 21], kind: "uri", uri: "https://example.org" },
    { rect: [10, 30, 34, 41], kind: "uri", uri: "javascript:alert(1)" },
  ],
  images: [{ rect: [10, 100, 210, 250], xref: 7, width: 400, height: 300 }],
  annotations: [],
};

describe("PdfLayer", () => {
  test("text copies in reading order with spaces and line breaks, placed at its boxes", () => {
    const { container } = render(<PdfLayer layer={layer} imageUrl={(x) => `/img/${x}`} onGoto={() => {}} />);
    expect(container.querySelector(".pdf-text")?.textContent).toBe("Hello world\nNext\n");
    const first = container.querySelector<HTMLElement>(".pdf-run")!;
    // places are shares of the page and the size follows the page's --k, so a zoom does not re-render the runs
    expect([first.style.left, first.style.top]).toEqual(["3.333%", "2.500%"]);
    expect(first.style.getPropertyValue("--fs")).toBe("11.00");
  });

  test("page links go to their page, web links open, unsafe links stay inert", () => {
    const onGoto = mock(() => {});
    const { container } = render(<PdfLayer layer={layer} imageUrl={(x) => `/img/${x}`} onGoto={onGoto} />);
    const links = container.querySelectorAll<HTMLElement>(".pdf-link");
    fireEvent.click(links[0]);
    expect(onGoto).toHaveBeenCalledWith(3, 120);
    expect(links[1].getAttribute("href")).toBe("https://example.org");
    expect(links[2].tagName).toBe("SPAN");
    expect(links[2].getAttribute("href")).toBeNull();
    expect(container.querySelector(".pdf-image-save")?.getAttribute("href")).toBe("/img/7");
  });

  test("a formula run holds its LaTeX, and hovering anywhere in its box opens a card with the formula and its source", async () => {
    const withMath: PdfPageLayer = {
      ...layer,
      lines: [{ block: 0, runs: [{ t: "$$y=a\\times b \\tag{1.2}$$", rect: [10, 60, 200, 72], sp: true, math: 0 }] }],
      math: [{ rect: [10, 55, 200, 78], latex: "y=a\\times b", display: true, tag: "1.2" }],
    };
    const { container } = render(<PdfLayer layer={withMath} imageUrl={(x) => `/img/${x}`} onGoto={() => {}} />);
    expect(container.querySelector(".pdf-text")?.textContent).toBe(" $$y=a\\times b \\tag{1.2}$$\n");
    const page = container.querySelector<HTMLElement>(".pdf-layer")!;
    page.getBoundingClientRect = () => ({ left: 0, top: 0, width: layer.size[0], height: layer.size[1] }) as DOMRect;
    // a lower line of the display (below the run that carries the LaTeX), at 1 px per point
    fireEvent.mouseMove(page, { clientX: 100, clientY: 76 });
    await new Promise((r) => setTimeout(r, 400));
    const card = document.body.querySelector(".pdf-math-card")!;   // a portal: it floats over the whole app
    expect(card.querySelector(".katex")).toBeTruthy();
    expect(card.querySelector("code")?.textContent).toBe("$$y=a\\times b \\tag{1.2}$$");
  });
});
