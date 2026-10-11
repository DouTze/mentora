"""Formulas of a born-digital PDF page as LaTeX, by rules only.

The method follows the systems that read formulas from a PDF's own data instead of OCR (InftyReader, MaxTract,
SymbolScraper) and lays them out like DRACULAE:

1. Glyphs: every character with what it really is (texglyphs: the TeX glyph name from the embedded font, else its
   Unicode), its tight box (TEXT_ACCURATE_BBOXES, from the glyph outline), its baseline and font size.
2. Regions: on each line, words holding math glyphs (a math font, a math Unicode character, a raised or lowered
   script) form inline formulas; lines that are mostly math, stacked together, form a display formula (an equation
   number at the right margin becomes \\tag).
3. Layout: drawn horizontal rules make fractions, radicals' overlines and overlines; accents attach to the symbol
   under them; big operators and operator names take the symbols above / below them as limits; then the main
   baseline is found and the remaining symbols are superscripts or subscripts of the baseline symbol before them,
   each parsed again on its own baseline. Several baselines of full-size symbols are rows (cases with a left brace,
   otherwise gathered).

The result is a reading of the layout, not a proof of the notation: an unusual layout can come out wrong."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median

from .texglyphs import ACCENTS, BIG_OPERATORS, MATH_FONT, alphabet_of, charstring_names, glyph

# -- glyph identity ---------------------------------------------------------------------------------------------
_UNI2TEX: dict[str, str] | None = None
_TEXT_MODE = {"→": "\\rightarrow", "←": "\\leftarrow", "↔": "\\leftrightarrow", "×": "\\times", "·": "\\cdot",
              "′": "'", "″": "''", "−": "-", "√": "\\sqrt", "…": "\\ldots", "⋯": "\\cdots", "°": "^\\circ", "∗": "*",
              "÷": "\\div", "±": "\\pm", "¬": "\\neg", "|": "|", "‖": "\\|", "{": "\\{", "}": "\\}", "\\": "\\backslash",
              "%": "\\%", "#": "\\#", "&": "\\&", "_": "\\_", "$": "\\$", "~": "\\sim", "^": "\\hat{}"}


def uni_tex(c: str) -> str:
    """LaTeX (math mode) of a character: pylatexenc's Unicode table, its \\ensuremath{…} unwrapped."""
    global _UNI2TEX
    if _UNI2TEX is None:
        from pylatexenc.latexencode import get_builtin_uni2latex_dict
        _UNI2TEX = {}
        for k, v in get_builtin_uni2latex_dict().items():
            m = re.fullmatch(r"\\ensuremath\{(.*)\}", v)
            if m:
                _UNI2TEX[chr(k)] = m.group(1)
        _UNI2TEX.update(_TEXT_MODE)
    if len(c) == 1 and c.isascii() and (c.isalnum() or c in "+-=<>()[]/,.:;!?'|*"):
        return c
    return _UNI2TEX.get(c, c)


_MATH_RANGES = ((0x0391, 0x03C9), (0x03D0, 0x03F6), (0x2032, 0x2037), (0x2100, 0x214F), (0x2190, 0x21FF),
                (0x2200, 0x22FF), (0x2308, 0x230B), (0x27C0, 0x27EF), (0x27F0, 0x27FF), (0x2980, 0x29FF),
                (0x2A00, 0x2AFF), (0x1D400, 0x1D7FF))


def math_char(c: str) -> bool:
    return bool(c) and (c in "±×÷∞" or any(a <= ord(c[0]) <= b for a, b in _MATH_RANGES))


class FontNames:
    """Glyph names of a document's Type 1 fonts (by font xref), read once per font."""

    def __init__(self, doc):
        self.doc = doc
        self._names: dict[int, list[str] | None] = {}
        self._fonts: dict[int, dict[str, list[int]]] = {}

    def names(self, xref: int) -> list[str] | None:
        if xref not in self._names:
            try:
                _name, ext, _typ, buf = self.doc.extract_font(xref)
                self._names[xref] = charstring_names(buf) if ext in ("pfa", "pfb", "n/a") and buf else None
            except Exception:  # noqa: BLE001 — a font MuPDF cannot hand out keeps its Unicode
                self._names[xref] = None
        return self._names[xref]

    def page_fonts(self, pno: int) -> dict[str, list[int]]:
        """Font base name (no subset tag) → its xrefs on the page."""
        if pno not in self._fonts:
            out: dict[str, list[int]] = {}
            for xref, _ext, _typ, base, *_ in self.doc.get_page_fonts(pno):
                out.setdefault((base or "").split("+")[-1], []).append(xref)
            self._fonts[pno] = out
        return self._fonts[pno]

    def span_names(self, pno: int, font: str, chars) -> list[str] | None:
        """The glyph names of one span: of the page's font with that base name whose names agree best with the
        span's plain letters (two subsets of one font share the base name)."""
        best, score = None, -1
        for xref in self.page_fonts(pno).get(font, []):
            ns = self.names(xref)
            if not ns:
                continue
            # plain ASCII characters MuPDF reads right are the evidence: letters, digits ("one" → "1"), punctuation
            ok = sum(1 for u, gid, *_ in chars if 0 <= gid < len(ns) and 32 < u < 127
                     and identify(ns[gid], chr(u))[0] == chr(u))
            fit = all(0 <= gid < len(ns) for _u, gid, *_ in chars)
            if fit and ok > score:
                best, score = ns, ok
        return best


def identify(name: str | None, c: str, font: str = "") -> tuple[str, str]:
    """(text, LaTeX) of a glyph from its name in its font (preferred) or its character."""
    if name:
        if (known := glyph(name, font)):
            return known
        if len(name) == 1 and name.isascii():
            return name, name
        m = re.fullmatch(r"uni([0-9A-Fa-f]{4,6})", name)
        if m:
            ch = chr(int(m.group(1), 16))
            return ch, uni_tex(ch)
    if "\ufb00" <= c <= "\ufb06":            # a ligature of a font without names: its letters
        import unicodedata
        return unicodedata.normalize("NFKC", c), unicodedata.normalize("NFKC", c)
    return c, uni_tex(c)


