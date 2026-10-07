"""Text of a page: glyphs in view space, then reading order (a port of printcraft crates/render/src/text.rs).

PyMuPDF gives every glyph with its Unicode value and box; boxes are mapped to view space (rotation applied, y down).
On top of the glyph list `layout` groups lines (baseline clustering), infers word gaps, reverses right-to-left runs
and orders columns. Each glyph also keeps its font, size, style flags and colour, which the reader and the notation
evidence use."""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field


@dataclass
class Glyph:
    text: str
    rect: list[float]                  # [x0, y0, x1, y1] in page view space
    font: str = ""
    size: float = 0.0
    flags: int = 0                     # PyMuPDF span flags: 1 superscript, 2 italic, 8 monospaced, 16 bold
    color: int = 0                     # sRGB as 0xRRGGBB


@dataclass
class PageText:
    """The text of one page in reading order, with line structure."""
    glyphs: list[Glyph] = field(default_factory=list)
    line_of: list[int] = field(default_factory=list)          # each glyph's line (lines numbered in reading order)
    space_before: list[bool] = field(default_factory=list)    # a word break precedes the glyph on the same line
    block_of_line: list[int] = field(default_factory=list)    # each line's block (stacked lines of one column)
    quarter: int = 0                   # the text runs this many quarter turns clockwise from left-to-right

    def is_empty(self) -> bool:
        return not self.glyphs

    def text_of(self, start: int, end: int) -> str:
        """Text of glyphs start..end with spaces and line breaks reconstructed."""
        out: list[str] = []
        for i in range(start, min(end, len(self.glyphs))):
            if i != start:
                if self.line_of[i] != self.line_of[i - 1]:
                    out.append("\n")
                elif self.space_before[i]:
                    out.append(" ")
            out.append(self.glyphs[i].text)
        return "".join(out)

    def plain_text(self) -> str:
        return self.text_of(0, len(self.glyphs))

    def lines(self) -> list[tuple[int, int]]:
        """(start, end) glyph range of every line."""
        out: list[tuple[int, int]] = []
        for i, ln in enumerate(self.line_of):
            if out and self.line_of[out[-1][0]] == ln:
                out[-1] = (out[-1][0], i + 1)
            else:
                out.append((i, i + 1))
        return out

    def _chars(self, fold) -> tuple[list[str], list[int]]:
        """The page as characters (words and lines separated by one space) with the glyph each came from."""
        chars: list[str] = []
        owner: list[int] = []
        for i, g in enumerate(self.glyphs):
            if i > 0 and (self.space_before[i] or self.line_of[i] != self.line_of[i - 1]) and chars and chars[-1] != " ":
                chars.append(" ")
                owner.append(i)
            for c in fold(g.text):
                chars.append(" " if c.isspace() else c)
                owner.append(i)
        return chars, owner

    def find(self, needle: str, case_sensitive: bool = False, whole_words: bool = False) -> list[tuple[int, int]]:
        """Glyph ranges of every match; words may span line breaks, which count as a single space."""
        fold = (lambda s: s) if case_sensitive else str.lower
        want = list(" ".join(fold(needle).split()))
        if not want:
            return []
        chars, owner = self._chars(fold)
        word = lambda k: 0 <= k < len(chars) and chars[k].isalnum()   # noqa: E731
        out, i, n = [], 0, len(want)
        while i + n <= len(chars):
            bounded = not whole_words or (not word(i - 1) and not word(i + n))
            if bounded and chars[i:i + n] == want:
                out.append((owner[i], owner[i + n - 1] + 1))
                i += n
            else:
                i += 1
        return out

    def line_rects(self, start: int, end: int) -> list[list[float]]:
        """One rectangle per line for the glyphs start..end (for highlighting)."""
        out: list[tuple[int, list[float]]] = []
        for i in range(start, min(end, len(self.glyphs))):
            r = self.glyphs[i].rect
            if out and out[-1][0] == self.line_of[i]:
                _union(out[-1][1], r)
            else:
                out.append((self.line_of[i], list(r)))
        return [r for _, r in out]

    def nearest(self, x: float, y: float) -> int | None:
        """Index of the glyph nearest to a view-space point; vertical distance weighs more (prefer the same line)."""
        best, best_d = None, float("inf")
        for i, g in enumerate(self.glyphs):
            r = g.rect
            dx = r[0] - x if x < r[0] else x - r[2] if x > r[2] else 0.0
            dy = r[1] - y if y < r[1] else y - r[3] if y > r[3] else 0.0
            if dx + dy * 3 < best_d:
                best, best_d = i, dx + dy * 3
        return best


