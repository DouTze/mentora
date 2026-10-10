"""R5: PDF symbol and media evidence from self-made fixtures (evals/reading/pdf/). Page, bbox, spans and font flags
are kept per block; regions render on demand; symbols are confirmed only by a vision-capable session model reading
the rendered region; otherwise the unit stays unverified with an actionable reason, never guessed."""
import io
import json
from pathlib import Path

from app.llm.base import Capabilities
from app.llm.mock import MockBackend
from app.materials import extract_units
from app.reading.pdf_evidence import attach_evidence, render_region, unit_symbol_evidence
from app.reading.source import extract_blocks
from test_workflow import graph_app, idle, run  # noqa: F401  (fixture re-export)

PDF = Path(__file__).resolve().parent.parent / "evals" / "reading" / "pdf"


def evidence_blocks(name):
    path = PDF / f"{name}.pdf"
    units = extract_units("pdf", path.read_bytes())
    _, blocks = extract_blocks(name, "page", 1, len(units), units)
    return path, attach_evidence(path, blocks)


def test_font_flags_and_lost_glyphs_are_kept_per_block():
    path, blocks = evidence_blocks("symbols")
    sym = next(b for b in blocks if b.text.startswith("The weight vector"))
    e = sym.meta["pdf"]
    assert e["page"] == 1 and len(e["bbox"]) == 4 and e["bold_tokens"] == ["w"] and "x" in e["italic_tokens"]
    assert e["superscripts"] == ["2"] and e["math_fonts"] == ["Symbol"] and e["lost_glyphs"]
    assert any(sp["font"] == "Times-Bold" and sp["t"] == "w" for sp in e["spans"])
    # the text reads the raised 2 as a superscript (app.pdfdoc.math); the font evidence above is still what confirms it
    assert any("$x^{2}$" in b.text for b in blocks)
    plain = next(b for b in blocks if b.text.startswith("This paragraph"))
    assert unit_symbol_evidence([plain], str(path), False) is None
    ev = unit_symbol_evidence([sym], str(path), False)
    assert ev["status"] == "needs_visual" and ev["regions"] == [{"page": 1, "bbox": e["bbox"]}] and "遺失" in ev["reason"]
    assert unit_symbol_evidence([sym], None, False)["status"] == "unavailable"


def test_only_the_region_is_rendered():
    path, blocks = evidence_blocks("symbols")
    e = next(b for b in blocks if b.text.startswith("The weight")).meta["pdf"]
    png = render_region(path, e["page"], e["bbox"])
    assert png.startswith(b"\x89PNG")
    import pymupdf
    pix = pymupdf.Pixmap(png)
    assert pix.width < 3 * 480 and pix.height < 3 * 120             # a strip of the page, not the page


def test_a_caption_next_to_an_image_marks_a_figure_reference():
    from app.reading import segmentation as seg
    from app.reading.schemas import SegmentationPlan, SourceUnit
    _, blocks = evidence_blocks("figure")
    cap = next(b for b in blocks if b.text.startswith("Figure 1"))
    assert cap.meta["pdf"]["caption"] and cap.meta["pdf"]["images_near"]
    plan = SegmentationPlan(units=[SourceUnit(unit_id=f"u{i}", ordinal=i, block_ids=[b.block_id], segmentation_version="x")
                                   for i, b in enumerate(blocks)])
    quotes = seg.to_quotes("page", plan, {b.block_id: b for b in blocks})
    assert quotes[0]["has_figure"] and not quotes[1]["has_figure"]


def upload_pdf(c, name):
    data = (PDF / f"{name}.pdf").read_bytes()
    m = c.post("/api/materials", files={"file": (f"{name}.pdf", io.BytesIO(data))}).json()
    s = c.post("/api/sessions", json={"material_id": m["id"], "start": 1, "end": 1}).json()
    return idle(c, s["id"])