TEX_FONT = re.compile(r"^(CM|LM|EU|MSAM|MSBM|RSFS|SF|EC|TC)", re.I)


def wants_math(doc, pno: int) -> bool:
    """Whether a page may hold formulas (only those pages pay for the glyph and formula analysis): TeX or math fonts,
    italic fonts (variables in documents not made by TeX), or math characters in its text."""
    names = [(f[3] or "").split("+")[-1] for f in doc.get_page_fonts(pno)]
    if any(MATH_FONT.match(n) or TEX_FONT.match(n) or re.search(r"Italic|Oblique|-It\b|,Italic", n, re.I) for n in names):
        return True
    return any(math_char(c) for c in doc[pno].get_text("text"))


# -- glyphs of a page -------------------------------------------------------------------------------------------
@dataclass
class Glyph:
    c: str                      # MuPDF's character (what the text layer had)
    text: str                   # what it is (corrected through the glyph name)
    tex: str
    rect: list[float]           # tight box, page space (MuPDF's unrotated, y down)
    oy: float                   # baseline
    size: float
    font: str
    math: bool = False

    @property
    def space(self) -> bool:
        return not self.c.strip()


def _key(x: float, y: float) -> tuple[int, int]:
    return round(x * 4), round(y * 4)


def read_lines(page, fonts: FontNames, flags: int) -> list[dict]:
    """The page's text blocks (`flags` as the caller's own extraction, so blocks and lines are the same) with every
    character as a Glyph: {"bbox", "lines": [{"bbox", "glyphs"}]}. Image blocks are left out."""
    import pymupdf

    pno = page.number
    trace: dict[tuple[int, int], tuple[str | None, str]] = {}
    for sp in page.get_texttrace():
        names = fonts.span_names(pno, sp["font"], sp["chars"])
        for u, gid, origin, _bbox in sp["chars"]:
            name = names[gid] if names and 0 <= gid < len(names) else None
            trace[_key(*origin)] = (name, sp["font"])
    tight: dict[tuple[int, int], list[float]] = {}
    for b in page.get_text("rawdict", flags=pymupdf.TEXT_ACCURATE_BBOXES | pymupdf.TEXT_PRESERVE_LIGATURES)["blocks"]:
        for ln in b.get("lines", []):
            for s in ln["spans"]:
                for ch in s["chars"]:
                    tight[_key(*ch["origin"])] = list(ch["bbox"])
    out = []
    # ligatures kept as one glyph each, so every character meets its own glyph in the trace (the name spells it out)
    for b in page.get_text("rawdict", flags=flags | pymupdf.TEXT_PRESERVE_LIGATURES)["blocks"]:
        if b.get("type") != 0:
            continue
        lines = []
        for ln in b["lines"]:
            gs: list[Glyph] = []
            for s in ln["spans"]:
                for ch in s["chars"]:
                    k = _key(*ch["origin"])
                    name, font = trace.get(k, (None, s["font"]))
                    named = bool(name) and name not in (".notdef", "space")
                    c = ch["c"] if ch["c"].strip() or not named else "\u2063"   # a glyph MuPDF reads as a space (← in cmsy)
                    text, tex = identify(name, ch["c"], font) if c.strip() else (c, " ")
                    r = tight.get(k) or list(ch["bbox"])
                    if r[2] - r[0] <= 0 or r[3] - r[1] <= 0:
                        r = list(ch["bbox"])
                    gs.append(Glyph(c, text, tex, r, ch["origin"][1], s["size"], font,
                                    bool(c.strip()) and (bool(MATH_FONT.match(font)) or math_char(text[:1]))))
            lines.append({"bbox": list(ln["bbox"]), "glyphs": gs})
        out.append({"bbox": list(b["bbox"]), "lines": lines})
    return out


def page_rules(page) -> list[list[float]]:
    """Thin horizontal strokes and filled bars (fraction bars, radical overlines, overlines) in page space."""
    out = []
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        return out
    for d in drawings:
        width = d.get("width") or 0.4
        for it in d.get("items", []):
            if it[0] == "l":
                p, q = it[1], it[2]
                if abs(p.y - q.y) < 0.6 and abs(q.x - p.x) > 2:
                    y = (p.y + q.y) / 2
                    out.append([min(p.x, q.x), y - width / 2, max(p.x, q.x), y + width / 2])
            elif it[0] == "re":
                r = it[1]
                if 0 < r.height < 1.6 and r.width > 2 and r.width > r.height * 4:
                    out.append([r.x0, r.y0, r.x1, r.y1])
    return out


# -- layout to LaTeX --------------------------------------------------------------------------------------------
@dataclass
class Sym:
    tex: str
    rect: list[float]
    oy: float | None            # baseline; None: a glyph that hangs from its origin (cmex, radicals): not a guide
    size: float
    font: str = ""
    kind: str = "glyph"         # glyph | word | group
    bold: bool = False
    key: tuple[int, int, int] | None = None     # the glyph it was made from (block, line, glyph)

    @property
    def cx(self) -> float:
        return (self.rect[0] + self.rect[2]) / 2

    @property
    def cy(self) -> float:
        return (self.rect[1] + self.rect[3]) / 2

    @property
    def w(self) -> float:
        return self.rect[2] - self.rect[0]


def _union(rs) -> list[float]:
    rs = list(rs)
    return [min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)]


