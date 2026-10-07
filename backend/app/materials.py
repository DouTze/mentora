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
# Word / Office bullets are glyphs of symbol fonts (Wingdings, Symbol) with private-use code points: at the start of a
# line they are a list marker, not lost text
PUA_BULLET_RE = re.compile(r"^[\ue000-\uf8ff]+\s+")
MARGIN_EDGE = 0.09          # top / bottom share of the page where running headers and footers sit
MARGIN_REPEAT_PAGES = 3     # an edge text (digits ignored) repeated on this many pages is a running header / footer


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


def _page_text_blocks(page) -> list[tuple[str, tuple[float, float, float, float], bool]]:
    """(joined text, bbox, at the top / bottom edge) of the page's text blocks, in PyMuPDF's reading order."""
    import pymupdf

    height = page.rect.height or 1
    out = []
    for x0, y0, x1, y1, text, *_rest in page.get_text("blocks", flags=pymupdf.TEXT_DEHYPHENATE):
        if _rest[1] != 0:   # block type: 0 text, 1 image
            continue
        lines = [PUA_BULLET_RE.sub("• ", ln.strip()) for ln in text.split("\n") if ln.strip()]
        if lines:
            out.append((_join_lines(lines), (x0, y0, x1, y1), y1 < height * MARGIN_EDGE or y0 > height * (1 - MARGIN_EDGE)))
    return out


def pdf_running_margins(doc) -> frozenset[str]:
    """Edge texts (digits ignored) that repeat on MARGIN_REPEAT_PAGES or more pages: running headers / footers such as
    a chapter title, which carry no page number of their own."""
    if len(doc) <= MARGIN_REPEAT_PAGES:
        return frozenset()
    seen: dict[str, int] = {}
    for page in doc:
        for key in {_margin_key(t) for t, _, edge in _page_text_blocks(page) if edge and len(t) <= 80}:
            seen[key] = seen.get(key, 0) + 1
    return frozenset(k for k, n in seen.items() if n >= MARGIN_REPEAT_PAGES)


def pdf_page_paragraphs(page, margins: frozenset[str] = frozenset()) -> list[tuple[str, tuple[float, float, float, float]]]:
    """(paragraph text, bbox) of one PyMuPDF page, in reading order. A PyMuPDF text block is a paragraph in typeset
    books, but only a line in PDFs whose line spacing is wide (Word exports): the reading pipeline rebuilds
    paragraphs from the text itself (reading/source.py), so this layer does not guess. Hyphenated line breaks are
    joined, Office bullet glyphs become "• ", and edge blocks are dropped when they carry a page number or are one of
    the document's running `margins` (pdf_running_margins). The importer and the evidence extractor both use this, so
    a block's (page, paragraph index) locator points at the same region in both."""
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    for text, bbox, edge in _page_text_blocks(page):
        if edge and len(text) <= 80 and (PAGE_MARGIN_RE.search(text) or _margin_key(text) in margins):
            continue
        out.append((text, bbox))
    return out


def _pdf_pages_pymupdf(data: bytes) -> list[str]:
    """One string per page, paragraphs separated by blank lines (see pdf_page_paragraphs)."""
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001 — pymupdf raises plain exceptions for broken files
        raise MaterialError("unreadable", f"無法讀取這個 PDF：{e}") from e
    margins = pdf_running_margins(doc)
    return ["\n\n".join(t for t, _ in pdf_page_paragraphs(page, margins)) for page in doc]


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
    def __init__(self, db: Database, files_dir: Path | None = None, library_dir: Path | None = None):
        """`library_dir`: the person's own book folder (~/.mentora); every import leaves a copy of the file there."""
        self.db = db
        self.library_dir = library_dir or config.LIBRARY_DIR
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
        self.keep_copy(filename, data)
        mid = new_id()
        self.files_dir.mkdir(parents=True, exist_ok=True)
        (self.files_dir / f"{mid}.{kind}").write_bytes(data)
        with self.db.conn() as c:
            c.execute("INSERT INTO materials VALUES (?,?,?,?,?,?,?,?,?)",
                      (mid, category_id or None, Path(filename).stem, filename, kind,
                       "page" if kind == "pdf" else "line", len(units), json.dumps(units, ensure_ascii=False), now_iso()))
        return self.get(mid, with_units=False)

    def keep_copy(self, filename: str, data: bytes) -> Path | None:
        """Put the file in the library folder under its own name: skipped when the same file is already there, given
        a " (2)" suffix when another file has that name. A failure (permissions, full disk) does not stop the import."""
        try:
            self.library_dir.mkdir(parents=True, exist_ok=True)
            stem, ext = Path(filename).stem, Path(filename).suffix
            target, n = self.library_dir / Path(filename).name, 2
            while target.exists():
                if target.stat().st_size == len(data) and target.read_bytes() == data:
                    return target
                target, n = self.library_dir / f"{stem} ({n}){ext}", n + 1
            target.write_bytes(data)
            return target
        except OSError:
            log.warning("could not copy %s into %s", filename, self.library_dir, exc_info=True)
            return None

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
