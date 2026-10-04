"""Source blocks: the extracted text of a study range, cut into the smallest traceable pieces.

Text and positions are decided here, by the program, once per range (a *snapshot*); models only ever refer to
block IDs. Policies (versioned by EXTRACTION_VERSION):

- md / txt (unit = line): a block is a paragraph (consecutive non-blank lines), a heading (`#` line outside a code
  fence), a list item (with its continuation lines), a code fence (blank lines inside stay inside), a display
  formula (`$$ … $$`, possibly multi-line), a table (`|` lines), a block quote (`>` lines) or a figure (`![…](…)`).
  Lines are kept as extracted except trailing whitespace; heading markers are removed from the heading text.
- pdf (unit = page): the importer already joined PyMuPDF layout blocks into paragraphs (blank-line separated) and
  dropped running headers/footers with page numbers. A one-line short paragraph without closing punctuation on a
  page with other paragraphs is a heading (same rule as the legacy splitter). A page's last paragraph that does not
  end a sentence `continues` on the next page.
- A paragraph / list item longer than MAX_BLOCK_CHARS is cut at sentence ends (never inside `$…$`) into pieces
  that keep `parent_block_id` and their offsets. A formula / code / table block is never cut; above
  HARD_MAX_BLOCK_CHARS it is refused as too large (there is no safe boundary).

IDs come from the snapshot, the reading order and the text hash, so identical paragraphs get different IDs."""
from __future__ import annotations

import hashlib
import re

from .schemas import Locator, SourceBlock

EXTRACTION_VERSION = "blocks-v2"   # v2: heading levels and structure confidence
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


SENT_RE = re.compile(r"[。！？!?]+[" + re.escape(CLOSERS) + r"]*|(?<=[.;])\s+(?=[A-Z一-鿿])")


def sentence_cuts(text: str) -> list[int]:
    """Offsets right after a sentence end that are safe to cut at (not inside inline math)."""
    spans = _inline_math_spans(text)
    cuts = []
    for m in SENT_RE.finditer(text):
        c = m.end()
        if 0 < c < len(text) and not any(a < c < b for a, b in spans):
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


def _is_page_heading(p: str, n_paras: int) -> bool:
    return "\n" not in p and len(p) <= 40 and not re.search(r"[。．.!?！？,，;；]$", p) and n_paras > 1


def _page_blocks(units: list[str], start: int) -> list[dict]:
    out: list[dict] = []
    for i, page in enumerate(units):
        no = start + i
        paras = [(m.start(), m.end()) for m in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\s*$)", page, re.S)]
        for k, (a, b) in enumerate(paras, 1):
            p = page[a:b]
            s = p.strip()
            kind = ("heading" if _is_page_heading(p, len(paras)) else
                    "formula" if s.startswith("$$") and s.endswith("$$") else
                    "list_item" if LIST_RE.match(p) else "paragraph")
            out.append({"kind": kind, "text": p, "start": no, "end": no, "para": k, "paras": len(paras),
                        "char_start": a, "char_end": b, "last_on_page": k == len(paras),
                        # a short line is a heading guess: its level is unknown and it never defines a section
                        **({"level": None, "confidence": "low"} if kind == "heading" else {})})
    for x, y in zip(out, out[1:]):   # a sentence broken by the page end continues on the next page
        if x.get("last_on_page") and y["start"] == x["start"] + 1 and y.get("para") == 1 \
                and x["kind"] not in ("heading", "formula") and y["kind"] != "heading" and not ends_sentence(x["text"]):
            x["continues"] = True
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
                          continues=continues, heading_level=r.get("level"), structure_confidence=r.get("confidence"))
        blocks.append(blk)
        return blk

    for r in raw:
        text, base = r["text"], r.get("char_start", 0)
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