OPERATOR_NAMES = {"max", "min", "log", "ln", "exp", "sin", "cos", "tan", "cot", "sec", "csc", "lim", "sup", "inf",
                  "det", "arg", "Pr", "deg", "dim", "ker", "gcd", "mod", "sinh", "cosh", "tanh", "liminf", "limsup", "lg"}
LIMIT_OPS = {"\\max", "\\min", "\\lim", "\\sup", "\\inf", "\\liminf", "\\limsup", "\\det", "\\Pr", "\\gcd"}
DELIMS = {"(", ")", "[", "]", "\\{", "\\}", "|", "\\|", "\\langle", "\\rangle", "\\lfloor", "\\rfloor", "\\lceil",
          "\\rceil", "/", "\\backslash"}
_UPRIGHT = re.compile(r"^(CMR|CMBX|CMSS|CMTT|CMSL|LMRoman|LMSans|Times|Helvetica|Arial)", re.I)
_BOLD = re.compile(r"(CMBX|CMB\d|Bold|BX)", re.I)


def sym_of(g: Glyph) -> Sym:
    hangs = g.font.upper().startswith(("CMEX", "LMEX")) or g.tex == "\\sqrt"
    tex = g.tex
    alpha = alphabet_of(g.font)
    if alpha and len(tex) == 1 and tex.isalpha():
        tex = f"{alpha}{{{tex}}}"
    bold = bool(_BOLD.search(g.font)) and len(tex) == 1 and tex.isalnum()
    if g.font.upper().startswith("CMMIB") and (len(tex) == 1 and tex.isalpha() or tex.startswith("\\")):
        tex, bold = f"\\boldsymbol{{{tex}}}", False
    return Sym(tex, list(g.rect), None if hangs else g.oy, g.size, g.font, bold=bold)


def _merge_pieces(syms: list[Sym]) -> list[Sym]:
    """Big delimiters drawn in pieces (top, middle, bottom, extension) → one delimiter; empty pieces dropped."""
    syms = [s for s in syms if s.tex]
    out: list[Sym] = []
    for s in sorted(syms, key=lambda s: (s.rect[0], s.rect[1])):
        prev = next((p for p in reversed(out[-4:]) if p.tex == s.tex and p.tex in DELIMS and p.oy is None and s.oy is None
                     and min(p.rect[2], s.rect[2]) - max(p.rect[0], s.rect[0]) > 0.5 * min(p.w, s.w)), None)
        if prev and s.rect[1] - prev.rect[3] < 0.5 * s.size:
            prev.rect = _union([prev.rect, s.rect])
            continue
        out.append(s)
    return out


def _words(syms: list[Sym]) -> list[Sym]:
    """Upright letters set close together on one baseline → an operator name (\\max) or a word (\\mathrm / \\text)."""
    def letter(s: Sym) -> bool:
        return s.kind == "glyph" and len(s.tex) == 1 and s.tex.isalpha() and bool(_UPRIGHT.match(s.font)) and not s.bold

    # letters are grouped among themselves, by baseline then left to right: a limit set under a name ("a" under
    # "argmax") lies between its letters in x but not on their baseline
    others = [s for s in syms if not letter(s)]
    syms = sorted((s for s in syms if letter(s)), key=lambda s: (round((s.oy or 0) / max(s.size * 0.15, 0.5)), s.rect[0]))
    out: list[Sym] = list(others)
    run: list[Sym] = []

    def flush() -> None:
        if not run:
            return
        word = "".join(s.tex for s in run)
        if len(run) == 1:
            out.append(run[0])
        else:
            if word in OPERATOR_NAMES:
                tex = "\\" + word
            elif word in ("argmax", "argmin"):
                tex = f"\\operatorname*{{{word}}}"
            elif len(word) <= 3:
                tex = f"\\mathrm{{{word}}}"
            else:
                tex = f"\\text{{{word}}}"
            out.append(Sym(tex, _union(s.rect for s in run), run[0].oy, run[0].size, run[0].font, kind="word"))
        run.clear()

    for s in syms:
        if run and abs((s.oy or 0) - (run[-1].oy or 0)) < 0.15 * s.size and -0.1 * s.size < s.rect[0] - run[-1].rect[2] < 0.25 * s.size:
            run.append(s)
            continue
        flush()
        run.append(s)
    flush()
    return out


def _tok(s: Sym) -> str:
    return f"\\mathbf{{{s.tex}}}" if s.bold else s.tex


def _join(tokens: list[str]) -> str:
    out = ""
    for t in tokens:
        if not t:
            continue
        if out and re.search(r"\\[A-Za-z]+$", out) and re.match(r"[A-Za-z0-9]", t):
            out += " "
        out += t
    return out


def _inside(r: list[float], box: list[float], pad: float = 0.5) -> bool:
    return r[0] >= box[0] - pad and r[2] <= box[2] + pad and r[1] >= box[1] - pad and r[3] <= box[3] + pad


def _stack(cands: list[Sym], edge: float, up: bool, ref: float) -> list[Sym]:
    """The symbols stacked directly above (`up`) or below a rule at y = `edge`: those touching it, then the symbols
    set on them (a superscript, a nested line), never type larger than the touching ones (the text line above an
    inline fraction is body size, the numerator script size)."""
    pool = [s for s in cands if (s.rect[3] <= edge + 0.35 * s.size if up else s.rect[1] >= edge - 0.35 * s.size)]

    def dist(s: Sym) -> float:
        return (edge - s.rect[3]) if up else (s.rect[1] - edge)

    out = [s for s in pool if dist(s) <= 0.55 * s.size]
    if not out:
        return []
    cap = 1.05 * max(s.size for s in out)
    grown = True
    while grown:
        grown = False
        for s in pool:
            if any(s is o for o in out) or s.size > cap:
                continue
            for o in out:
                vgap = (o.rect[1] - s.rect[3]) if up else (s.rect[1] - o.rect[3])
                hov = min(s.rect[2], o.rect[2]) - max(s.rect[0], o.rect[0])
                if vgap <= 0.45 * max(s.size, o.size) and hov > -0.25 * s.size:
                    out.append(s)
                    grown = True
                    break
    return out


