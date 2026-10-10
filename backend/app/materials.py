"""Imported materials (教材): pdf / md / txt, grouped into categories.

A material is split into addressable units so a study range can be exact:
pdf → pages, md/txt → lines. Unit numbers are 1-based, ranges are inclusive."""
from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

from . import config
from .db import Database, new_id, now_iso

log = logging.getLogger("mentor.materials")
KINDS = {".pdf": "pdf", ".md": "md", ".markdown": "md", ".txt": "txt"}
# how a PDF's text was extracted: 1 PyMuPDF's characters as they are; 2 glyphs read through their names (TeX fonts)
# and formulas as $…$ / $$…$$ (app.pdfdoc.math). Stored per material: the paragraph evidence re-reads the original
# the same way its units were made, so a (page, paragraph) locator keeps pointing at the same block.
TEXT_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  position INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materials (
  id TEXT PRIMARY KEY,
  category_id TEXT,
  title TEXT NOT NULL,
  filename TEXT NOT NULL,
  kind TEXT NOT NULL,
  unit TEXT NOT NULL,
  unit_count INTEGER NOT NULL,
  units TEXT NOT NULL,
  created_at TEXT NOT NULL,
  text_version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'ready',
  status_detail TEXT,
  translate_zh INTEGER NOT NULL DEFAULT 0,  -- 1: guides of a non-Chinese material are written in 繁體中文
  language TEXT                             -- what the material is written in (tutor.reading.source_language); NULL = not told
);
"""


class MaterialError(Exception):
    def __init__(self, code: str, detail: str, extra: dict | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra or {}    # e.g. the material a duplicate file already is


PAGE_MARGIN_RE = re.compile(r"^(\d{1,4}|[ivxlc]{1,6})\b|\b(\d{1,4})$", re.I)   # running header/footer with a page number
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
# Word / Office bullets are glyphs of symbol fonts (Wingdings, Symbol) with private-use code points: at the start of a
# line they are a list marker, not lost text
PUA_BULLET_RE = re.compile(r"^[\ue000-\uf8ff]+\s+")
MARGIN_EDGE = 0.09          # top / bottom share of the page where running headers and footers sit
MARGIN_REPEAT_PAGES = 3     # an edge text (digits ignored) repeated on this many pages is a running header / footer
FOOTNOTE_SIZE = 0.88        # footnotes are set at most this share of the page's body font size …
FOOTNOTE_ZONE = 0.5         # … in the lower part of the page
FOOTNOTE_MARK_RE = re.compile(r"^([0-9]{1,3}|[*†‡§¶]{1,3})\s*(?=\S)")
FIGURE_CAPTION_RE = re.compile(r"^\s*(Figure|Fig\.|圖)\s*[\dA-Z一二三四五六七八九十]", re.I)
NOTE_END_RE = re.compile(r"[.?!。？！)）]\s*$")
FIGURE_CAPTION_GAP = 36     # points between a figure's drawing and its caption
FIGURE_MAX_HEIGHT = 0.8     # a drawing region taller than this share of the page is a page frame, not a figure


def _margin_key(text: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", "", text))


def _join_lines(lines: list[str]) -> str:
    """Lines of one block → one paragraph: a hyphen at a line end is a broken word ("criti-" + "cisms");
    CJK lines join without a space, Latin lines with one."""
    out = ""
    for ln in lines:
        if not out:
            out = ln
        elif out.endswith("-") and ln[:1].islower():
            out = out[:-1] + ln
        elif CJK_RE.search(out[-1:]) or CJK_RE.search(ln[:1]):
            out += ln
        else:
            out += " " + ln
    return re.sub(r"[ \t]+", " ", out).strip()


def _page_text_blocks(page, math=None) -> list[tuple[str, tuple[float, float, float, float], bool, float, list[str], list[str]]]:
    """(joined text, bbox, at the top / bottom edge, dominant font size, its lines, the plain lines) of the page's text
    blocks, in PyMuPDF's reading order. With `math` (app.pdfdoc.math.PageMath of the page, same flags) the text has
    glyphs read through their names and formulas as $…$ / $$…$$; a block whose glyphs all went into a formula of
    another block is left out. The plain lines are PyMuPDF's own characters, for the decisions on the layout (margins,
    footnote marks, captions), the same with or without formulas."""
    import pymupdf

    height = page.rect.height or 1
    out = []
    if math is None:
        sizes: dict[int, float] = {}
        for b in page.get_text("dict", flags=pymupdf.TEXT_DEHYPHENATE)["blocks"]:
            counts: dict[float, int] = {}
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    counts[round(sp["size"], 1)] = counts.get(round(sp["size"], 1), 0) + len(sp["text"].strip())
            if counts:
                sizes[b["number"]] = max(counts, key=counts.get)
        for x0, y0, x1, y1, text, number, kind in page.get_text("blocks", flags=pymupdf.TEXT_DEHYPHENATE):
            if kind != 0:   # block type: 0 text, 1 image
                continue
            lines = [PUA_BULLET_RE.sub("• ", ln.strip()) for ln in text.split("\n") if ln.strip()]
            if lines:
                out.append((_join_lines(lines), (x0, y0, x1, y1), y1 < height * MARGIN_EDGE or y0 > height * (1 - MARGIN_EDGE),
                            sizes.get(number, 0.0), lines, lines))
        return out
    for bi, b in enumerate(math.blocks):
        plain = [PUA_BULLET_RE.sub("• ", "".join(g.c for g in ln["glyphs"]).strip()) for ln in b["lines"]]
        plain = [ln for ln in plain if ln]
        lines = [PUA_BULLET_RE.sub("• ", ln.strip()) for ln in math.block_text(bi) if ln.strip()]
        if lines:
            counts = {}
            for ln in b["lines"]:
                for g in ln["glyphs"]:
                    if g.c.strip():
                        counts[round(g.size, 1)] = counts.get(round(g.size, 1), 0) + 1
            x0, y0, x1, y1 = b["bbox"]
            out.append((_join_lines(lines), (x0, y0, x1, y1), y1 < height * MARGIN_EDGE or y0 > height * (1 - MARGIN_EDGE),
                        max(counts, key=counts.get) if counts else 0.0, lines, plain))
    return out


def _plain(block) -> str:
    return _join_lines(block[5])


def _figure_regions(page, blocks) -> list[tuple[float, float, float, float]]:
    """Drawings and images that a figure caption ("Figure 3.1", "圖 2") sits right below or above: the figure's body,
    whose labels are part of the picture, not of the text. A table's lines are content and never count."""
    rects = [tuple(r) for r in page.cluster_drawings()] + [tuple(im["bbox"]) for im in page.get_image_info()]
    captions = [b[1] for b in blocks if FIGURE_CAPTION_RE.match(_plain(b))]
    out = []
    for r in rects:
        if r[3] - r[1] > FIGURE_MAX_HEIGHT * page.rect.height:
            continue
        for c in captions:
            side = (r[1] - FIGURE_CAPTION_GAP <= c[1] and c[1] - r[3] <= FIGURE_CAPTION_GAP and c[1] >= r[3] - 2) or \
                   (c[3] <= r[1] + 2 and r[1] - c[3] <= FIGURE_CAPTION_GAP)
            if side and min(r[2], c[2]) > max(r[0], c[0]):
                out.append(r)
                break
    return out


