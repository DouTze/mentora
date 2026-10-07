"""app.pdfdoc: reading order (the cases of printcraft's text.rs tests), document info and page layers."""
import pymupdf
import pytest

from app.pdfdoc import Glyph, document_info, embedded_file, extract_page, image_bytes, layout, page_layer
from app.pdfdoc.text import from_upright, to_upright


def g(t, x0, y0, x1):
    return Glyph(t, [x0, y0, x1, y0 + 10.0])


def word(v, s, x, y, advance):
    for i, c in enumerate(s):
        x0 = x + i * advance
        v.append(g(c, x0, y, x0 + 6.0))


def test_upright_mapping_round_trips():
    r = [10.0, 20.0, 30.0, 25.0]
    for q in range(4):
        assert from_upright(to_upright(r, q, 300.0, 200.0), q, 300.0, 200.0) == r
    assert to_upright([290.0, 0.0, 300.0, 10.0], 1, 300.0, 200.0) == [0.0, 0.0, 10.0, 10.0]


def test_lines_words_and_search():
    v = []
    word(v, "Hello", 10, 10, 6)
    word(v, "world", 50, 10, 6)
    word(v, "Next", 10, 30, 6)
    t = layout(v)
    assert t.plain_text() == "Hello world\nNext"
    assert t.find("WORLD next") == [(5, 14)]
    assert t.find("o") == [(4, 5), (6, 7)]
    assert len(t.line_rects(3, 12)) == 2
    assert t.nearest(52, 14) == 5


def test_fake_bold_duplicates_are_removed():
    v = []
    for i, c in enumerate("Bold"):
        x = 10 + i * 7
        v += [g(c, x, 10, x + 6), g(c, x + 0.3, 10, x + 6.3)]
    assert layout(v).plain_text() == "Bold"


def test_two_columns_read_left_then_right_even_if_drawn_interleaved():
    v = []
    word(v, "Left1", 10, 10, 6)
    word(v, "Right1", 200, 10, 6)
    word(v, "Left2", 10, 24, 6)
    word(v, "Right2", 200, 24, 6)
    assert layout(v).plain_text() == "Left1\nLeft2\nRight1\nRight2"


def test_tracked_text_keeps_letters_together():
    v = []
    word(v, "CHAPTER", 10, 10, 9)
    word(v, "FOUR", 10 + 7 * 9 + 5, 10, 9)
    assert layout(v).plain_text() == "CHAPTER FOUR"


def test_rtl_runs_come_out_in_logical_order_and_cjk_has_no_spaces():
    v = []
    word(v, "םולש", 10, 10, 6)
    assert layout(v).plain_text() == "שלום"
    v = []
    word(v, "每个字", 10, 10, 9)
    assert layout(v).plain_text() == "每个字"


@pytest.fixture
def sample(tmp_path):
    """Three pages: links (URI and to page 3), an image, a highlight, an attachment, an outline, page labels, and a
    page shown rotated."""
    doc = pymupdf.open()
    p1 = doc.new_page(width=300, height=400)
    p1.insert_text((40, 60), "See the official site for more.", fontsize=11)
    p1.insert_text((40, 90), "Doubled letters: called, will, offer.", fontsize=11)
    p1.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(40, 48, 200, 64), "uri": "https://example.org/docs"})
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 6), False)
    pix.clear_with(200)
    p1.insert_image(pymupdf.Rect(40, 200, 120, 260), pixmap=pix)
    hl = p1.search_for("official")[0]
    p1.add_highlight_annot(hl).set_info(content="key word", title="reader")
    doc.new_page(width=300, height=400).insert_text((40, 60), "Second page text.")
    p3 = doc.new_page(width=300, height=400)
    p3.insert_text((40, 60), "Rotated page text.")
    p3.set_rotation(90)
    doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(40, 78, 200, 94), "page": 2, "to": pymupdf.Point(0, 100)})
    doc.embfile_add("notes.txt", b"hello attachment", filename="notes.txt", desc="notes")
    doc.set_toc([[1, "Start", 1], [2, "Detail", 2], [1, "End", 3]])
    doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1},
                         {"startpage": 1, "prefix": "", "style": "D", "firstpagenum": 1}])
    doc.set_metadata({"title": "Sample Book", "author": "A. Writer"})
    path = tmp_path / "sample.pdf"
    doc.save(path)
    return path


def test_document_info(sample):
    with pymupdf.open(sample) as doc:
        info = document_info(doc)
    assert info["title"] == "Sample Book" and info["author"] == "A. Writer" and info["page_count"] == 3
    assert info["pages"][0] == [300.0, 400.0] and info["pages"][2] == [400.0, 300.0]     # rotation applied
    assert info["labels"] == ["i", "1", "2"]
    assert [o["title"] for o in info["outline"]] == ["Start", "End"]
    assert info["outline"][0]["children"][0] == {"title": "Detail", "page": 2, "children": [], "y": info["outline"][0]["children"][0].get("y")}
    assert info["counts"]["links"] == 2 and info["counts"]["images"] == 1 and info["counts"]["annotations"] == 1
    assert info["attachments"] == [{"name": "notes.txt", "description": "notes", "size": 16, "source": {"kind": "document", "key": "notes.txt"}}]
    assert info["fonts"] and not info["warnings"]


def test_page_layer_text_links_images_annotations(sample):
    with pymupdf.open(sample) as doc:
        layer = page_layer(doc, 1)
        text = extract_page(doc[0]).plain_text()
        data, ext = image_bytes(doc, layer["images"][0]["xref"])
        name, blob = embedded_file(doc, {"kind": "document", "key": "notes.txt"})
    assert text.splitlines() == ["See the official site for more.", "Doubled letters: called, will, offer."]
    first = layer["lines"][0]["runs"]
    assert [r["t"] for r in first] == ["See", "the", "official", "site", "for", "more."]
    assert "sp" not in first[0] and all(r["sp"] for r in first[1:])
    x0, y0, x1, y1 = first[0]["rect"]
    assert 39 < x0 < 41 and y0 < 60 < y1 and y1 - y0 == pytest.approx(11, abs=0.1)   # the em box of 11pt text
    links = {ln["kind"]: ln for ln in layer["links"]}
    assert links["uri"]["uri"] == "https://example.org/docs" and links["uri"]["rect"] == [40, 48, 200, 64]
    assert links["page"]["page"] == 3
    assert layer["images"][0]["rect"] == [40, 200, 120, 260] and (layer["images"][0]["width"], layer["images"][0]["height"]) == (8, 6)
    assert ext in ("png", "jpeg") and data[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xdb")
    hl = layer["annotations"][0]
    assert hl["type"] == "Highlight" and hl["content"] == "key word" and hl["author"] == "reader" and hl["quads"]
    assert (name, blob) == ("notes.txt", b"hello attachment")


def test_rotated_page_is_laid_out_in_view_space(sample):
    with pymupdf.open(sample) as doc:
        layer = page_layer(doc, 3)
        assert extract_page(doc[2]).plain_text() == "Rotated page text."
    assert layer["size"] == [400.0, 300.0] and layer["label"] == "2"
    # /Rotate 90 turns the text to run downwards on screen
    assert layer["quarter"] == 1
    for r in layer["lines"][0]["runs"]:
        x0, y0, x1, y1 = r["rect"]
        assert 0 <= x0 < x1 <= 400 and 0 <= y0 < y1 <= 300 and (y1 - y0) > (x1 - x0)
