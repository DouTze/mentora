"""One page for the reader: what is laid over the page image so text can be selected and copied in reading order,
links followed, images and comments found. All rectangles are in view space (points, y down)."""
from __future__ import annotations

from .inspect import page_annotations, page_images, page_links
from .text import PageText, extract_page

LAYER_VERSION = 1


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


def page_layer(doc, n: int) -> dict:
    """Page n (1-based): size, label, text lines (runs), links, images and annotations."""
    page = doc[n - 1]
    text = extract_page(page)
    lines = []
    for start, end in text.lines():
        runs = _runs(text, start, end)
        if runs:
            lines.append({"block": text.block_of_line[text.line_of[start]], "runs": runs})
    return {
        "version": LAYER_VERSION, "page": n, "label": page.get_label() or str(n),
        "size": [round(page.rect.width, 1), round(page.rect.height, 1)], "quarter": text.quarter,
        "lines": lines, "links": page_links(doc, page), "images": page_images(page), "annotations": page_annotations(page),
    }