def to_latex(syms: list[Sym], rules: list[list[float]]) -> str:
    """LaTeX of a set of symbols (and the rules among them)."""
    tex = _parse(_words(_merge_pieces(syms)), rules)
    tex = re.sub(r"(\\cdot){3}", r"\\cdots ", tex)
    return re.sub(r"\.\.\.", r"\\ldots ", tex).replace(" }", "}").strip()


def _parse(syms: list[Sym], rules: list[list[float]]) -> str:
    if not syms:
        return ""
    syms = list(syms)
    ref = max(s.size for s in syms)
    box = _union(s.rect for s in syms)
    rules = [r for r in rules if r[0] < box[2] and r[2] > box[0] and box[1] - ref < r[1] < box[3] + ref]
    used: set[int] = set()

    # 1. rules, widest first: radicals, fractions, over- / underlines
    for ri in sorted(range(len(rules)), key=lambda i: -(rules[i][2] - rules[i][0])):
        if ri in used:
            continue
        r = rules[ri]
        y = (r[1] + r[3]) / 2
        tol = 0.05 * ref
        inside = [s for s in syms if r[0] - tol <= s.cx <= r[2] + tol and s.rect[0] >= r[0] - 0.3 * s.size
                  and s.rect[2] <= r[2] + 0.3 * s.size]
        rad = next((s for s in syms if s.tex == "\\sqrt" and abs(s.rect[2] - r[0]) < 0.6 * s.size
                    and abs(s.rect[1] - r[1]) < 0.6 * s.size), None)
        if rad:
            body = [s for s in inside if s is not rad and s.cy > y and s.rect[1] < rad.rect[3]]
            index = [s for s in syms if s is not rad and s not in body and s.rect[2] <= rad.cx + 0.2 * rad.size
                     and s.rect[3] <= rad.cy and s.rect[0] >= rad.rect[0] - 1.5 * rad.size]
            inner = [rules[j] for j in range(len(rules)) if j != ri and j not in used and body
                     and _inside(rules[j], _union(s.rect for s in body), 1.0)]
            used.add(ri)
            used.update(rules.index(x) for x in inner)
            tex = "\\sqrt" + (f"[{_parse(index, [])}]" if index else "") + f"{{{_parse(body, inner)}}}"
            members = [rad, *body, *index]
            oy = median([s.oy for s in body if s.oy is not None] or [rad.rect[3] - 0.2 * rad.size])
            syms = [s for s in syms if s not in members] + [Sym(tex, _union([*(s.rect for s in members), r]), oy, ref, kind="group")]
            continue
        above, below = _stack(inside, r[1], True, ref), _stack(inside, r[3], False, ref)
        if not above and not below:
            continue
        inner_of = lambda part: [rules[j] for j in range(len(rules)) if j != ri and j not in used and part  # noqa: E731
                                 and _inside(rules[j], _union(s.rect for s in part), 1.0)]
        ia, ib = inner_of(above), inner_of(below)
        used.add(ri)
        used.update(rules.index(x) for x in ia + ib)
        if above and below:
            tex = f"\\frac{{{_parse(above, ia)}}}{{{_parse(below, ib)}}}"
            oy = y + 0.25 * ref                                        # the math axis sits 0.25 em above the baseline
        elif below:
            tex, oy = f"\\overline{{{_parse(below, ib)}}}", median([s.oy for s in below if s.oy is not None] or [r[3] + ref])
        else:
            tex, oy = f"\\underline{{{_parse(above, ia)}}}", median([s.oy for s in above if s.oy is not None] or [r[1]])
        members = above + below
        syms = [s for s in syms if s not in members] + [Sym(tex, _union([*(s.rect for s in members), r]), oy,
                                                            max(s.size for s in members), kind="group")]

    # 2. a dot over "=" is \\doteq; accents sit over the symbol under them
    for eq in [s for s in syms if s.tex == "="]:
        dot = next((s for s in syms if s.tex in (".", "\\cdot") and s.rect[3] <= eq.rect[1] + 0.05 * eq.size
                    and eq.rect[0] <= s.cx <= eq.rect[2] and eq.rect[1] - s.rect[3] < 0.5 * eq.size), None)
        if dot:
            syms = [s for s in syms if s is not dot and s is not eq] + [
                Sym("\\doteq", _union([eq.rect, dot.rect]), eq.oy, eq.size, eq.font, kind="group")]
    for a in [s for s in syms if s.tex in ACCENTS and s.kind == "glyph"]:
        under = [s for s in syms if s is not a and s.cy > a.cy and s.rect[1] - a.rect[3] < 0.5 * ref
                 and min(s.rect[2], a.rect[2]) - max(s.rect[0], a.rect[0]) > 0.3 * min(s.w, a.w)]
        if not under:
            continue
        base = max(under, key=lambda s: min(s.rect[2], a.rect[2]) - max(s.rect[0], a.rect[0]))
        syms = [s for s in syms if s is not a and s is not base] + [
            Sym(f"{a.tex}{{{_tok(base)}}}", _union([a.rect, base.rect]), base.oy, base.size, base.font, kind="group")]

    # 3. limits above / below big operators and operator names
    for op in sorted([s for s in syms if s.tex in BIG_OPERATORS or s.tex in LIMIT_OPS or s.tex.startswith("\\operatorname*")],
                     key=lambda s: -s.size):
        if op not in syms:
            continue
        span = (op.rect[0] - 0.35 * op.size, op.rect[2] + 0.35 * op.size)
        near = [s for s in syms if s is not op and span[0] <= s.cx <= span[1] and s.size < 0.9 * op.size]   # limits are set small
        upper = [s for s in near if s.rect[3] <= op.rect[1] + 0.15 * op.size and op.rect[1] - s.rect[3] < 1.1 * op.size]
        lower = [s for s in near if s.rect[1] >= op.rect[3] - 0.15 * op.size and s.rect[1] - op.rect[3] < 1.1 * op.size]
        if op.tex == "\\int":      # an integral's limits sit to its right as scripts
            upper = lower = []
        if not upper and not lower:
            continue
        tex = op.tex + (f"_{{{_parse(lower, [])}}}" if lower else "") + (f"^{{{_parse(upper, [])}}}" if upper else "")
        members = [op, *upper, *lower]
        syms = [s for s in syms if s not in members] + [Sym(tex, _union(s.rect for s in members), op.oy, op.size, kind="group")]

    # 4. rows, then the baseline and its scripts
    rows = _rows(syms, ref)
    if len(rows) > 1:
        return _rows_tex(rows, ref)
    return _baseline(syms, ref)