def _inside(b: tuple, r: tuple, pad: float = 3.0) -> bool:
    return b[0] >= r[0] - pad and b[1] >= r[1] - pad and b[2] <= r[2] + pad and b[3] <= r[3] + pad


def _is_note(block, body: float) -> bool:
    """A short footnote at the page's bottom edge looks like a footer ("6 Section 17.4 …"): small type, a note mark
    and a closing sentence tell it apart (a footer carries no sentence end)."""
    text = _plain(block)
    return bool(block[2] and body and block[3] <= FOOTNOTE_SIZE * body and FOOTNOTE_MARK_RE.match(text)
                and NOTE_END_RE.search(text))


def _body_size(blocks) -> float:
    counts: dict[float, int] = {}
    for text, _, edge, size, *_ in blocks:
        if not edge and size:
            counts[size] = counts.get(size, 0) + len(text)
    return max(counts, key=counts.get) if counts else 0.0


def _note_text(text: str, mark: str) -> str:
    """The note's text without its mark, which the formula reader may have written as a superscript ($^{1}$)."""
    sup = r"(?:\{\})?\^\{?" + re.escape(mark) + r"\}?"
    m = re.match(r"^\s*\$" + sup + r"\s*(.*?)\$\s*", text)   # "${}^{1}\mathrm{We}$ use …": the mark and a word set with it
    if m:
        word = re.fullmatch(r"\\mathrm\{([^}]*)\}", m.group(1).strip())
        rest = word.group(1) if word else (f"${m.group(1).strip()}$" if m.group(1).strip() else "")
        return (rest + " " + text[m.end():]).strip()
    m = re.match(r"^\s*" + re.escape(mark) + r"\s*", text)
    return text[m.end():].strip() if m else text.strip()


