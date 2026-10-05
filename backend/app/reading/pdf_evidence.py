"""PDF symbol and media evidence (R5).

A PDF text layer loses what notation depends on: bold vs italic letters, sub- and superscripts, symbol fonts whose
glyphs have no Unicode mapping. For each extracted paragraph block this module keeps, from the original file:

- page number and bbox (the same paragraph the importer produced: materials.pdf_page_paragraphs);
- the text spans with font name, size and PyMuPDF style flags (1 superscript, 2 italic, 16 bold);
- what the spans suggest: bold / italic single-letter tokens, superscripts, math or symbol fonts, glyphs the text
  layer lost (control / private-use characters);
- images on the page next to the paragraph (a caption-like paragraph near an image is a figure reference).

These are clues, not verdicts. Font names and flags never decide how a symbol is written; only a rendered region
read by a vision-capable model of the session (or the author's own LaTeX, for md sources) can confirm notation.
Without that, a unit with symbols stays `unverified` and is not published as a checked guide."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

EVIDENCE_VERSION = "pdf-evidence-v1"
MATH_FONT_RE = re.compile(r"CM(MI|SY|EX|BX|MIB|BSY)|MSBM|MSAM|Symbol|Math|STIX|Euler|EUFM|EUSM|rsfs|mathptmx|TeXGyre.*Math", re.I)
CAPTION_RE = re.compile(r"^\s*(Figure|Fig\.|Table|圖|表)\s*[\dA-Z一二三四五六七八九十.\-:：]", re.I)
MAX_SPANS = 80


def _lost(text: str) -> bool:
    return any(unicodedata.category(ch) in ("Cc", "Co", "Cn") and ch not in "\n\t" for ch in text)


def _overlap(a, b) -> float:
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0) / max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))


def _is_bullet_span(line_spans: list[dict], k: int) -> bool:
    """A span of private-use glyphs that opens its line is an Office list bullet (Wingdings / Symbol), not lost text."""
    t = line_spans[k]["text"].strip()
    return bool(t) and all("" <= ch <= "" for ch in t) and not "".join(s["text"] for s in line_spans[:k]).strip()


def page_evidence(page, margins: frozenset[str] = frozenset()) -> list[dict]:
    """Evidence of every paragraph of one page, in the importer's paragraph order."""
    from ..materials import pdf_page_paragraphs

    paras = pdf_page_paragraphs(page, margins)
    spans = [s for b in page.get_text("dict")["blocks"] if b.get("type") == 0
             for ln in b["lines"] for k, s in enumerate(ln["spans"]) if s["text"].strip() and not _is_bullet_span(ln["spans"], k)]
    images = [tuple(round(v, 1) for v in im["bbox"]) for im in page.get_image_info()]
    out = []
    for k, (text, bbox) in enumerate(paras, 1):
        own = [s for s in spans if _overlap(s["bbox"], bbox) > 0.5]
        bold = sorted({s["text"].strip() for s in own if s["flags"] & 16 and len(s["text"].strip()) <= 3})
        italic = sorted({s["text"].strip() for s in own if s["flags"] & 2 and len(s["text"].strip()) <= 3})
        sup = sorted({s["text"].strip() for s in own if s["flags"] & 1})
        fonts = sorted({s["font"] for s in own})
        near = [im for im in images if abs(im[1] - bbox[3]) < 60 or abs(bbox[1] - im[3]) < 60 or _overlap(bbox, im) > 0]
        out.append({
            "version": EVIDENCE_VERSION, "page": page.number + 1, "para": k, "bbox": [round(v, 1) for v in bbox],
            "spans": [{"t": s["text"], "font": s["font"], "flags": s["flags"], "size": round(s["size"], 1)} for s in own][:MAX_SPANS],
            "fonts": fonts, "math_fonts": [f for f in fonts if MATH_FONT_RE.search(f)],
            "bold_tokens": bold, "italic_tokens": italic, "superscripts": sup,
            "lost_glyphs": any(_lost(s["text"]) for s in own) or _lost(text),
            "images_near": near, "caption": bool(near and CAPTION_RE.match(text)),
        })
    return out