def _rows(syms: list[Sym], ref: float) -> list[list[Sym]]:
    """Several baselines of full-size symbols far apart: the rows of a multi-line formula."""
    full = sorted({round(s.oy, 1) for s in syms if s.oy is not None and s.size >= 0.8 * ref})
    if len(full) < 2:
        return [syms]
    bands: list[list[float]] = [[full[0]]]
    for y in full[1:]:
        if y - bands[-1][-1] > 1.1 * ref:
            bands.append([y])
        else:
            bands[-1].append(y)
    if len(bands) < 2:
        return [syms]
    centers = [median(b) for b in bands]
    rows: list[list[Sym]] = [[] for _ in bands]
    tall: list[Sym] = []
    for s in syms:
        if s.oy is None and s.rect[1] <= centers[0] - 0.5 * ref and s.rect[3] >= centers[-1] - 0.2 * ref:
            tall.append(s)                                 # a brace or bar running along all rows
            continue
        y = s.oy if s.oy is not None else s.cy + 0.3 * ref
        rows[min(range(len(centers)), key=lambda i: abs(centers[i] - y))].append(s)
    if tall:
        rows[0] = rows[0] + tall
    return [r for r in rows if r]


def _rows_tex(rows: list[list[Sym]], ref: float) -> str:
    brace = next((s for s in rows[0] if s.tex == "\\{" and s.oy is None), None)
    if brace:
        before = [s for s in rows[0] if s.rect[2] <= brace.rect[0] + 0.1]
        rows = [[s for s in r if s is not brace and s not in before] for r in rows]
        lines = []
        for r in rows:
            r = sorted(r, key=lambda s: s.rect[0])
            cut = next((i for i in range(1, len(r)) if r[i].rect[0] - r[i - 1].rect[2] > 1.2 * ref), None)
            lines.append(_baseline(r, ref) if cut is None else f"{_baseline(r[:cut], ref)} & {_baseline(r[cut:], ref)}")
        return _join([_baseline(before, ref), "\\begin{cases}" + " \\\\ ".join(lines) + "\\end{cases}"])
    texts = [_baseline(sorted(r, key=lambda s: s.rect[0]), ref) for r in rows]
    rel = re.compile(r"^(=|\\doteq|\\leq|\\geq|<|>|\\approx|\\equiv|\\sim|\\propto|\\in|\\neq|\\Rightarrow|\\to|\\rightarrow)")
    if all(rel.match(t) for t in texts[1:]):
        first = texts[0]
        m = re.search(r"(=|\\doteq|\\leq|\\geq|\\approx|\\equiv)", first)
        if m:
            lines = [first[:m.start()] + "&" + first[m.start():]] + ["&" + t for t in texts[1:]]
            return "\\begin{aligned}" + " \\\\ ".join(lines) + "\\end{aligned}"
    return "\\begin{gathered}" + " \\\\ ".join(texts) + "\\end{gathered}"


def _baseline(syms: list[Sym], ref: float) -> str:
    if not syms:
        return ""
    ref = max(s.size for s in syms)
    guides = [s.oy for s in syms if s.oy is not None and s.size >= 0.8 * ref] or [s.oy for s in syms if s.oy is not None]
    if not guides:
        return _join([_tok(s) for s in sorted(syms, key=lambda s: s.rect[0])])
    base = median(guides)
    syms = sorted(syms, key=lambda s: s.rect[0])

    def on_base(s: Sym) -> bool:
        if s.oy is None:
            return True
        if s.size >= 0.8 * ref:
            return abs(s.oy - base) <= 0.2 * ref
        return abs(s.oy - base) <= 0.04 * ref        # small type exactly on the baseline (not a script)

    line = [s for s in syms if on_base(s)]
    if not line:
        line = [syms[0]]
    scripts: dict[int, tuple[list[Sym], list[Sym]]] = {id(s): ([], []) for s in line}
    pre: tuple[list[Sym], list[Sym]] = ([], [])
    last: dict[int, tuple[list[Sym], Sym]] = {}     # per owner: the script group written last and its last symbol
    for s in syms:
        if any(s is b for b in line):
            continue
        owner = next((b for b in reversed(line) if b.rect[0] <= s.rect[0] + 0.1 * s.size), None)
        sup, sub = scripts[id(owner)] if owner else pre
        prev = last.get(id(owner))
        if prev and s.size <= prev[1].size * 0.9 and -0.1 * s.size <= s.rect[0] - prev[1].rect[2] < 0.3 * s.size and s.oy is not None \
                and prev[1].oy is not None and abs(s.oy - base) < abs(prev[1].oy - base) + 0.3 * ref and \
                ((s.oy < base) != (prev[0] is sup)):
            group = prev[0]                           # a script of the script (a prime on a subscript) stays with it
        else:
            group = sup if s.oy < base else sub
        group.append(s)
        last[id(owner)] = (group, s)

    def scripted(tok: str, sup: list[Sym], sub: list[Sym]) -> str:
        out = tok
        if sub:
            out += f"_{{{_parse(sub, [])}}}"
        if sup:
            s = _parse(sup, [])
            out += "'" * s.count("\\prime") if re.fullmatch(r"(\\prime)+", s) else f"^{{{s}}}"
        return out

    def spaced(k: int) -> bool:
        """A word of text in the formula ("for all") keeps the spaces set around it."""
        a, b = line[k - 1], line[k]
        return any(re.match(r"\\(text|mathrm)\{", s.tex) for s in (a, b)) and b.rect[0] - a.rect[2] >= 0.2 * ref

    toks = [scripted("{}", *pre)] if pre[0] or pre[1] else []
    toks += [("\\ " if k and spaced(k) else "") + scripted(_tok(b), *scripts[id(b)]) for k, b in enumerate(line)]
    return _join(toks)


