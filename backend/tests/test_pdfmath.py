"""app.pdfdoc.math: glyph names from Type 1 programs, layout to LaTeX, formulas on a page, and the imported text."""
import io

import pymupdf
import pytest

from app.pdfdoc.math import FontNames, Sym, identify, page_math, to_latex
from app.pdfdoc.texglyphs import charstring_names


# -- glyph identity -------------------------------------------------------------------------------------------
def _encrypt(plain: bytes, r: int = 55665) -> bytes:
    out = bytearray()
    for p in b"\0\0\0\0" + plain:
        c = p ^ (r >> 8)
        r = ((c + r) * 52845 + 22719) & 0xFFFF
        out.append(c)
    return bytes(out)


def test_charstring_names_in_freetype_order():
    plain = b"/Private 1 dict begin end /CharStrings 3 dict dup begin\n/alpha 2 RD xy ND\n/.notdef 1 RD z ND\n/pi 3 RD abc ND\nend\n"
    pfa = b"%!PS-AdobeFont-1.0: CMMI10\ncurrentfile eexec\n" + _encrypt(plain).hex().encode() + b"\ncleartomark\n"
    assert charstring_names(pfa) == [".notdef", "alpha", "pi"]          # .notdef first, as FreeType indexes
    assert charstring_names(b"not a type 1 font") is None


def test_tex_glyph_names():
    assert identify("pi", "⇡", "CMMI10") == ("π", "\\pi")                 # the name wins over a broken ToUnicode
    assert identify("summationdisplay", "P", "CMEX10") == ("∑", "\\sum")
    assert identify("asteriskmath", "⇤", "CMSY10") == ("∗", "\\ast")
    assert identify("ff", "↵", "CMR10") == ("ff", "ff")
    assert identify("triangle", "4", "CMSY10")[1] == "\\triangle"
    assert identify("triangle", "4", "MSAM10")[1] == "\\vartriangle"   # the same name in another font
    assert identify(None, "≤", "CambriaMath") == ("≤", "\\leq")          # no name: the Unicode table


# -- layout -----------------------------------------------------------------------------------------------------
def g(tex, x, oy, size=10.0, font="CMMI10", w=None, top=None, bottom=None):
    w = w if w is not None else 0.5 * size
    top = oy - 0.7 * size if top is None else top
    bottom = oy + 0.05 * size if bottom is None else bottom
    return Sym(tex, [x, top, x + w, bottom], oy, size, font)


def test_scripts_primes_and_words():
    # x_{t+1}^{2}
    syms = [g("x", 10, 100), g("t", 15, 102, 7), g("+", 18, 102, 7, "CMR7"), g("1", 22, 102, 7, "CMR7"), g("2", 15, 96, 7, "CMR7")]
    assert to_latex(syms, []) == "x_{t+1}^{2}"
    # v_{π'}(s): the prime on the subscript stays with it
    syms = [g("v", 10, 100), g("\\pi", 15, 102, 7), g("\\prime", 19, 99.5, 5, "CMSY5"), g("(", 23, 100, font="CMR10"),
            g("s", 27, 100), g(")", 32, 100, font="CMR10")]
    assert to_latex(syms, []) == "v_{\\pi'}(s)"
    # an operator name with its limit under it
    syms = [g(c, 10 + 5 * i, 100, font="CMR10") for i, c in enumerate("argmax")] + [g("a", 22, 108, 7)]
    assert to_latex(syms, []) == "\\operatorname*{argmax}_{a}"


def test_fraction_radical_limits_accent():
    # \frac{1}{1-γ} from a rule
    syms = [g("1", 20, 95, font="CMR10"), g("1", 10, 110, font="CMR10"), g("-", 16, 110, font="CMSY10"), g("\\gamma", 24, 110)]
    assert to_latex(syms, [[9, 99.8, 30, 100.2]]) == "\\frac{1}{1-\\gamma}"
    # \sqrt{x} from a radical and its overline
    rad = Sym("\\sqrt", [10, 92, 18, 104], None, 10, "CMSY10")
    assert to_latex([rad, g("x", 19, 100)], [[18, 92, 26, 92.4]]) == "\\sqrt{x}"
    # \sum_{k=0}^{\infty} with limits above and below
    op = Sym("\\sum", [10, 90, 24, 106], None, 10, "CMEX10")
    lims = [g("k", 11, 113, 7), g("=", 15, 113, 7, "CMR7"), g("0", 19, 113, 7, "CMR7"), g("\\infty", 13, 88, 7, "CMSY7")]
    assert to_latex([op, *lims, g("x", 26, 100)], []) == "\\sum_{k=0}^{\\infty}x"
    # \hat{v} and \doteq
    assert to_latex([g("\\hat", 10, 93, font="CMR10", top=90, bottom=92), g("v", 10, 100)], []) == "\\hat{v}"
    assert to_latex([g("G", 10, 100), g("=", 18, 100, font="CMR10", top=95, bottom=98),
                     g(".", 19, 93.5, font="CMR10", w=1.5, top=92, bottom=93.5),
                     g("R", 26, 100)], []) == "G\\doteq R"


