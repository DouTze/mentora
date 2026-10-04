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
    assert "x2" in sym.text                                      # the text layer flattened x² — why text is not evidence
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


def test_a_text_only_model_holds_a_symbol_unit_with_an_actionable_reason(graph_app):
    c = graph_app
    s = upload_pdf(c, "symbols")
    wf = s["workflow"]
    assert wf["waiting"] == "retry" and wf["error"]["code"] == "notation_unverified"
    assert "支援影像" in wf["error"]["detail"] and "第 1 頁" in wf["error"]["detail"]
    assert "0" not in s["reading"]["guides"]                         # nothing published as a checked guide
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
    assert g["gate1"]["status"] == "pass"
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