def test_a_text_only_model_writes_the_handout_with_a_note_on_its_symbols(graph_app):
    """Without a model that can look at the page the symbols cannot be confirmed: the handout is still written, and
    it tells the reader which symbols to check against the textbook (with what would fix it)."""
    c = graph_app
    s = upload_pdf(c, "symbols")
    g = s["reading"]["guides"]["0"]
    notes = [u for u in g["unverified"] if u.get("code") == "notation_unverified"]
    assert g["guide"] and notes and "支援影像" in notes[0]["text"]
    snap = c.app_ref.state.db.get_session(s["id"])["workflow_meta"]["snapshot_id"]
    blocks = c.app_ref.state.repo.load_blocks(snap)
    assert any((b.meta.get("pdf") or {}).get("bold_tokens") == ["w"] for b in blocks)   # evidence persisted


def test_a_vision_model_reads_the_region_and_the_unit_passes(graph_app, monkeypatch, tmp_path):
    real = MockBackend.generate

    async def caps(self):
        return Capabilities(vision=True, source="test")

    async def gen(self, messages, **kw):
        if (kw.get("meta") or {}).get("step") == "symbols":
            assert messages[0].get("images")
            from app.llm.base import LLMResult
            return LLMResult(text=json.dumps({"formulas": ["\\mathbf{w} \\leftarrow \\mathbf{w} - \\eta \\nabla L", "x^{2}"],
                                              "symbols": [{"symbol": "w", "latex": "\\mathbf{w}", "style": "bold"},
                                                          {"symbol": "x", "latex": "x", "style": "italic"}],
                                              "unreadable": []}), latency_ms=1)
        return await real(self, messages, **kw)

    monkeypatch.setattr(MockBackend, "capabilities", caps)
    monkeypatch.setattr(MockBackend, "generate", gen)
    c = graph_app
    s = upload_pdf(c, "symbols")
    g = s["reading"]["guides"]["0"]
    assert g["review_status"] == "passed" and r"\mathbf{w}" in g["guide"]   # the equation as the region shows it
    trace = c.app_ref.state.repo.list_llm_calls(s["id"])
    assert [x["node"] for x in trace][:2] == ["segment.w01", "symbols"] or "symbols" in [x["node"] for x in trace]
    ev_dir = Path(c.app_ref.state.svc.reading_dir).parent / "evidence"
    assert list(ev_dir.glob("*.png"))                               # the rendered region is kept for review


def test_footnote_marks_and_bold_words_alone_do_not_make_a_unit_symbolic():
    from app.reading.schemas import Locator, SourceBlock
    def block(**clues):
        ev = {"page": 1, "bbox": [0, 0, 1, 1], "bold_tokens": [], "italic_tokens": [], "superscripts": [], "math_fonts": [],
              "lost_glyphs": False, **clues}
        return SourceBlock(block_id="b", material_snapshot_id="s", ordinal=0, kind="paragraph", text="t", text_hash="h",
                           extraction_version="v", locator=Locator(unit="page", start=1, end=1, para=1), meta={"pdf": ev})
    assert unit_symbol_evidence([block(superscripts=["1"], bold_tokens=["AI"])], "/x.pdf", False) is None
    assert unit_symbol_evidence([block(bold_tokens=["w"])], "/x.pdf", False)["status"] == "needs_visual"


