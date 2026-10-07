"""From index entries to books on the shelf: keyword scoring (no model), PDF / text metadata, and cover images.

Scoring is what makes the shelf fast: typing in the home page's box and every scout run rank the whole index with
plain string matching in a few milliseconds; the model only chooses among the top of that list."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path

from .index import Entry, ShelfIndex, series_files

BOOKISH_FOLDER = re.compile(r"book|書|ebook|epub|paper|論文|reading|閱讀|讀書|library|kindle|textbook|教材|課本|講義|notes?|筆記", re.I)
_CJK = re.compile(r"[㐀-鿿]")


def keywords_of(text: str) -> list[str]:
    """Terms of a free-text query: Latin words of 2+ letters; Chinese runs as they are plus their 2-char pieces,
    so 強化學習 also finds 強化式學習 and 學習 alone does not drown everything."""
    out: list[str] = []
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]{1,}|[㐀-鿿]+|\d{2,}", text.lower()):
        if _CJK.match(tok) and len(tok) > 2:
            out.append(tok)
            out += [tok[i:i + 2] for i in range(len(tok) - 1)]
        else:
            out.append(tok)
    stop = {"the", "and", "for", "of", "to", "in", "an", "a", "on", "with", "想讀", "一些", "關於", "有關", "書籍", "東西"}
    return list(dict.fromkeys(t for t in out if t not in stop))


_SHORT_LATIN = re.compile(r"^[a-z0-9+#.\-]{1,4}$")


def hit(k: str, text: str) -> bool:
    """Substring match, except that a short Latin term must be a whole word: "rl" is not in "world", "art" not in
    "start". `k` and `text` are lower-case."""
    if _SHORT_LATIN.match(k):
        return re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", text) is not None
    return k in text


def weights(keywords: list[str]) -> dict[str, float]:
    """A 2-char piece cut from a longer Chinese term counts half: it is a fallback for a looser wording."""
    return {k: .5 if _CJK.match(k) and len(k) == 2 and any(len(o) > 2 and k in o for o in keywords) else 1.0 for k in keywords}


def score(e: Entry, weighted: dict[str, float], now: float, info: dict | None = None) -> float:
    info = info or {}
    name, folder, path = f"{e.name} {info.get('title') or ''}".lower(), e.folder.lower(), e.path.lower()
    head = info.get("head") or ""
    s = 0.0
    for k, w in weighted.items():
        if hit(k, name):
            s += 3 * w
        elif hit(k, folder):
            s += 1.5 * w
        elif hit(k, head):   # the opening pages: a book about it often says so before its file name does
            s += 1.0 * w
        elif hit(k, path):
            s += .5 * w
    if weighted and s == 0:
        return 0.0
    age_days = max(0.0, (now - e.mtime) / 86400)
    s += .6 if e.kind == "pdf" else 0
    s += .3 if e.size > 2_000_000 else 0
    s += .8 * math.exp(-age_days / 120)
    s += 1.0 if BOOKISH_FOLDER.search(folder) else 0
    pages = info.get("pages") or 0   # a book has pages; a receipt, a form or a report has a handful
    s += 1.2 if pages >= 80 else .6 if pages >= 25 else -.8 if 0 < pages < 8 else 0
    s += 1.4 if e.parts >= 20 else .6 if e.parts else 0   # a serial someone keeps collecting is being read
    return max(s, .01)


def rank(entries: list[Entry], keywords: list[str], *, exclude: set[str], limit: int,
         known: dict[str, dict] | None = None) -> list[tuple[Entry, float]]:
    """`known`: what was read from inside the files (title, pages), when it has been; the title is matched like the
    file name."""
    now, known = time.time(), known or {}
    weighted = weights(keywords)
    scored = [(e, score(e, weighted, now, known.get(e.id))) for e in entries if not _imported(e, exclude)]
    scored = [x for x in scored if x[1] > 0]
    scored.sort(key=lambda x: -x[1])
    # one folder must not fill the whole shelf (a downloaded course with 40 lecture PDFs)
    out, per_folder = [], {}
    for e, s in scored:
        if per_folder.get(e.folder, 0) >= 3:
            continue
        per_folder[e.folder] = per_folder.get(e.folder, 0) + 1
        out.append((e, s))
        if len(out) >= limit:
            break
    return out


def _imported(e: Entry, exclude: set[str]) -> bool:
    return Path(e.path).name.lower() in exclude or e.name.lower() in exclude


META_VERSION = 2   # bump when what is read from a file changes (2: the opening text, `head`)
HEAD_CHARS = 1500


class BookMeta:
    """Title / author / pages / cover aspect / opening text per entry, cached in meta.json and invalidated by the
    file's mtime. The opening text is what lets the shelf search without a model reach past file names."""

    def __init__(self, index: ShelfIndex):
        self.index = index
        self.file = index.dir / "meta.json"
        self.covers = index.dir / "covers"
        self._lock = threading.Lock()
        try:
            self.cache: dict[str, dict] = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}

    def get(self, e: Entry) -> dict:
        c = self.cache.get(e.id)
        if c and c.get("mtime") == e.mtime and c.get("v") == META_VERSION:
            return c
        if e.cloud:   # never trigger a download just to show a title
            return {}
        if e.parts:   # a series is titled by its chapters' shared name; its first chapter is its opening
            first = series_files(e.path)[:1]
            c = {"mtime": e.mtime, "v": META_VERSION, "head": _text_meta(first[0]).get("head") if first else None}
        else:
            c = {"mtime": e.mtime, "v": META_VERSION, **(_pdf_meta(e.path) if e.kind == "pdf" else _text_meta(e.path))}
        with self._lock:
            self.cache[e.id] = c
        return c

    def known(self) -> dict[str, dict]:
        with self._lock:
            return dict(self.cache)

    def enrich(self, entries: list[Entry], budget_s: float = 6.0) -> None:
        """Read the metadata of files not seen yet within a time budget (runs in a thread): PDFs first, whose page
        count tells a book from a receipt, then text files; newest first within each."""
        t0 = time.monotonic()
        for e in sorted((e for e in entries if not e.cloud), key=lambda e: (e.kind != "pdf", -e.mtime)):
            if time.monotonic() - t0 > budget_s:
                break
            self.get(e)
        self.save()

    def keep_only(self, ids: set[str]) -> None:
        """Forget files outside the current scope: their metadata, opening text and cover images."""
        with self._lock:
            self.cache = {k: v for k, v in self.cache.items() if k in ids}
        if self.covers.is_dir():
            for f in self.covers.iterdir():
                if f.name.split("-", 1)[0] not in ids:
                    f.unlink(missing_ok=True)
        self.save()

    def save(self) -> None:
        with self._lock:
            self.index.dir.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")

    def cover(self, e: Entry) -> Path | None:
        """First page of a PDF as a PNG, 320px wide. Text files get a drawn cover in the browser instead."""
        if e.kind != "pdf" or e.cloud:
            return None
        out = self.covers / f"{e.id}-{int(e.mtime)}.png"
        if out.exists():
            return out
        try:
            import pymupdf
            with pymupdf.open(e.path) as doc:
                page = doc[0]
                zoom = 320 / max(page.rect.width, 1)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                self.covers.mkdir(parents=True, exist_ok=True)
                pix.save(str(out))
            return out
        except Exception:
            return None

    def book(self, e: Entry, **extra) -> dict:
        m = self.get(e)
        return {"id": e.id, "title": m.get("title") or e.name, "author": m.get("author") or "", "kind": e.kind,
                "pages": m.get("pages"), "parts": e.parts or None, "folder": e.folder, "aspect": m.get("aspect") or .7,
            "cloud": e.cloud, **extra}