# -- extraction ------------------------------------------------------------------------------------
def extract_page(page) -> PageText:
    """All glyphs of a PyMuPDF page in view space, laid out in reading order. Ligatures come out as their letters
    (MuPDF splits the glyph box), the spaces MuPDF invents between words are dropped (layout infers word gaps)."""
    import pymupdf

    m = page.rotation_matrix
    w, h = page.rect.width, page.rect.height
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    glyphs: list[Glyph] = []
    directions = [0, 0, 0, 0]                       # glyphs running right, down, left, up in view space
    for b in raw["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            ux, uy = ln["dir"]                      # baseline direction in page space (y down)
            dx, dy = ux * m.a + uy * m.c, ux * m.b + uy * m.d
            d = (0 if dx >= 0 else 2) if abs(dx) >= abs(dy) else (1 if dy > 0 else 3)
            for s in ln["spans"]:
                size = s["size"] or 1.0
                for ch in s["chars"]:
                    c = ch["c"]
                    if ch.get("synthetic") or not c or all(not x.isprintable() and not x.isspace() for x in c):
                        continue
                    # the em box, as PrintCraft measures glyphs: the advance along the baseline, 0.8 em above it and
                    # 0.2 em below (MuPDF's own box spans the font's whole ascender-to-descender, which is taller and
                    # varies by font, and the layout thresholds are in em)
                    bx0, by0, bx1, by1 = ch["bbox"]
                    ox, oy = ch["origin"]
                    adv = abs((bx1 - bx0) * ux) + abs((by1 - by0) * uy)
                    upx, upy = uy, -ux
                    pts = [pymupdf.Point(ox + ux * a + upx * e, oy + uy * a + upy * e) * m
                           for a in (0.0, adv) for e in (-0.2 * size, 0.8 * size)]
                    r = pymupdf.Rect(min(p.x for p in pts), min(p.y for p in pts), max(p.x for p in pts), max(p.y for p in pts))
                    if not r.is_valid or r.width > 10_000 or r.height > 10_000:
                        continue
                    directions[d] += 1
                    glyphs.append(Glyph(c, [r.x0, r.y0, r.x1, r.y1], s["font"], s["size"], s["flags"], s["color"]))
    # lay out in the frame where most text runs left to right, then map the boxes back (vertical CJK, rotated pages)
    quarter = max(range(4), key=lambda q: (directions[q], q == 0))
    for g in glyphs:
        g.rect = to_upright(g.rect, quarter, w, h)
    text = layout(glyphs)
    for g in text.glyphs:
        g.rect = from_upright(g.rect, quarter, w, h)
    text.quarter = quarter
    return text


def to_upright(r: list[float], quarter: int, w: float, h: float) -> list[float]:
    """View-space box → the frame where text runs left to right, for text running `quarter` × 90° clockwise."""
    x0, y0, x1, y1 = r
    if quarter == 1:
        return [y0, w - x1, y1, w - x0]
    if quarter == 2:
        return [w - x1, h - y1, w - x0, h - y0]
    if quarter == 3:
        return [h - y1, x0, h - y0, x1]
    return list(r)


def from_upright(r: list[float], quarter: int, w: float, h: float) -> list[float]:
    u0, v0, u1, v1 = r
    if quarter == 1:
        return [w - v1, u0, w - v0, u1]
    if quarter == 2:
        return [w - u1, h - v1, w - u0, h - v0]
    if quarter == 3:
        return [v0, h - u1, v1, h - u0]
    return list(r)


# -- layout -----------------------------------------------------------------------------------------
def _in(c: str, ranges) -> bool:
    o = ord(c)
    return any(a <= o <= b for a, b in ranges)


RTL = ((0x0590, 0x08FF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF), (0x10800, 0x10FFF), (0x1E800, 0x1EFFF))
# scripts whose combining signs can sit apart from their base (Indic, Thai, Lao, Myanmar, Khmer): no inferred spaces
COMPLEX = ((0x0300, 0x036F), (0x0900, 0x0DFF), (0x0E00, 0x0EFF), (0x1000, 0x109F), (0x1780, 0x17FF))
CJK = ((0x2E80, 0x9FFF), (0xAC00, 0xD7AF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF), (0x20000, 0x3134F), (0x1100, 0x11FF))


def _any(text: str, ranges) -> bool:
    return any(_in(c, ranges) for c in text)


def _union(a: list[float], b: list[float]) -> None:
    a[0], a[1], a[2], a[3] = min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


class _Seg:
    __slots__ = ("idx", "bbox", "h")

    def __init__(self, i: int, rect: list[float], h: float):
        self.idx, self.bbox, self.h = [i], list(rect), h

    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2


def layout(glyphs: list[Glyph]) -> PageText:
    """Reading-order layout. Glyphs are reordered into reading order, lines numbered in that order, word gaps
    inferred per line. The heuristics are printcraft's:

    1. Segments: consecutive glyphs (content order) on one baseline with no column-sized gap. Segments on the same
       baseline that nearly touch are merged (text drawn out of order).
    2. Blocks: segments stacked with line-sized gaps and overlapping horizontally.
    3. Block order: repeatedly take the top-most remaining block, preferring a block to its left that overlaps it
       vertically (so columns read left, then right).
    4. Within a segment glyphs run left to right; runs of right-to-left script are reversed (logical order)."""
    # drop "fake bold" duplicates: the same character redrawn at (almost) the same place
    keep = [True] * len(glyphs)
    for i in range(1, len(glyphs)):
        g = glyphs[i]
        gh = max(g.rect[3] - g.rect[1], 0.1)
        for j in range(i - 1, max(i - 4, 0) - 1, -1):
            p = glyphs[j]
            if keep[j] and p.text == g.text and abs(p.rect[0] - g.rect[0]) < gh * 0.2 and abs(p.rect[1] - g.rect[1]) < gh * 0.2:
                keep[i] = False
                break
    glyphs = [g for g, k in zip(glyphs, keep) if k]
    n = len(glyphs)
    if n == 0:
        return PageText()
    height = [max(g.rect[3] - g.rect[1], 0.1) for g in glyphs]
    cy = [(g.rect[1] + g.rect[3]) / 2 for g in glyphs]

    # 1. segments in content order
    segs: list[_Seg] = []
    for i in range(n):
        g = glyphs[i].rect
        if segs:
            p = segs[-1].idx[-1]
            ph = min(height[p], height[i])
            gap = g[0] - glyphs[p].rect[2]
            if abs(cy[i] - cy[p]) < ph * 0.5 and gap < ph * 3 and g[0] > glyphs[p].rect[0] - ph * 2:
                s = segs[-1]
                s.idx.append(i)
                _union(s.bbox, g)
                s.h = max(s.h, height[i])
                continue
        segs.append(_Seg(i, g, height[i]))
    segs = _merge_same_baseline(segs)
    for s in segs:
        s.idx.sort(key=lambda k: glyphs[k].rect[0])

    # 2. blocks
    blocks: list[tuple[list[int], list[float]]] = []
    for si in sorted(range(len(segs)), key=lambda k: segs[k].bbox[1]):
        s = segs[si]
        target = None
        for bi, (members, bb) in enumerate(blocks):
            last = segs[members[-1]]
            vgap = s.bbox[1] - last.bbox[3]
            overlap = min(s.bbox[2], bb[2]) - max(s.bbox[0], bb[0])
            minw = max(min(s.bbox[2] - s.bbox[0], bb[2] - bb[0]), 1.0)
            if -last.h * 0.5 < vgap < max(last.h, s.h) * 1.1 and overlap > minw * 0.3 and abs(last.h / s.h - 1) < 0.6:
                target = bi
                break
        if target is None:
            blocks.append(([si], list(s.bbox)))
        else:
            blocks[target][0].append(si)
            _union(blocks[target][1], s.bbox)

    # 3. block order
    remaining = list(range(len(blocks)))
    block_order: list[int] = []
    while remaining:
        top = min(remaining, key=lambda k: blocks[k][1][1])
        tb = blocks[top][1]
        left = [c for c in remaining
                if blocks[c][1][2] <= tb[0] + 1
                and min(blocks[c][1][3], tb[3]) - max(blocks[c][1][1], tb[1]) > 0.5 * min(blocks[c][1][3] - blocks[c][1][1], tb[3] - tb[1])]
        pick = min(left, key=lambda k: blocks[k][1][0]) if left else top
        remaining.remove(pick)
        block_order.append(pick)

    # 4. glyphs in reading order with line numbers and word gaps
    order: list[int] = []
    line_of: list[int] = []
    space_before: list[bool] = []
    block_of_line: list[int] = []
    line = 0
    for bn, b in enumerate(block_order):
        for si in blocks[b][0]:
            s = segs[si]
            idx = list(s.idx)
            rtl = lambda k: _any(glyphs[k].text, RTL)   # noqa: E731
            k = 0
            while k < len(idx):
                if rtl(idx[k]):
                    e = k
                    while e + 1 < len(idx) and (rtl(idx[e + 1]) or (not glyphs[idx[e + 1]].text.strip() and e + 2 < len(idx) and rtl(idx[e + 2]))):
                        e += 1
                    idx[k:e + 1] = idx[k:e + 1][::-1]
                    k = e + 1
                else:
                    k += 1
            # word gaps relative to this line's typical letter gap (handles tracking)
            gaps = sorted(glyphs[c].rect[0] - glyphs[p].rect[2] for p, c in zip(s.idx, s.idx[1:]))
            typical = max(gaps[len(gaps) // 3], 0.0) if gaps else 0.0
            threshold = max(typical + s.h * 0.15, s.h * 0.15)
            spaces = [False] * len(idx)
            pos = {g: k for k, g in enumerate(idx)}
            for p, c in zip(s.idx, s.idx[1:]):
                gap = glyphs[c].rect[0] - glyphs[p].rect[2]
                pt, ct = glyphs[p].text, glyphs[c].text
                cjk = _any(pt, CJK) and _any(ct, CJK) and gap < s.h * 0.5
                tight = (_any(pt, COMPLEX) or _any(ct, COMPLEX)) and gap < s.h * 0.6
                if gap > threshold and not cjk and not tight and pt.strip() and ct.strip():
                    spaces[pos[max(c, p)]] = True
            for k, g in enumerate(idx):
                order.append(g)
                line_of.append(line)
                space_before.append(k > 0 and spaces[k])
            block_of_line.append(bn)
            line += 1
    return PageText([glyphs[i] for i in order], line_of, space_before, block_of_line)


def _merge_same_baseline(segs: list[_Seg]) -> list[_Seg]:
    """Merge segments on the same baseline that nearly touch (printcraft's merge loop; candidates are looked up by
    baseline so a dense page stays fast)."""
    merged = True
    while merged:
        merged = False
        order = sorted(range(len(segs)), key=lambda k: segs[k].cy())
        cys = [segs[k].cy() for k in order]
        max_h = max((s.h for s in segs), default=0.0)
        for oa, a in enumerate(order):
            sa = segs[a]
            lo, hi = bisect_left(cys, sa.cy() - max_h * 0.3), bisect_left(cys, sa.cy() + max_h * 0.3)
            for b in order[lo:hi]:
                if b == a:
                    continue
                sb = segs[b]
                hh = min(sa.h, sb.h)
                gap = max(sb.bbox[0] - sa.bbox[2], sa.bbox[0] - sb.bbox[2])
                if abs(sa.cy() - sb.cy()) < hh * 0.3 and abs(sa.h / sb.h - 1) < 0.35 and gap < hh * 1.2:
                    sa.idx.extend(sb.idx)
                    _union(sa.bbox, sb.bbox)
                    sa.h = max(sa.h, sb.h)
                    segs = [s for k, s in enumerate(segs) if k != b]
                    merged = True
                    break
            if merged:
                break
    return segs