def test_rows_align_on_their_relations():
    row1 = [g("G", 10, 100), g("=", 18, 100, font="CMR10"), g("a", 26, 100)]
    row2 = [g("=", 18, 125, font="CMR10"), g("b", 26, 125)]
    assert to_latex(row1 + row2, []) == "\\begin{aligned}G&=a \\\\ &=b\\end{aligned}"


# -- a page ------------------------------------------------------------------------------------------------------
def make_pdf() -> bytes:
    """A paragraph with x² and an inline fraction, then a display with an equation number."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 60), "We square it: x", fontname="tiro", fontsize=11)
    w = pymupdf.get_text_length("We square it: ", "tiro", 11)
    page.insert_text((40 + w, 60), "x", fontname="tiit", fontsize=11)
    page.insert_text((40 + w + 6, 55), "2", fontname="tiro", fontsize=7)
    page.insert_text((40 + w + 11, 60), " and take half,", fontname="tiro", fontsize=11)
    x = 40 + w + 11 + pymupdf.get_text_length(" and take half, ", "tiro", 11)
    page.insert_text((x + 1, 55), "1", fontname="tiro", fontsize=7)
    page.insert_text((x + 1, 64), "2", fontname="tiro", fontsize=7)
    page.draw_line((x, 57.3), (x + 5.5, 57.3), width=0.4)
    page.insert_text((x + 9, 60), "of it.", fontname="tiro", fontsize=11)
    page.insert_text((150, 110), "y", fontname="tiit", fontsize=11)
    page.insert_text((160, 110), "=", fontname="tiro", fontsize=11)
    page.insert_text((171, 110), "a", fontname="tiit", fontsize=11)
    page.insert_text((180, 110), "×", fontname="tiro", fontsize=11)
    page.insert_text((191, 110), "b", fontname="tiit", fontsize=11)
    page.insert_text((330, 110), "(1.2)", fontname="tiro", fontsize=11)
    page.insert_text((40, 150), "The rest is plain text without any formula at all.", fontname="tiro", fontsize=11)
    return doc.tobytes()


def test_formulas_of_a_page_and_their_text():
    doc = pymupdf.open(stream=make_pdf(), filetype="pdf")
    pm = page_math(doc[0], FontNames(doc), pymupdf.TEXT_DEHYPHENATE)
    found = {r.markup() for r in pm.regions}
    assert "$x^{2}$" in found and "$\\frac{1}{2}$" in found
    assert "$$y=a\\times b \\tag{1.2}$$" in found
    text = [t for bi in range(len(pm.blocks)) for t in pm.block_text(bi)]
    assert "We square it: $x^{2}$ and take half, $\\frac{1}{2}$" in text      # the fraction sits in its line of text
    assert "The rest is plain text without any formula at all." in text


def test_imported_pdf_text_has_latex(tmp_path):
    from app.db import Database
    from app.materials import TEXT_VERSION, MaterialStore
    store = MaterialStore(Database(tmp_path / "db.sqlite3"), files_dir=tmp_path / "files", library_dir=tmp_path / "lib")
    m = store.add("formulas.pdf", make_pdf(), None)
    assert m["text_version"] == TEXT_VERSION
    page = store.get(m["id"])["units"][0]
    assert "$x^{2}$" in page and "$$y=a\\times b \\tag{1.2}$$" in page


def test_reader_layer_carries_the_formulas():
    from app.pdfdoc import page_layer
    doc = pymupdf.open(stream=make_pdf(), filetype="pdf")
    layer = page_layer(doc, 1)
    assert {m["latex"] for m in layer["math"]} >= {"x^{2}", "\\frac{1}{2}", "y=a\\times b"}
    display = next(m for m in layer["math"] if m["display"])
    assert display["tag"] == "1.2"
    runs = [r for ln in layer["lines"] for r in ln["runs"]]
    # the glyphs of a formula give way to one run with its LaTeX, so copying the selection copies the LaTeX
    math_runs = {r["t"]: r["math"] for r in runs if "math" in r}
    assert "$x^{2}$" in math_runs and "$$y=a\\times b \\tag{1.2}$$" in math_runs
    assert not any(r["t"] in ("×", "y") for r in runs)