def _good_title(t: str | None) -> str | None:
    if not t:
        return None
    t = re.sub(r"^Microsoft (Word|PowerPoint) - ", "", t.strip())
    t = re.sub(r"[*_`]{1,3}([^*_`]+)[*_`]{1,3}", r"\1", t).strip()   # Markdown emphasis in a heading
    t = re.sub(r"\.(pdf|docx?|pptx?|tex)$", "", t, flags=re.I).strip()
    if len(t) < 4 or re.fullmatch(r"(untitled|title|document|slide\s*\d*|\d+|[0-9a-f\-]{16,})", t, re.I):
        return None
    return t[:120]


def _pdf_meta(path: str) -> dict:
    try:
        import pymupdf
        pymupdf.TOOLS.mupdf_display_errors(False)   # a damaged PDF is skipped quietly, not logged per object
        with pymupdf.open(path) as doc:
            md = doc.metadata or {}
            r = doc[0].rect if doc.page_count else None
            head = " ".join(doc[i].get_text() for i in range(min(3, doc.page_count)))
            return {"title": _good_title(md.get("title")), "author": (md.get("author") or "").strip()[:60] or None,
                    "pages": doc.page_count, "aspect": round(r.width / r.height, 3) if r and r.height else None,
                    "head": _squash(head)}
    except Exception:
        return {}


def _text_meta(path: str) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return {}
    m = re.search(r"^#\s+(.+)$", head, re.M) or re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", head, re.M | re.I)
    return {"title": _good_title(m.group(1)) if m else None, "head": _squash(head)}


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()[:HEAD_CHARS]
