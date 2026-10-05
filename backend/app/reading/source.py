"""Source blocks: the extracted text of a study range, cut into the smallest traceable pieces.

Text and positions are decided here, by the program, once per range (a *snapshot*); models only ever refer to
block IDs. Policies (versioned by EXTRACTION_VERSION):

- md / txt (unit = line): a block is a paragraph (consecutive non-blank lines), a heading (`#` line outside a code
  fence), a list item (with its continuation lines), a code fence (blank lines inside stay inside), a display
  formula (`$$ … $$`, possibly multi-line), a table (`|` lines), a block quote (`>` lines) or a figure (`![…](…)`).
  Lines are kept as extracted except trailing whitespace; heading markers are removed from the heading text.
- pdf (unit = page): the importer keeps PyMuPDF layout blocks (blank-line separated) and drops running headers /
  footers. A layout block is a paragraph in a typeset book but a single line in a Word export, so layout is not
  trusted: the range becomes one text stream, layout lines are joined until a sentence ends, and every sentence is a
  block (see _page_blocks). Headings are guessed conservatively (pdf_layout); the planner groups sentences by meaning.
- A paragraph / list item longer than MAX_BLOCK_CHARS is cut at sentence ends (never inside `$…$`) into pieces
  that keep `parent_block_id` and their offsets. A formula / code / table block is never cut; above
  HARD_MAX_BLOCK_CHARS it is refused as too large (there is no safe boundary).

IDs come from the snapshot, the reading order and the text hash, so identical paragraphs get different IDs."""
from __future__ import annotations

import hashlib
import re

from .schemas import Locator, SourceBlock

EXTRACTION_VERSION = "blocks-v3"   # v2: heading levels and structure confidence; v3: PDF text stream cut into sentences
MAX_BLOCK_CHARS = 700
TARGET_PIECE_CHARS = 350
HARD_MAX_BLOCK_CHARS = 8000

HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(\S.*?)\s*#*\s*$")
LIST_RE = re.compile(r"^\s{0,3}([-*+]|\d{1,3}[.)、])\s+\S")
FIGURE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
SENT_END = "。！？!?"
CLOSERS = "」』）)】\"'”’"


class SourceError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def snapshot_id(material_id: str, unit: str, start: int, end: int, units: list[str]) -> str:
    return "snap-" + sha("\x1f".join([material_id, unit, str(start), str(end), EXTRACTION_VERSION, sha("\x1e".join(units))]))[:16]


def ends_sentence(text: str) -> bool:
    t = text.rstrip().rstrip(CLOSERS)
    return bool(t) and (t[-1] in SENT_END or t.endswith(".") or t.endswith("$$") or t.endswith("```"))


def _inline_math_spans(text: str) -> list[tuple[int, int]]:
    spans, i = [], 0
    while True:
        a = text.find("$", i)
        if a == -1:
            return spans
        if text.startswith("$$", a):
            b = text.find("$$", a + 2)
            if b == -1:
                return spans
            spans.append((a, b + 2))
            i = b + 2
            continue
        b = text.find("$", a + 1)
        if b == -1:
            return spans
        spans.append((a, b + 1))
        i = b + 1


ENUM_ONLY_RE = re.compile(r"^\s*[(（]?[0-9A-Za-z一二三四五六七八九十ivxIVX]{1,4}[.)）]\s*$")   # "A." "1." "(iv)" — a list label
SENT_RE = re.compile(r"[。！？!?]+[" + re.escape(CLOSERS) + r"]*|(?<=[.;])\s+(?=[A-Z一-鿿])")


def sentence_cuts(text: str) -> list[int]:
    """Offsets right after a sentence end that are safe to cut at (not inside inline math)."""
    spans = _inline_math_spans(text)
    cuts = []
    for m in SENT_RE.finditer(text):
        c = m.end()
        if 0 < c < len(text) and not any(a < c < b for a, b in spans) \
                and not ENUM_ONLY_RE.match(text[cuts[-1] if cuts else 0:c]):
            cuts.append(c)
    return cuts


def cut_long(text: str, target: int = TARGET_PIECE_CHARS) -> list[tuple[int, int]]:
    """[start, end) pieces of about `target` chars at safe sentence ends; one long sentence stays whole."""
    cuts = sentence_cuts(text)
    pieces, a = [], 0
    last = 0
    for c in cuts + [len(text)]:
        if c - a > target and last > a:
            pieces.append((a, last))
            a = last
        last = c
    pieces.append((a, len(text)))
    out = []
    for a, b in pieces:   # trim whitespace at the cut, keep offsets exact
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if b > a:
            out.append((a, b))
    return out