# -- regions --------------------------------------------------------------------------------------------------------
TAG_RE = re.compile(r"^\((\d+(?:\.\d+)*[a-z]?)\)$")
BRIDGE = set("0123456789=+-−()[]{}/|,.:;<>!'*·×")
DISPLAY_WORDS = 0.25   # a display line is mostly math: words of text in it take at most this share of its glyphs


@dataclass
class Region:
    """One formula: where it is, its LaTeX, the glyphs it replaces (block, line, glyph) and where its markup goes
    (`anchor`: before that glyph; a glyph index equal to the line's length means its end)."""
    latex: str
    display: bool
    rect: list[float]
    members: list[tuple[int, int, int]] = field(default_factory=list)
    tag: str | None = None
    anchor: tuple[int, int, int] = (0, 0, 0)

    def markup(self) -> str:
        if not self.display:
            return f"${self.latex}$"
        tag = f" \\tag{{{self.tag}}}" if self.tag else ""
        return f"$${self.latex}{tag}$$"


def _words_of(gs: list[Glyph]) -> list[list[int]]:
    """Words of a line: split at spaces and at word-sized gaps (TeX sets spaces as gaps, not characters)."""
    out: list[list[int]] = []
    cur: list[int] = []
    for i, g in enumerate(gs):
        if g.space:
            if cur:
                out.append(cur)
            cur = []
            continue
        if cur and g.text:
            p = gs[cur[-1]]
            if g.rect[0] - p.rect[2] > 0.22 * max(g.size, p.size):
                out.append(cur)
                cur = []
        cur.append(i)
    if cur:
        out.append(cur)
    return out


def _main_size(gs: list[Glyph]) -> float:
    sizes = [g.size for g in gs if not g.space]
    return median(sizes) if sizes else 10.0


@dataclass
class _Line:
    bi: int
    li: int
    gs: list[Glyph]
    runs: list[list[int]]
    tag: list[int] | None
    share: float
    kind: str                   # strong (mostly math) | weak (only brackets, digits, punctuation) | text
    box: list[float]
    size: float
    span: list[float] | None = None     # the whole line, glyphs a fraction took included


def _line_parts(gs: list[Glyph]) -> tuple[list[list[int]], list[int] | None, float, bool]:
    """(math runs as glyph indices, the trailing equation number's indices, share of the line's glyphs in math or
    bridging them, whether every word is a bracket / digit / punctuation)."""
    words = _words_of(gs)
    if not words:
        return [], None, 0.0, False
    size = _main_size(gs)
    base = median([gs[i].oy for w in words for i in w if gs[i].size >= 0.9 * size] or [gs[words[0][0]].oy])
    tag = None
    last = words[-1]
    if len(words) > 1 and TAG_RE.match("".join(gs[i].text for i in last)) \
            and gs[last[0]].rect[0] - gs[words[-2][-1]].rect[2] > 2 * size:
        tag, words = last, words[:-1]

    def is_math(w: list[int]) -> bool:
        if any(gs[i].math for i in w):
            return True
        if not any(gs[i].size < 0.8 * size or abs(gs[i].oy - base) > 0.15 * size for i in w):
            return False
        # a raised or small mark after a word of text is a footnote mark, not a formula ("function⁶")
        plain = "".join(gs[i].text for i in w if gs[i].size >= 0.8 * size and abs(gs[i].oy - base) <= 0.15 * size)
        return not re.search(r"[A-Za-z]{3}", plain)

    def is_bridge(w: list[int]) -> bool:
        text = "".join(gs[i].text for i in w)
        if len(text) == 1 and text.isalpha() and re.search(r"Italic|Oblique|CMMI|CMTI", gs[w[0]].font, re.I):
            return True                                # a variable set in italic (y = a × b): joins a formula
        return all(c in BRIDGE for c in text) or text in OPERATOR_NAMES or text in ("argmax", "argmin")

    kinds = ["m" if is_math(w) else "b" if is_bridge(w) else "t" for w in words]
    runs: list[list[int]] = []
    k = 0
    while k < len(words):
        if kinds[k] != "m":
            k += 1
            continue
        a = k
        while a > 0 and kinds[a - 1] == "b":
            a -= 1
        e = k
        while e + 1 < len(words) and kinds[e + 1] in "mb":
            e += 1
        while e > a and kinds[e] == "b" and all(gs[i].text in ",.;:" for i in words[e]):   # sentence punctuation
            e -= 1
        idx = [i for w in words[a:e + 1] for i in w]
        while len(idx) > 1 and gs[idx[-1]].text in ",.;:" and not gs[idx[-1]].math:
            idx.pop()
        if idx:
            runs.append(list(range(idx[0], idx[-1] + 1)))
        k = e + 1
    n = sum(len(w) for w in words)
    share = sum(len(w) for w, kind in zip(words, kinds) if kind in "mb") / n if n and runs else 0.0
    # a line with words of text in it ("If τ+n<T, then: …" in an algorithm) is text with formulas, not a display; a
    # display opens with math and may carry a few connecting words after it ("= 1, for all s ∈ S")
    wordy = sum(len(w) for w, kind in zip(words, kinds) if kind == "t" and len(w) >= 2)
    if wordy > DISPLAY_WORDS * n or wordy and kinds[0] == "t":
        share = min(share, 0.5)
    return runs, tag, share, all(kind == "b" for kind in kinds)