def _union(boxes: list[list[float]]) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def merge_evidence(evs: list[dict]) -> dict:
    """One block rebuilt from several layout paragraphs (lines of a Word export, a sentence across a page break):
    clues are pooled; `page` / `bbox` are the first page's region and `regions` lists one region per page."""
    if len(evs) == 1:
        return evs[0]
    pages: dict[int, list[list[float]]] = {}
    for e in evs:
        pages.setdefault(e["page"], []).append(e["bbox"])
    regions = [{"page": p, "bbox": [round(v, 1) for v in _union(bs)]} for p, bs in pages.items()]
    return {
        "version": EVIDENCE_VERSION, "page": regions[0]["page"], "para": evs[0]["para"], "bbox": regions[0]["bbox"],
        "regions": regions, "spans": [s for e in evs for s in e["spans"]][:MAX_SPANS],
        **{k: sorted({x for e in evs for x in e[k]}) for k in ("fonts", "math_fonts", "bold_tokens", "italic_tokens", "superscripts")},
        "lost_glyphs": any(e["lost_glyphs"] for e in evs),
        "images_near": [list(x) for x in dict.fromkeys(tuple(im) for e in evs for im in e["images_near"])],
        "caption": any(e["caption"] for e in evs),
    }


def attach_evidence(pdf_path: str | Path, blocks: list) -> list:
    """Copies of the PDF blocks with `meta["pdf"]` filled from the original file (blocks of other kinds unchanged).
    A block rebuilt from several layout paragraphs (`meta["pdf_paras"]`, [[page, para], …]) pools their evidence;
    a block cut from a long paragraph shares its paragraph's evidence."""
    import pymupdf

    from ..materials import pdf_running_margins

    doc = pymupdf.open(str(pdf_path))
    margins = pdf_running_margins(doc)
    cache: dict[int, list[dict]] = {}

    def para_ev(page_no: int, para: int) -> dict | None:
        if page_no not in cache:
            cache[page_no] = page_evidence(doc[page_no - 1], margins) if 1 <= page_no <= len(doc) else []
        evs = cache[page_no]
        return evs[para - 1] if 0 < para <= len(evs) else None

    out = []
    for b in blocks:
        refs = b.meta.get("pdf_paras") or ([[b.locator.start, b.locator.para]] if b.locator.para is not None else [])
        if b.locator.unit != "page" or not refs:
            out.append(b)
            continue
        evs = [e for e in (para_ev(p, k) for p, k in refs) if e]
        out.append(b.model_copy(update={"meta": {**b.meta, "pdf": merge_evidence(evs)}}) if evs else b)
    return out


def render_region(pdf_path: str | Path, page: int, bbox: list[float], zoom: float = 3.0, pad: float = 6.0) -> bytes:
    """PNG of one region of a page (only that region enters the model's context, never the whole book)."""
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    p = doc[page - 1]
    r = pymupdf.Rect(bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad) & p.rect
    return p.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=r).tobytes("png")


def unit_symbol_evidence(blocks: list, pdf_path: str | None, has_math_text: bool) -> dict | None:
    """What a unit's notation check can rely on. None: the unit has no symbol-like content. Otherwise:
    - needs_visual: regions to render and the font clues (a vision-capable model can confirm them);
    - unavailable: symbols are there but the original PDF (or its evidence) is missing."""
    evs = [b.meta.get("pdf") for b in blocks if b.meta.get("pdf")]
    clues = {"bold_tokens": sorted({t for e in evs for t in e["bold_tokens"]}),
             "italic_tokens": sorted({t for e in evs for t in e["italic_tokens"]}),
             "superscripts": sorted({t for e in evs for t in e["superscripts"]}),
             "math_fonts": sorted({f for e in evs for f in e["math_fonts"]}),
             "lost_glyphs": any(e["lost_glyphs"] for e in evs)}
    # superscripts alone are often footnote marks and short bold words are often emphasis ("AI"): they are recorded as
    # clues but only math-like evidence makes a unit symbol-bearing
    bold_letters = [t for t in clues["bold_tokens"] if len(t) == 1 and t.isalpha()]
    symbolic = has_math_text or clues["math_fonts"] or clues["lost_glyphs"] or bold_letters
    if not symbolic:
        return None
    regions = [r for e in evs for r in e.get("regions") or [{"page": e["page"], "bbox": e["bbox"]}]]
    if not pdf_path or not regions:
        return {"status": "unavailable", "version": EVIDENCE_VERSION, "clues": clues, "regions": regions,
                "reason": "這一段有數學符號，但找不到原始 PDF 的版面證據，無法確認符號的字型（粗體／斜體／上下標）"}
    return {"status": "needs_visual", "version": EVIDENCE_VERSION, "clues": clues, "regions": regions,
            "pdf_path": str(pdf_path),
            "reason": "PDF 文字層無法確認符號的字型（粗體／斜體／上下標）" + ("，且部分符號在文字層遺失" if clues["lost_glyphs"] else "")
                      + "。可改用支援影像的模型（例如 Ollama 的 qwen3-vl）重試，或改用以 LaTeX 寫成的 md／txt 教材"}


def png_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]