# -- raw blocks -----------------------------------------------------------------

def _line_blocks(units: list[str], start: int) -> list[dict]:
    """[{kind, text, start, end, char_end}] for unit = line (units[0] is line number `start`)."""
    out: list[dict] = []
    buf: list[str] = []
    kind = "paragraph"
    first = 0

    def flush(last: int) -> None:
        nonlocal buf, kind
        if buf:
            text = "\n".join(buf)
            out.append({"kind": kind, "text": text, "start": first, "end": last, "char_end": len(text)})
        buf, kind = [], "paragraph"

    i, n = 0, len(units)
    while i < n:
        no = start + i
        raw = units[i].rstrip()
        if FENCE_RE.match(raw):
            flush(no - 1)
            fence = FENCE_RE.match(raw).group(1)
            j = i + 1
            while j < n and not units[j].lstrip().startswith(fence):
                j += 1
            j = min(j, n - 1)
            text = "\n".join(u.rstrip() for u in units[i:j + 1])
            out.append({"kind": "code", "text": text, "start": no, "end": start + j, "char_end": len(text)})
            i = j + 1
            continue
        if raw.strip().startswith("$$") and (raw.strip() == "$$" or not raw.strip().endswith("$$") or len(raw.strip()) < 4):
            flush(no - 1)
            j = i + 1
            while j < n and "$$" not in units[j]:
                j += 1
            j = min(j, n - 1)
            text = "\n".join(u.rstrip() for u in units[i:j + 1])
            out.append({"kind": "formula", "text": text, "start": no, "end": start + j, "char_end": len(text)})
            i = j + 1
            continue
        if not raw.strip():
            flush(no - 1)
            i += 1
            continue
        h = HEADING_RE.match(raw)
        if h:
            flush(no - 1)
            out.append({"kind": "heading", "text": h.group(2).strip(), "start": no, "end": no, "char_end": len(raw),
                        "level": len(h.group(1)), "confidence": "high"})
            i += 1
            continue
        s = raw.strip()
        line_kind = ("figure" if FIGURE_RE.match(raw) else "formula" if s.startswith("$$") and s.endswith("$$")
                     else "list_item" if LIST_RE.match(raw) else "table" if s.startswith("|")
                     else "quote" if s.startswith(">") else None)
        if line_kind in ("figure", "formula", "list_item") or (line_kind in ("table", "quote") and kind != line_kind):
            flush(no - 1)
        elif line_kind is None and kind in ("table", "quote", "figure", "formula"):
            flush(no - 1)
        if not buf:
            first = no
            kind = line_kind or "paragraph"
        buf.append(raw)
        if line_kind in ("figure", "formula"):
            flush(no)
        i += 1
    flush(start + n - 1)
    return out


HEADING_MAX_CHARS = 40
HEADING_LINE_SHARE = 0.75   # a heading is clearly narrower than the page's full lines
NO_HEADING_END_RE = re.compile(r"[。．.!?！？,，;；：:、]$")
BULLET_RE = re.compile(r"^\s*[•○●■□◆◇▪▫–\-*+]\s")
PDF_ITEM_RE = re.compile(r"^\s*(•|[-*+]\s|\d{1,3}[.)、]\s*\S|[（(]\d{1,2}[）)]|[A-Za-z][.)]\s)")
PARA_END = SENT_END + "：:"   # a layout line ending with one of these may close a paragraph


def _page_paras(page: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\s*$)", page, re.S)]


def _width(text: str) -> int:
    """Display width: a CJK / full-width char takes two columns, so mixed Chinese–English lines compare fairly."""
    return sum(2 if CJK_CHAR_RE.match(ch) else 1 for ch in text)


def _full_line_width(widths: list[int]) -> float:
    """Width of a full line on this page (90th percentile of its layout paragraphs)."""
    if not widths:
        return 0.0
    s = sorted(widths)
    return s[min(len(s) - 1, int(len(s) * 0.9))]