def _gap(a: list[float], b: list[float]) -> tuple[float, float]:
    """(horizontal, vertical) gap between two boxes (negative: overlap)."""
    return max(a[0], b[0]) - min(a[2], b[2]), max(a[1], b[1]) - min(a[3], b[3])


@dataclass
class _Frac:
    sym: Sym
    members: set[tuple[int, int, int]]
    rules: list[int]


def _fractions(blocks: list[dict], rules: list[list[float]], page_width: float) -> list[_Frac]:
    """Fractions found from their bars, before lines are looked at: MuPDF puts a numerator on the line of the text
    around it and the denominator on a line of its own, so a fraction is gathered across lines and blocks."""
    syms: list[Sym] = []
    for bi, b in enumerate(blocks):
        for li, ln in enumerate(b["lines"]):
            for i, g in enumerate(ln["glyphs"]):
                if not g.space and g.text:
                    s = sym_of(g)
                    s.key = (bi, li, i)
                    syms.append(s)
    out: list[_Frac] = []
    used: set[int] = set()
    taken: set[tuple[int, int, int]] = set()
    for ri in sorted(range(len(rules)), key=lambda i: -(rules[i][2] - rules[i][0])):
        r = rules[ri]
        if ri in used or r[2] - r[0] > 0.5 * page_width:
            continue
        pool = [s for s in syms if s.key not in taken and r[0] - 0.5 <= s.cx <= r[2] + 0.5
                and s.rect[0] >= r[0] - 0.3 * s.size and s.rect[2] <= r[2] + 0.3 * s.size
                and abs(s.cy - r[1]) < 2.5 * s.size]
        if not pool or any(s.tex == "\\sqrt" and abs(s.rect[2] - r[0]) < 0.6 * s.size for s in syms):
            continue
        ref = max(s.size for s in pool)
        above, below = _stack(pool, r[1], True, ref), _stack(pool, r[3], False, ref)
        if not above or not below:
            continue
        span = _union(s.rect for s in above + below)
        mathy = any(MATH_FONT.match(s.font) or s.tex.startswith("\\") or s.tex.isdigit() for s in above + below)
        if not mathy or (r[2] - r[0]) > (span[2] - span[0]) + 1.5 * ref + 0.2 * (r[2] - r[0]):
            continue
        inner = [j for j in range(len(rules)) if j != ri and j not in used and _inside(rules[j], span, 1.0)]
        used.add(ri)
        used.update(inner)
        tex = f"\\frac{{{_parse(_words(_merge_pieces(above)), [rules[j] for j in inner])}}}" \
              f"{{{_parse(_words(_merge_pieces(below)), [rules[j] for j in inner])}}}"
        members = {s.key for s in above + below}
        taken |= members
        y = (r[1] + r[3]) / 2
        out.append(_Frac(Sym(tex, _union([span, r]), y + 0.25 * ref, ref, kind="group"), members, [ri, *inner]))
    return out