def pdf_page_math(doc, page, fonts=None):
    """The page's glyphs and formulas (app.pdfdoc.math) for text version 2; None on a page without TeX or math fonts,
    whose text is PyMuPDF's own."""
    import pymupdf

    from .pdfdoc.math import FontNames, page_math, wants_math
    if not wants_math(doc, page.number):
        return None
    return page_math(page, fonts or FontNames(doc), pymupdf.TEXT_DEHYPHENATE)


def pdf_running_margins(doc) -> frozenset[str]:
    """Edge texts (digits ignored) that repeat on MARGIN_REPEAT_PAGES or more pages: running headers / footers such as
    a chapter title, which carry no page number of their own."""
    if len(doc) <= MARGIN_REPEAT_PAGES:
        return frozenset()
    seen: dict[str, int] = {}
    for page in doc:
        for key in {_margin_key(t) for t, _, edge, *_ in _page_text_blocks(page) if edge and len(t) <= 80}:
            seen[key] = seen.get(key, 0) + 1
    return frozenset(k for k, n in seen.items() if n >= MARGIN_REPEAT_PAGES)


def pdf_page_paragraphs(page, margins: frozenset[str] = frozenset(), math=None) -> list[tuple[str, tuple[float, float, float, float]]]:
    """(paragraph text, bbox) of one PyMuPDF page, in reading order. A PyMuPDF text block is a paragraph in typeset
    books, but only a line in PDFs whose line spacing is wide (Word exports): the reading pipeline rebuilds
    paragraphs from the text itself (reading/source.py), so this layer does not guess. Hyphenated line breaks are
    joined, Office bullet glyphs become "• ", and edge blocks are dropped when they carry a page number or are one of
    the document's running `margins` (pdf_running_margins). The importer and the evidence extractor both use this, so
    a block's (page, paragraph index) locator points at the same region in both. `math` (pdf_page_math, text version 2)
    gives the text with formulas as LaTeX; the layout decisions (margins, footnotes, figures) are made on the plain
    text either way.

    Footnotes (the run of small-type blocks closing the page, opened by a note mark) are written as Markdown
    footnotes "[^1]: …" after the text and kept out of its sentence flow (reading/source.py); text inside a figure
    (labels of a drawing next to its caption) is dropped, the caption kept."""
    blocks = _page_text_blocks(page, math)
    body = _body_size(blocks)
    blocks = [b for b in blocks if _is_note(b, body) or not (b[2] and len(_plain(b)) <= 80 and (
        PAGE_MARGIN_RE.search(_plain(b)) or _margin_key(_plain(b)) in margins))]
    figures = _figure_regions(page, blocks)
    blocks = [b for b in blocks if FIGURE_CAPTION_RE.match(_plain(b)) or not any(_inside(b[1], r) for r in figures)]
    height = page.rect.height or 1
    n = len(blocks)
    while n and body and blocks[n - 1][3] <= FOOTNOTE_SIZE * body and blocks[n - 1][1][1] > FOOTNOTE_ZONE * height:
        n -= 1
    while n < len(blocks) and not FOOTNOTE_MARK_RE.match(_plain(blocks[n])):
        n += 1   # small type that no mark opens is not a footnote area (a caption, a small final line)
    out: list[tuple[str, tuple[float, float, float, float]]] = [(b[0], b[1]) for b in blocks[:n]]
    notes: list[list[tuple[str, str]]] = []   # per note: (plain line, text line)
    boxes: list[tuple[float, float, float, float]] = []
    for _, bbox, _, _, lines, plain in blocks[n:]:
        pairs = list(zip(plain, lines)) if len(plain) == len(lines) else [(_join_lines(plain), _join_lines(lines))]
        for k, (pl, line) in enumerate(pairs):
            # a mark opens a note at a block's start, or on a line after a closed sentence (two notes set in one block)
            opens = FOOTNOTE_MARK_RE.match(pl) and (k == 0 or NOTE_END_RE.search(pairs[k - 1][0]))
            if opens or not notes:
                notes.append([(pl, line)])
                boxes.append(bbox)
            else:   # the note before it goes on
                notes[-1].append((pl, line))
                b = boxes[-1]
                boxes[-1] = (min(b[0], bbox[0]), min(b[1], bbox[1]), max(b[2], bbox[2]), max(b[3], bbox[3]))
    for note, bbox in zip(notes, boxes):
        mark = FOOTNOTE_MARK_RE.match(note[0][0]).group(1)
        out.append((f"[^{mark}]: {_note_text(_join_lines([t for _, t in note]), mark)}", bbox))
    return out


