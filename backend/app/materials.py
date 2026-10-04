"""Imported materials (教材): pdf / md / txt, grouped into categories.

A material is split into addressable units so a study range can be exact:
pdf → pages, md/txt → lines. Unit numbers are 1-based, ranges are inclusive."""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

from . import config
from .db import Database, new_id, now_iso

KINDS = {".pdf": "pdf", ".md": "md", ".markdown": "md", ".txt": "txt"}
MAX_RANGE_CHARS = 60000  # per session; the range is read in parts of reading.PART_CHARS, so this bounds time, not context

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
  created_at TEXT NOT NULL
);
"""


class MaterialError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


PAGE_MARGIN_RE = re.compile(r"^(\d{1,4}|[ivxlc]{1,6})\b|\b(\d{1,4})$", re.I)   # running header/footer with a page number
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")


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


def pdf_page_paragraphs(page) -> list[tuple[str, tuple[float, float, float, float]]]:
    """(paragraph text, bbox) of one PyMuPDF page, in reading order. PyMuPDF's text blocks follow the page's layout
    objects, so a block is a paragraph (or a heading / caption). Hyphenated line breaks are joined; running headers
    and footers carrying a page number are dropped. The importer and the evidence extractor both use this, so a
    block's (page, paragraph index) locator points at the same region in both."""
    import pymupdf

    height = page.rect.height or 1
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    blocks = [b for b in page.get_text("blocks", flags=pymupdf.TEXT_DEHYPHENATE) if b[6] == 0]
    for i, (x0, y0, x1, y1, text, *_rest) in enumerate(blocks):
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if not lines:
            continue
        joined = _join_lines(lines)
        at_edge = y1 < height * 0.09 or y0 > height * 0.91
        if at_edge and len(joined) <= 80 and PAGE_MARGIN_RE.search(joined) and (i == 0 or i == len(blocks) - 1):
            continue
        out.append((joined, (x0, y0, x1, y1)))
    return out


def _pdf_pages_pymupdf(data: bytes) -> list[str]:
    """One string per page, paragraphs separated by blank lines (see pdf_page_paragraphs)."""
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001 — pymupdf raises plain exceptions for broken files
        raise MaterialError("unreadable", f"無法讀取這個 PDF：{e}") from e
    return ["\n\n".join(t for t, _ in pdf_page_paragraphs(page)) for page in doc]


def _pdf_pages_pypdf(data: bytes) -> list[str]:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        return [(p.extract_text() or "").strip() for p in PdfReader(io.BytesIO(data)).pages]
    except (PyPdfError, ValueError) as e:
        raise MaterialError("unreadable", f"無法讀取這個 PDF：{e}") from e


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
    def __init__(self, db: Database, files_dir: Path | None = None):
        self.db = db
        self.files_dir = files_dir or (Path(db.path).parent / "materials" if db.path != ":memory:" else config.DATA_DIR / "materials")
        with db.conn() as c:
            c.executescript(SCHEMA)

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
    META = "id, category_id, title, filename, kind, unit, unit_count, created_at"

    def list(self) -> list[dict]:
        with self.db.conn() as c:
            return [dict(r) for r in c.execute(f"SELECT {self.META} FROM materials ORDER BY created_at")]

    def add(self, filename: str, data: bytes, category_id: str | None) -> dict:
        kind = KINDS.get(Path(filename).suffix.lower())
        if not kind:
            raise MaterialError("unsupported", "目前只支援 pdf、md、txt。")
        self._check_category(category_id)
        units = extract_units(kind, data)
        mid = new_id()
        self.files_dir.mkdir(parents=True, exist_ok=True)
        (self.files_dir / f"{mid}.{kind}").write_bytes(data)
        with self.db.conn() as c:
            c.execute("INSERT INTO materials VALUES (?,?,?,?,?,?,?,?,?)",
                      (mid, category_id or None, Path(filename).stem, filename, kind,
                       "page" if kind == "pdf" else "line", len(units), json.dumps(units, ensure_ascii=False), now_iso()))
        return self.get(mid, with_units=False)

    def get(self, mid: str, *, with_units: bool = True) -> dict:
        with self.db.conn() as c:
            row = c.execute(f"SELECT {self.META}{', units' if with_units else ''} FROM materials WHERE id=?", (mid,)).fetchone()
        if not row:
            raise MaterialError("not_found", "找不到這份教材")
        d = dict(row)
        if with_units:
            d["units"] = json.loads(d["units"])
        return d

    def update(self, mid: str, *, title: str | None, category_id: str | None, set_category: bool) -> dict:
        self.get(mid, with_units=False)
        with self.db.conn() as c:
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
        (self.files_dir / f"{mid}.{m['kind']}").unlink(missing_ok=True)

    def file_path(self, mid: str) -> tuple[Path, dict]:
        m = self.get(mid, with_units=False)
        return self.files_dir / f"{mid}.{m['kind']}", m

    def range_units(self, mid: str, start: int, end: int) -> tuple[dict, list[str]]:
        """Raw units start..end (1-based, inclusive) plus the material row without its units."""
        m = self.get(mid)
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
        if len(text) > MAX_RANGE_CHARS:
            raise MaterialError("range_too_large", f"這個範圍有 {len(text)} 字，超過一次學習的上限 {MAX_RANGE_CHARS} 字。請縮小範圍。")
        return m, text