def page_regions(blocks: list[dict], rules: list[list[float]], page_width: float = 612.0) -> list[Region]:
    """Every formula of the page (blocks as read_lines gives them)."""
    fracs = _fractions(blocks, rules, page_width)
    claimed = {k for f in fracs for k in f.members}
    free_rules = [r for i, r in enumerate(rules) if not any(i in f.rules for f in fracs)]
    rules = free_rules
    lines: list[_Line] = []
    for bi, b in enumerate(blocks):
        for li, ln in enumerate(b["lines"]):
            # glyphs a fraction took are blanked (indices stay, so the line's text can still be rebuilt)
            gs = [Glyph(" ", " ", " ", g.rect, g.oy, g.size, g.font) if (bi, li, i) in claimed else g
                  for i, g in enumerate(ln["glyphs"])]
            ink = [g for g in gs if not g.space and g.text]
            if not ink:
                continue
            runs, tag, share, bridges = _line_parts(gs)
            kind = "strong" if runs and share >= 0.75 else "weak" if bridges and len(ink) <= 12 else "text"
            box = _union(g.rect for i, g in enumerate(gs) if not g.space and g.text and not (tag and i in tag))
            span = _union(g.rect for g in ln["glyphs"] if not g.space)
            lines.append(_Line(bi, li, gs, runs, tag, share, kind, box, _main_size(gs), span))

    # math lines set together, across blocks (MuPDF often makes each piece of a display a block of its own):
    # numerator over denominator, limits, brackets, "=" and operator names, the rows of a long display
    frac_lines = [_Line(-1, k, [], [], None, 1.0, "strong", f.sym.rect, f.sym.size) for k, f in enumerate(fracs)]
    pool = [x for x in lines if x.kind != "text"] + frac_lines
    parent = list(range(len(pool)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(pool):
        for j in range(i + 1, len(pool)):
            b = pool[j]
            gx, gy = _gap(a.box, b.box)
            size = max(a.size, b.size)
            if (gy < 0.9 * size and gx < 2 * size) or (gy < 0 and gx < 3 * size):
                parent[find(i)] = find(j)
    groups: dict[int, list[_Line]] = {}
    for i, x in enumerate(pool):
        groups.setdefault(find(i), []).append(x)
    clusters = [c for c in groups.values() if any(x.kind == "strong" for x in c)]
    # an equation number set as a line of its own at the right of a display
    for x in lines:
        if x.kind == "weak" and TAG_RE.match("".join(g.text for g in x.gs if not g.space)) and not any(x in c for c in clusters):
            home = next((c for c in clusters if _gap(_union(y.box for y in c), x.box)[1] < 0
                         and x.box[0] - _union(y.box for y in c)[2] > 3 * x.size), None)
            if home:
                x.tag = [i for i, g in enumerate(x.gs) if not g.space]
                home.append(x)

    text_lines = [x for x in lines if x.kind != "strong"]
    out: list[Region] = []
    taken: set[tuple[int, int, int]] = set()
    for c in clusters:
        if all(x.kind == "weak" for x in c):
            continue
        if not any(x.bi >= 0 for x in c) and False:
            continue
        box = _union(x.box for x in c)
        size = max(x.size for x in c)
        cy = (box[1] + box[3]) / 2
        hosts = sorted((t for t in text_lines if t not in c and t.kind == "text"
                        and t.box[1] - 0.25 * t.size <= cy <= t.box[3] + 0.25 * t.size
                        and _gap(t.span or t.box, box)[0] < size and box[3] - box[1] < 4 * t.size),
                       key=lambda t: (t.span or t.box)[0])
        host = hosts[0] if hosts else None
        members: list[tuple[int, int, int]] = []
        glyphs: list[Glyph] = []
        extra: list[Sym] = []
        tag = None
        for x in c:
            if x.bi == -1:
                extra.append(fracs[x.li].sym)
                members += sorted(fracs[x.li].members)
        for x in sorted((x for x in c if x.bi >= 0), key=lambda x: (x.bi, x.li)):
            if x.tag:
                tag = TAG_RE.match("".join(x.gs[i].text for i in x.tag)).group(1)
            for i, g in enumerate(x.gs):
                if not g.space or (x.tag and i in x.tag):
                    members.append((x.bi, x.li, i))
                if not g.space and g.text and not (x.tag and i in x.tag):
                    glyphs.append(g)
        if host:     # a fraction (or other stacked formula) inside a line of text: one inline formula there
            near = [r for r in host.runs if _gap(_union(host.gs[i].rect for i in r if not host.gs[i].space), box)[0] < 0.8 * size]
            for r in near:
                members += [(host.bi, host.li, i) for i in r]
                glyphs += [host.gs[i] for i in r if not host.gs[i].space]
                taken.update((host.bi, host.li, i) for i in r)
            at = min(r[0] for r in near) if near else next((i for i, g in enumerate(host.gs) if g.rect[0] >= box[0] and not g.space),
                                                             len(host.gs))
            anchor, display = (host.bi, host.li, at), False
        else:
            # a display stands apart: indented from the text lines around it, or in a block of its own
            first = next((x for x in c if x.bi >= 0), None)
            around = [t for t in text_lines if t.kind == "text" and abs(t.box[1] - box[1]) < 6 * size
                      and _gap(t.box, box)[0] < 0 and first is not None and t.bi == first.bi]
            display = not around or box[0] - min(t.box[0] for t in around) > 1.5 * size or len(c) > 1
            anchor = min(members)
        rs = [r for r in rules if r[0] >= box[0] - size and r[2] <= box[2] + size and box[1] - 1 <= (r[1] + r[3]) / 2 <= box[3] + 1]
        latex = to_latex([sym_of(g) for g in glyphs] + extra, rs)
        out.append(Region(latex, display, _union([*(g.rect for g in glyphs), *(e.rect for e in extra)]), members,
                          tag if display else None, anchor))
    clustered = {(x.bi, x.li) for c in clusters for x in c if not all(y.kind == "weak" for y in c)}
    for x in lines:
        if (x.bi, x.li) in clustered:
            continue
        for run in x.runs:
            if (x.bi, x.li, run[0]) in taken:
                continue
            glyphs = [x.gs[i] for i in run if not x.gs[i].space and x.gs[i].text]
            if not glyphs:
                continue
            box = _union(g.rect for g in glyphs)
            rs = [r for r in rules if r[0] >= box[0] - 1 and r[2] <= box[2] + 1 and box[1] - 2 <= r[1] <= box[3] + 2]
            out.append(Region(to_latex([sym_of(g) for g in glyphs], rs), False, box,
                              [(x.bi, x.li, i) for i in run], None, (x.bi, x.li, run[0])))
    return out


@dataclass
class PageMath:
    blocks: list[dict]
    regions: list[Region]

    def block_text(self, bi: int) -> list[str]:
        """Block bi's lines with formulas as $…$ / $$…$$ and every glyph as what it is (lines left empty dropped)."""
        anchors: dict[tuple[int, int], list[Region]] = {}
        for r in self.regions:
            if r.anchor[0] == bi:
                anchors.setdefault((r.anchor[1], r.anchor[2]), []).append(r)
        covered = {(l, i) for r in self.regions for b, l, i in r.members if b == bi}
        out = []
        for li, ln in enumerate(self.blocks[bi]["lines"]):
            s = ""
            for i in range(len(ln["glyphs"]) + 1):
                for r in sorted(anchors.get((li, i), []), key=lambda r: r.rect[0]):
                    s += (" " if s and not s.endswith(" ") else "") + r.markup() + " "
                if i == len(ln["glyphs"]) or (li, i) in covered:
                    continue
                g = ln["glyphs"][i]
                if g.space and s.endswith(" "):
                    continue
                s += g.text
            s = re.sub(r"(?<=\$)\s+([,.;:)])", r"\1", re.sub(r"\s{2,}", " ", s.strip())) if s.strip() else ""
            if s:
                out.append(s)
        return out


def page_math(page, fonts: FontNames, flags: int) -> PageMath:
    blocks = read_lines(page, fonts, flags)
    return PageMath(blocks, page_regions(blocks, page_rules(page), page.rect.width))