def test_math_fonts_settle_letter_styles_and_correct_a_vision_misreading():
    """qwen3-vl read S_t, A_t, R_t (CMMI, math italic) as bold and listed them again and again until its output was
    cut off. The embedded math font is what draws the glyph: a letter drawn in one style is corrected; a letter drawn
    in two (S_t and a calligraphic S) is left to the rendered region, and a reading outside both is a conflict."""
    from app.reading.contracts import SymbolItem, SymbolTranscription
    from app.reading.gates import fix_by_fonts
    from app.reading.pdf_evidence import font_style, letter_fonts
    assert [font_style(f) for f in ("ABCDEF+CMMI10", "CMMIB10", "CMBX12", "EUSM10", "MSBM10", "CMR10", "Times-Italic")] \
        == ["italic", "bold", "bold", "calligraphic", "blackboard", None, None]
    spans = [{"t": "S", "font": "CMMI10"}, {"t": ", A", "font": "CMMI10"}, {"t": "Pr", "font": "CMR10"},
             {"t": "a", "font": "CMR10"}, {"t": "S", "font": "EUSM10"}, {"t": "R", "font": "CMMI10"},
             {"t": "Finite", "font": "CMBX12"}]
    assert letter_fonts(spans) == {"A": ["italic"], "R": ["italic"], "S": ["calligraphic", "italic"]}
    bold = lambda s: SymbolItem(symbol=s, latex=rf"\mathbf{{{s}}}", style="bold")  # noqa: E731
    tr = SymbolTranscription(formulas=[r"\mathbf{A}_0, \mathbf{R}_1", r"\mathbf{A}_0, \mathbf{R}_1", r"S_t \in \mathcal{S}"],
                             symbols=[bold("A"), bold("R"), bold("A"), SymbolItem(symbol="S", latex="S", style="italic")])
    fixed, fixes, conflicts = fix_by_fonts(tr, {"A": ["italic"], "R": ["italic"], "S": ["calligraphic", "italic"]})
    assert fixed.formulas == ["A_0, R_1", r"S_t \in \mathcal{S}"] and not conflicts
    assert [(x.symbol, x.latex, x.style) for x in fixed.symbols] == [("A", "A", "italic"), ("R", "R", "italic"),
                                                                       ("S", "S", "italic")]
    assert fixes == ["A：粗體 → 斜體", "R：粗體 → 斜體"]
    _, _, conflicts = fix_by_fonts(SymbolTranscription(formulas=[r"\mathbf{S}_t"]), {"S": ["calligraphic", "italic"]})
    assert conflicts and "書法體或斜體" in conflicts[0]


def test_footnotes_leave_the_sentence_flow_and_figure_labels_are_dropped():
    import pymupdf
    from app.materials import pdf_page_paragraphs
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_textbox((50, 50, 550, 110), "The body text starts a sentence that runs on and on across the line" + " and on" * 12 + ".1", fontsize=10)
    page.draw_rect((100, 150, 400, 300), color=(0, 0, 0))
    page.insert_text((150, 220), "state s", fontsize=9)
    page.insert_text((100, 320), "Figure 1: An agent and its world.", fontsize=10)
    page.insert_textbox((50, 360, 550, 420), "More body text follows the figure and keeps going" + " and going" * 12 + ".", fontsize=10)
    page.insert_text((50, 760), "1 A note explaining a term.", fontsize=7)
    texts = [t for t, _ in pdf_page_paragraphs(page)]
    assert not any("state s" in t for t in texts)                    # a label drawn inside the figure
    assert any(t.startswith("Figure 1") for t in texts)              # the caption stays
    assert texts[-1] == "[^1]: A note explaining a term."


def test_labels_of_a_drawing_without_a_caption_are_dropped_but_a_larger_heading_beside_one_is_kept():
    """A backup diagram in an exercise has no caption: its short labels level with the drawing are part of it. A
    section heading set larger than the text inside a decorated frame is not a label."""
    import pymupdf
    from app.materials import pdf_page_paragraphs
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_textbox((50, 50, 550, 130), "Exercise 3.18 The value of a state depends on the values of the actions"
                        + " possible in that state" * 8 + ".", fontsize=10)
    page.draw_circle((300, 180), 20, color=(0, 0, 0))
    page.insert_text((330, 185), "v(s)", fontsize=9)
    page.draw_rect((50, 300, 550, 340), color=(0, 0, 0))
    page.insert_text((60, 325), "4.1 Evaluation", fontsize=16)
    page.insert_textbox((50, 360, 550, 440), "Body text continues after the heading" + " and keeps going" * 10 + ".", fontsize=10)
    texts = [t for t, _ in pdf_page_paragraphs(page)]
    assert "v(s)" not in texts and "4.1 Evaluation" in texts and len(texts) == 3