def pdf_layout(units: list[str]) -> list[list[tuple[int, int, str]]]:
    """Per page: (char_start, char_end, kind) of each blank-line separated layout paragraph, kind one of heading /
    formula / item (opens a list item) / label (a list label alone on its line) / body.

    The text layer does not say where paragraphs are: in a typeset book a layout paragraph is a paragraph, in a Word
    export it is one line. A heading guess therefore needs more than "short, no closing punctuation" (every
    mid-sentence Chinese line is that): it must follow a finished sentence (or another heading / the range start),
    and be shorter than the page's full lines. The guess is low confidence and never defines a section."""
    out: list[list[tuple[int, int, str]]] = []
    prev_closed = True
    for page in units:
        paras = _page_paras(page)
        full = _full_line_width([_width(page[a:b]) for a, b in paras])
        kinds = []
        for a, b in paras:
            p = page[a:b]
            s = p.strip()
            if s.startswith("$$") and s.endswith("$$"):
                kind = "formula"
            elif ENUM_ONLY_RE.match(s):
                kind = "label"   # a list label alone on its line ("A."): it opens the item that follows
            elif BULLET_RE.match(p):
                kind = "item"
            elif "\n" not in p and len(paras) > 1 and prev_closed and len(s) <= HEADING_MAX_CHARS \
                    and _width(s) <= HEADING_LINE_SHARE * full and not NO_HEADING_END_RE.search(s):
                kind = "heading"
            elif PDF_ITEM_RE.match(p) or LIST_RE.match(p):
                kind = "item"
            else:
                kind = "body"
            prev_closed = kind in ("heading", "formula") or (kind != "label" and (ends_sentence(p) or s.endswith(("：", ":"))))
            kinds.append((a, b, kind))
        out.append(kinds)
    return out


def _joiner(left: str, right: str) -> str:
    """What goes between two layout lines of one paragraph: nothing around CJK, a space between Latin words."""
    if not left or not right:
        return ""
    if ENUM_ONLY_RE.match(left):
        return " "
    if CJK_CHAR_RE.match(left[-1]) or CJK_CHAR_RE.match(right[0]) or left[-1] in "（(「『“‘" or right[0] in "，。、；：！？）)」』”’,.;:!?":
        return ""
    return " "


CJK_CHAR_RE = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿＀-￯]")


def _page_blocks(units: list[str], start: int) -> list[dict]:
    """PDF: the range's layout lines become one text stream, cut into sentences.

    Layout lines are joined into paragraphs until a line ends a sentence (or with a colon); a heading, a formula or a
    list item opens a new paragraph. Each paragraph is cut at sentence ends and every sentence is a block, so the
    planner can draw unit boundaries by meaning instead of trusting PDF line breaks; `para_group` keeps the rebuilt
    paragraph as a hint (rules fallback, planner listing, quote text). A sentence may run across a page break: its
    locator then starts on one page and ends on the next (char_start on the start page, char_end on the end page)."""
    layout = pdf_layout(units)
    out: list[dict] = []
    group: list[tuple[int, int, int, int]] = []   # (page index, para k, char_start, char_end) of the open paragraph

    def flush() -> None:
        nonlocal group
        if not group:
            return
        text, pieces = "", []   # pieces: (offset in text, page index, k, page char_start, length)
        for pi, k, a, b in group:
            seg = units[pi][a:b].replace("\n", " ")
            text += _joiner(text, seg)
            pieces.append((len(text), pi, k, a, len(seg)))
            text += seg
        gid = f"g{len(out):04d}"

        def where(off: int, end: bool) -> tuple[int, int, int]:
            """(page index, para k, page char offset) of a text offset (end=True: the char before `off`)."""
            for t0, pi, k, a, n in reversed(pieces):
                if (t0 < off <= t0 + n) if end else (t0 <= off < t0 + n):
                    return pi, k, a + off - t0
            t0, pi, k, a, n = pieces[-1] if end else pieces[0]
            return pi, k, a + (n if end else 0)

        cuts = sentence_cuts(text)
        spans = [(a, b) for a, b in zip([0] + cuts, cuts + [len(text)])]
        for a, b in spans:
            while a < b and text[a].isspace():
                a += 1
            while b > a and text[b - 1].isspace():
                b -= 1
            if b <= a:
                continue
            p0, k0, c0 = where(a, False)
            p1, _, c1 = where(b, True)
            refs = sorted({(start + pi, k) for t0, pi, k, pa, n in pieces if t0 < b and a < t0 + n})
            out.append({"kind": "paragraph", "text": text[a:b], "start": start + p0, "end": start + p1, "para": k0,
                        "paras": len(layout[p0]), "char_start": c0, "char_end": c1,
                        "meta": {"para_group": gid, "pdf_paras": [list(r) for r in refs]}})
        group = []

    for pi, kinds in enumerate(layout):
        for k, (a, b, kind) in enumerate(kinds, 1):
            if kind in ("heading", "formula"):
                flush()
                out.append({"kind": kind, "text": units[pi][a:b], "start": start + pi, "end": start + pi, "para": k,
                            "paras": len(kinds), "char_start": a, "char_end": b, "meta": {"pdf_paras": [[start + pi, k]]},
                            **({"level": None, "confidence": "low"} if kind == "heading" else {})})
                continue
            if kind in ("item", "label"):
                flush()
            group.append((pi, k, a, b))
            if kind != "label" and units[pi][a:b].rstrip().rstrip(CLOSERS)[-1:] in PARA_END:
                flush()
    flush()
    # locator para / paras count rebuilt paragraphs (and headings / formulas) starting on a page, not layout lines;
    # the layout paragraphs themselves stay in meta["pdf_paras"] for the PDF evidence
    numbers: dict[int, dict[str, int]] = {}
    for i, r in enumerate(out):
        key = r["meta"].get("para_group") or f"b{i}"
        page = numbers.setdefault(r["start"], {})
        r["para"] = page.setdefault(key, len(page) + 1)
    for r in out:
        r["paras"] = len(numbers[r["start"]])
    return out