def _pdf_pages_pymupdf(data: bytes) -> list[str]:
    """One string per page, paragraphs separated by blank lines (see pdf_page_paragraphs)."""
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001 — pymupdf raises plain exceptions for broken files
        raise MaterialError("unreadable", f"無法讀取這個 PDF：{e}") from e
    from .pdfdoc.math import FontNames
    margins = pdf_running_margins(doc)
    fonts = FontNames(doc)
    out = []
    for page in doc:
        try:
            math = pdf_page_math(doc, page, fonts)
        except Exception:  # noqa: BLE001 — a page the formula reader cannot take keeps PyMuPDF's text
            log.warning("formula reading failed on page %s", page.number + 1, exc_info=True)
            math = None
        out.append("\n\n".join(t for t, _ in pdf_page_paragraphs(page, margins, math)))
    return out


def _pdf_pages_pypdf(data: bytes) -> list[str]:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        return [(p.extract_text() or "").strip() for p in PdfReader(io.BytesIO(data)).pages]
    except (PyPdfError, ValueError) as e:
        raise MaterialError("unreadable", f"無法讀取這個 PDF：{e}") from e


def pdf_page_count(data: bytes) -> int:
    """Pages of a PDF, without reading its text (a damaged file is refused here, as extract_units would)."""
    try:
        import pymupdf
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            n = doc.page_count
    except Exception as e:  # noqa: BLE001
        raise MaterialError("unreadable", f"無法開啟這份 PDF：{e}") from e
    if n == 0:
        raise MaterialError("empty", "這份 PDF 沒有頁面。")
    return n


