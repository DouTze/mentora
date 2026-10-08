"""One page for the reader: what is laid over the page image so text can be selected and copied in reading order,
links followed, images and comments found. All rectangles are in view space (points, y down)."""
from __future__ import annotations

from .inspect import page_annotations, page_images, page_links, view_rect
from .text import PageText, extract_page

LAYER_VERSION = 2


def _runs(text: PageText, start: int, end: int) -> list[dict]:
    """A line cut into runs: a new run at each word break and where the font size changes, so each run can be
    stretched to its width on screen. `sp`: a space precedes the run."""
    runs: list[dict] = []
    cur: dict | None = None
    for i in range(start, end):
        g = text.glyphs[i]
        if not g.text.strip():                       # a drawn space ends the run; the next one starts a word
            if cur is not None:
                runs.append(cur)
                cur = None
            if runs:
                runs[-1]["_space"] = True
            continue
        brk = cur is not None and (text.space_before[i] or abs(g.size - cur["_size"]) > 0.5)
        if cur is None or brk:
            if cur is not None:
                runs.append(cur)
            cur = {"t": g.text, "rect": list(g.rect), "_size": g.size,
                   "sp": bool(runs) and (text.space_before[i] or runs[-1].get("_space", False))}
        else:
            cur["t"] += g.text
            r = cur["rect"]
            r[0], r[1], r[2], r[3] = min(r[0], g.rect[0]), min(r[1], g.rect[1]), max(r[2], g.rect[2]), max(r[3], g.rect[3])
    if cur is not None:
        runs.append(cur)
    for r in runs:
        r.pop("_size", None)
        r.pop("_space", None)
        r["rect"] = [round(v, 2) for v in r["rect"]]
        if not r["sp"]:
            del r["sp"]
    return runs


def _center_in(r: list[float], box: list[float]) -> bool:
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    return box[0] - 0.5 <= cx <= box[2] + 0.5 and box[1] - 0.5 <= cy <= box[3] + 0.5


def _with_math(lines: list[dict], math: list[dict]) -> list[dict]:
    """The text runs inside a formula give way to one run holding its LaTeX ($…$ / $$…$$), placed where the formula is,
    so selecting and copying the formula copies its LaTeX."""
    for k, m in enumerate(math):
        box = m["rect"]
        hit = [(li, ri) for li, ln in enumerate(lines) for ri, r in enumerate(ln["runs"]) if _center_in(r["rect"], box)]
        if not hit:
            continue
        heights = sorted(lines[li]["runs"][ri]["rect"][3] - lines[li]["runs"][ri]["rect"][1] for li, ri in hit)
        h = min(box[3] - box[1], heights[len(heights) // 2] * 1.1)
        cy = (box[1] + box[3]) / 2
        first = min(li for li, _ in hit)
        gone = set(hit)
        for li, ln in enumerate(lines):
            ln["runs"] = [r for ri, r in enumerate(ln["runs"]) if (li, ri) not in gone]
        run = {"t": m["tex"], "rect": [box[0], round(cy - h / 2, 2), box[2], round(cy + h / 2, 2)], "sp": True, "math": k}
        at = next((k for k, r in enumerate(lines[first]["runs"]) if r["rect"][0] >= box[0]), len(lines[first]["runs"]))
        lines[first]["runs"].insert(at, run)
    return [ln for ln in lines if ln["runs"]]


def page_math(doc, page) -> list[dict]:
    """The page's formulas for the reader: where (view space), their LaTeX and markup, display or inline."""
    import pymupdf

    from .math import FontNames, page_math as read, wants_math
    if not wants_math(doc, page.number):
        return []
    pm = read(page, FontNames(doc), pymupdf.TEXT_DEHYPHENATE)
    return [{"rect": view_rect(page, r.rect), "latex": r.latex, "display": r.display, "tag": r.tag, "tex": r.markup()}
            for r in pm.regions]


def page_layer(doc, n: int) -> dict:
    """Page n (1-based): size, label, text lines (runs), formulas, links, images and annotations."""
    page = doc[n - 1]
    text = extract_page(page)
    lines = []
    for start, end in text.lines():
        runs = _runs(text, start, end)
        if runs:
            lines.append({"block": text.block_of_line[text.line_of[start]], "runs": runs})
    try:
        math = page_math(doc, page)
    except Exception:  # noqa: BLE001 — a page the formula reader cannot take keeps its plain text layer
        math = []
    if math and text.quarter == 0:
        lines = _with_math(lines, math)
    return {
        "version": LAYER_VERSION, "page": n, "label": page.get_label() or str(n),
        "size": [round(page.rect.width, 1), round(page.rect.height, 1)], "quarter": text.quarter,
        "lines": lines, "math": [{k: v for k, v in m.items() if k != "tex"} for m in math],
        "links": page_links(doc, page), "images": page_images(page), "annotations": page_annotations(page),
    }