def extract_blocks(material_id: str, unit: str, start: int, end: int, units: list[str]) -> tuple[dict, list[SourceBlock]]:
    """(snapshot row, blocks in reading order) for units start..end of a material."""
    sid = snapshot_id(material_id, unit, start, end, units)
    raw = _line_blocks(units, start) if unit == "line" else _page_blocks(units, start)
    blocks: list[SourceBlock] = []

    def add(r: dict, text: str, a: int, b: int, parent: str | None = None, piece: int | None = None,
            continues: bool = False) -> SourceBlock:
        th = sha(text)
        bid = f"{sid[5:13]}-{len(blocks):04d}-{th[:6]}"
        blk = SourceBlock(block_id=bid, material_snapshot_id=sid, ordinal=len(blocks), kind=r["kind"], text=text,
                          locator=Locator(unit=unit, start=r["start"], end=r["end"], para=r.get("para"),
                                          paras=r.get("paras"), char_start=a, char_end=b),
                          text_hash=th, extraction_version=EXTRACTION_VERSION, parent_block_id=parent, piece=piece,
                          continues=continues, heading_level=r.get("level"), structure_confidence=r.get("confidence"),
                          meta=dict(r.get("meta") or {}))
        blocks.append(blk)
        return blk

    for r in raw:
        text, base = r["text"], r.get("char_start", 0)
        if "para_group" in r.get("meta", {}):   # a PDF sentence: offsets already resolved, one sentence is never cut
            add(r, text, base, r["char_end"])
            continue
        if len(text) <= MAX_BLOCK_CHARS or r["kind"] in ("heading",):
            add(r, text, base, base + len(text), continues=r.get("continues", False))
            continue
        if r["kind"] in ("formula", "code", "table", "figure"):
            if len(text) > HARD_MAX_BLOCK_CHARS:
                raise SourceError("range_too_large", f"第 {r['start']} 處的{'程式碼' if r['kind'] == 'code' else '公式／表格'}"
                                                     f"有 {len(text)} 字，沒有安全的切點，請縮小範圍或分開學習。")
            add(r, text, base, base + len(text))
            continue
        pieces = cut_long(text)
        parent = f"{sid[5:13]}-p{len(blocks):04d}-{sha(text)[:6]}"
        for k, (a, b) in enumerate(pieces, 1):
            add(r, text[a:b], base + a, base + b, parent=parent, piece=k,
                continues=r.get("continues", False) and k == len(pieces))
    if not blocks or all(b.kind == "heading" for b in blocks):
        raise SourceError("no_text", "這個範圍裡沒有可導讀的文字。")
    snap = {"snapshot_id": sid, "unit": unit, "start": start, "end": end, "extraction_version": EXTRACTION_VERSION,
            "text_hash": sha("\x1e".join(units)), "block_count": len(blocks)}
    return snap, blocks