def extract_units(kind: str, data: bytes) -> list[str]:
    if kind == "pdf":
        try:
            import pymupdf  # noqa: F401
            pages = _pdf_pages_pymupdf(data)
        except ImportError:
            pages = _pdf_pages_pypdf(data)
        if not any(pages):
            raise MaterialError("no_text", "這個 PDF 沒有可擷取的文字（可能是掃描檔）。請改用含文字層的 PDF，或先轉成 txt。")
        return pages
    for enc in ("utf-8-sig", "utf-8", "big5", "gb18030"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise MaterialError("unreadable", "無法判斷檔案的文字編碼，請另存為 UTF-8。")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise MaterialError("no_text", "檔案是空的。")
    return lines


class MaterialStore:
    def __init__(self, db: Database, files_dir: Path | None = None, library_dir: Path | None = None):
        """`library_dir`: the person's own book folder (~/.mentora); every import leaves a copy of the file there,
        named after its content (app.library)."""
        from .library import Library
        self.db = db
        self.library = Library(db, library_dir or config.LIBRARY_DIR)
        self.files_dir = files_dir or (Path(db.path).parent / "materials" if db.path != ":memory:" else config.DATA_DIR / "materials")
        with db.conn() as c:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(materials)")}
            if "text_version" not in cols:   # materials before it
                c.execute("ALTER TABLE materials ADD COLUMN text_version INTEGER NOT NULL DEFAULT 1")
            if "status" not in cols:         # materials before the background text reading: all read
                c.execute("ALTER TABLE materials ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'")
                c.execute("ALTER TABLE materials ADD COLUMN status_detail TEXT")
            if "translate_zh" not in cols:
                c.execute("ALTER TABLE materials ADD COLUMN translate_zh INTEGER NOT NULL DEFAULT 0")
            if "language" not in cols:   # materials imported before: their language is read from their text once
                from .tutor.reading import source_language
                c.execute("ALTER TABLE materials ADD COLUMN language TEXT")
                for mid, units in c.execute("SELECT id, units FROM materials").fetchall():
                    c.execute("UPDATE materials SET language=? WHERE id=?",
                              (source_language("\n".join(json.loads(units))), mid))

    @property
    def library_dir(self) -> Path:
        return self.library.folder

    @library_dir.setter
    def library_dir(self, folder: Path) -> None:
        self.library.folder = folder

    # -- categories ---------------------------------------------------------
    def list_categories(self) -> list[dict]:
        with self.db.conn() as c:
            return [dict(r) for r in c.execute("SELECT id, name, position FROM categories ORDER BY position, created_at")]

    def create_category(self, name: str) -> dict:
        cid = new_id()
        with self.db.conn() as c:
            pos = c.execute("SELECT COALESCE(MAX(position),0)+1 FROM categories").fetchone()[0]
            c.execute("INSERT INTO categories VALUES (?,?,?,?)", (cid, name.strip(), pos, now_iso()))
        return {"id": cid, "name": name.strip(), "position": pos}

    def rename_category(self, cid: str, name: str) -> dict:
        with self.db.conn() as c:
            if c.execute("UPDATE categories SET name=? WHERE id=?", (name.strip(), cid)).rowcount == 0:
                raise MaterialError("not_found", "找不到這個分類")
            return dict(c.execute("SELECT id, name, position FROM categories WHERE id=?", (cid,)).fetchone())

    def delete_category(self, cid: str) -> None:
        """Materials inside are kept and become uncategorised."""
        with self.db.conn() as c:
            c.execute("UPDATE materials SET category_id=NULL WHERE category_id=?", (cid,))
            c.execute("DELETE FROM categories WHERE id=?", (cid,))

    def _check_category(self, cid: str | None) -> None:
        if cid and not any(x["id"] == cid for x in self.list_categories()):
            raise MaterialError("not_found", "找不到這個分類")

    # -- materials ----------------------------------------------------------
    # status: "ready" (its text is read), "extracting" (a PDF whose text is being read in the background, see
    # app.material_extract) or "failed" (status_detail says why); the file itself is in place in every case
    META = ("id, category_id, title, filename, kind, unit, unit_count, created_at, text_version, status, status_detail, "
            "translate_zh, language")

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        d["translate_zh"] = bool(d["translate_zh"])
        return d

    def list(self) -> list[dict]:
        lib = self.library.all()
        with self.db.conn() as c:
            rows = [self._row(r) for r in c.execute(f"SELECT {self.META} FROM materials ORDER BY created_at")]
        for r in rows:
            r["alias"] = (lib.get(r["id"]) or {}).get("alias")
        return rows

    def adopt_library(self) -> int:
        """Give the materials imported before content names theirs (app.library.Library.adopt)."""
        return self.library.adopt(self.list(), lambda m: self.files_dir / f"{m['id']}.{m['kind']}")

    def add(self, filename: str, data: bytes, category_id: str | None, *, original_path: str | None = None,
            defer: bool = False) -> dict:
        """Import a file. The same file (same content, whatever its name) is refused as a duplicate, naming the
        material it already is. `original_path`: where the file came from, when known (the home bookshelf).
        `defer`: a PDF is recorded with its page count only (status "extracting") and its text read afterwards
        (app.material_extract); the caller schedules that."""
        from .library import sha256
        kind = KINDS.get(Path(filename).suffix.lower())
        if not kind:
            raise MaterialError("unsupported", "目前只支援 pdf、md、txt。")
        self._check_category(category_id)
        digest = sha256(data)
        same = self.library.by_hash(digest)
        if same:
            try:
                existing = self.get(same, with_units=False)
            except MaterialError:
                existing = None
            if existing:
                raise MaterialError("duplicate", f"「{existing['title']}」已經在書庫中，不會重複匯入。",
                                    {"material": existing})
        deferred = defer and kind == "pdf"
        units = [""] * pdf_page_count(data) if deferred else extract_units(kind, data)
        from .tutor.reading import source_language
        mid = new_id()
        self.files_dir.mkdir(parents=True, exist_ok=True)
        (self.files_dir / f"{mid}.{kind}").write_bytes(data)
        with self.db.conn() as c:
            c.execute("INSERT INTO materials (id, category_id, title, filename, kind, unit, unit_count, units, created_at, "
                      "text_version, status, language) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (mid, category_id or None, Path(filename).stem, filename, kind,
                       "page" if kind == "pdf" else "line", len(units), json.dumps(units, ensure_ascii=False), now_iso(),
                       TEXT_VERSION if kind == "pdf" else 1, "extracting" if deferred else "ready",
                       None if deferred else source_language("\n".join(units))))
        self.library.keep(mid, filename, data, digest, original_path)
        return self.get(mid, with_units=False)

    # -- the text, read in the background ------------------------------------------------------
    def extracting(self) -> list[str]:
        """Materials whose text is still to be read (also those left half done when the app stopped)."""
        with self.db.conn() as c:
            return [r["id"] for r in c.execute("SELECT id FROM materials WHERE status='extracting' ORDER BY created_at")]

    def set_units(self, mid: str, units: list[str]) -> None:
        with self.db.conn() as c:
            from .tutor.reading import source_language
            c.execute("UPDATE materials SET units=?, unit_count=?, status='ready', status_detail=NULL, language=? WHERE id=?",
                      (json.dumps(units, ensure_ascii=False), len(units), source_language("\n".join(units)), mid))

    def set_failed(self, mid: str, detail: str) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE materials SET status='failed', status_detail=? WHERE id=?", (detail[:300], mid))

    @staticmethod
    def require_ready(m: dict) -> None:
        """What needs the text (a question, 導讀, the companion's study) waits until it is read."""
        status = m.get("status") or "ready"
        if status == "extracting":
            raise MaterialError("preparing", f"正在整理「{m['title']}」的文字，完成後就能使用。", {"retryable": True})
        if status == "failed":
            raise MaterialError("unreadable", f"讀不到「{m['title']}」的文字：{m.get('status_detail') or '未知原因'}")

    def reorder_categories(self, ids: list[str]) -> list[dict]:
        """Categories in the given order; any not named keep their relative order after them."""
        current = [c["id"] for c in self.list_categories()]
        order = [i for i in ids if i in current] + [i for i in current if i not in ids]
        with self.db.conn() as c:
            for pos, cid in enumerate(order, 1):
                c.execute("UPDATE categories SET position=? WHERE id=?", (pos, cid))
        return self.list_categories()

    def get(self, mid: str, *, with_units: bool = True) -> dict:
        with self.db.conn() as c:
            row = c.execute(f"SELECT {self.META}{', units' if with_units else ''} FROM materials WHERE id=?", (mid,)).fetchone()
        if not row:
            raise MaterialError("not_found", "找不到這份教材")
        d = self._row(row)
        if with_units:
            d["units"] = json.loads(d["units"])
        lib = self.library.get(mid)
        d["alias"] = (lib or {}).get("alias")
        d["library"] = lib    # the copy in the book folder and what it stands for (app.library)
        return d

    def update(self, mid: str, *, title: str | None, category_id: str | None, set_category: bool,
               translate_zh: bool | None = None) -> dict:
        self.get(mid, with_units=False)
        with self.db.conn() as c:
            if translate_zh is not None:
                c.execute("UPDATE materials SET translate_zh=? WHERE id=?", (int(translate_zh), mid))
            if title is not None and title.strip():
                c.execute("UPDATE materials SET title=? WHERE id=?", (title.strip(), mid))
            if set_category:
                self._check_category(category_id)
                c.execute("UPDATE materials SET category_id=? WHERE id=?", (category_id or None, mid))
        return self.get(mid, with_units=False)

    def delete(self, mid: str) -> None:
        m = self.get(mid, with_units=False)
        with self.db.conn() as c:
            c.execute("DELETE FROM materials WHERE id=?", (mid,))
        self.library.remove(mid)      # its copy in the book folder goes too
        (self.files_dir / f"{mid}.{m['kind']}").unlink(missing_ok=True)
        (self.files_dir / "covers" / f"{mid}.png").unlink(missing_ok=True)
        import shutil
        shutil.rmtree(self.files_dir / "pages" / mid, ignore_errors=True)

    def file_path(self, mid: str) -> tuple[Path, dict]:
        m = self.get(mid, with_units=False)
        return self.files_dir / f"{mid}.{m['kind']}", m

    def cover(self, mid: str) -> Path | None:
        """First page of an imported PDF as a 240px-wide PNG (cached next to the file); None for text materials."""
        path, m = self.file_path(mid)
        if m["kind"] != "pdf" or not path.exists():
            return None
        out = self.files_dir / "covers" / f"{mid}.png"
        if out.exists():
            return out
        try:
            import pymupdf
            with pymupdf.open(path) as doc:
                page = doc[0]
                zoom = 240 / max(page.rect.width, 1)
                out.parent.mkdir(parents=True, exist_ok=True)
                page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(str(out))
            return out
        except Exception:
            return None

    PAGE_WIDTHS = (480, 720, 960, 1280, 1600, 2000)

    def pdf_of(self, mid: str) -> Path | None:
        path, m = self.file_path(mid)
        return path if m["kind"] == "pdf" and path.exists() else None

    def page_target(self, mid: str, n: int, width: int) -> tuple[Path, Path] | None:
        """(the PDF, where page n about `width` pixels wide is cached) — widths snap to a few sizes. The rendering
        itself runs in app.pdf_worker (MuPDF must not run on two threads of the server)."""
        pdf = self.pdf_of(mid)
        if not pdf or not 1 <= n <= self.get(mid, with_units=False)["unit_count"]:
            return None
        w = next((x for x in self.PAGE_WIDTHS if x >= width), self.PAGE_WIDTHS[-1])
        return pdf, self.files_dir / "pages" / mid / f"{n}-{w}.png"

    def sizes_cache(self, mid: str) -> Path:
        return self.files_dir / "pages" / mid / "sizes.json"

    def info_cache(self, mid: str) -> Path:
        """Where the document info (app.pdfdoc.document_info) of a PDF is cached; the version is in the name."""
        from .pdfdoc.layer import LAYER_VERSION
        return self.files_dir / "pages" / mid / f"info-v{LAYER_VERSION}.json"

    def layer_cache(self, mid: str, n: int) -> Path | None:
        """Where page n's layer (app.pdfdoc.page_layer) is cached; None when the material has no page n."""
        from .pdfdoc.layer import LAYER_VERSION
        if not self.pdf_of(mid) or not 1 <= n <= self.get(mid, with_units=False)["unit_count"]:
            return None
        return self.files_dir / "pages" / mid / f"layer-v{LAYER_VERSION}-{n}.json"

    def range_units(self, mid: str, start: int, end: int) -> tuple[dict, list[str]]:
        """Raw units start..end (1-based, inclusive) plus the material row without its units."""
        m = self.get(mid)
        self.require_ready(m)
        if not (1 <= start <= end <= m["unit_count"]):
            raise MaterialError("bad_range", f"範圍需在 1–{m['unit_count']} 之間，且起點不大於終點。")
        return m, m.pop("units")[start - 1:end]

    def range_text(self, mid: str, start: int, end: int) -> tuple[dict, str]:
        """Text of units start..end, each page labelled so the model can cite it."""
        m, units = self.range_units(mid, start, end)
        if m["unit"] == "page":
            text = "\n\n".join(f"[第 {start + i} 頁]\n{u}" for i, u in enumerate(units))
        else:
            text = "\n".join(units)
        if not text.strip():
            raise MaterialError("no_text", "這個範圍裡沒有文字。")
        return m, text
