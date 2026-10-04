"""Self-made PDF fixtures for symbol and media evidence (R5). Run from backend/:

    .venv/bin/python evals/reading/pdf/make_fixtures.py

symbols.pdf — a paragraph with a bold vector w (Times-Bold), an italic scalar x with a superscript 2, and Symbol-font
             glyphs (← − η ∇) whose text layer is lost; a second paragraph with no math; a heading-like short line.
figure.pdf  — an image, its caption ("Figure 1: …") right below, and a paragraph that refers to it.
The files are small and kept in the repository so the tests do not depend on PDF generation."""
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent


def run(page, x, y, parts):
    for text, font, size, rise in parts:
        page.insert_text((x, y + rise), text, fontname=font, fontsize=size)
        x += pymupdf.get_text_length(text, fontname=font, fontsize=size)
    return x


def symbols() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=480, height=360)
    page.insert_text((40, 50), "Gradient descent", fontname="hebo", fontsize=14)
    run(page, 40, 90, [("The weight vector ", "tiro", 12, 0), ("w", "tibo", 12, 0), (" is updated as ", "tiro", 12, 0),
                       ("w", "tibo", 12, 0), (" ← ", "symb", 12, 0), ("w", "tibo", 12, 0), (" − ", "symb", 12, 0),
                       ("η∇", "symb", 12, 0), ("L", "tiit", 12, 0), (".", "tiro", 12, 0)])
    run(page, 40, 106, [("The scalar ", "tiro", 12, 0), ("x", "tiit", 12, 0), ("2", "tiro", 8, -4),
                        (" is the squared input.", "tiro", 12, 0)])
    page.insert_text((40, 160), "This paragraph explains the idea in words only, without any formula.", fontname="tiro", fontsize=12)
    return doc.tobytes()


def figure() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=480, height=420)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 60), False)
    pix.clear_with(200)
    page.insert_image(pymupdf.Rect(60, 40, 300, 160), pixmap=pix)
    page.insert_text((60, 180), "Figure 1: Training loss over epochs.", fontname="tiro", fontsize=11)
    page.insert_text((60, 230), "As the figure shows, the loss drops quickly at first and then levels off.",
                     fontname="tiro", fontsize=12)
    return doc.tobytes()


if __name__ == "__main__":
    (HERE / "symbols.pdf").write_bytes(symbols())
    (HERE / "figure.pdf").write_bytes(figure())
    print("written", sorted(p.name for p in HERE.glob("*.pdf")))
