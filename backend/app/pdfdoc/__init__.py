"""A PDF read the way PrintCraft (github.com/storytold/printcraft, crates/render) reads one, on PyMuPDF.

    text      every glyph in page view space → lines in reading order with word gaps (printcraft `text.rs`)
    inspect   what is not pixels: metadata, page labels, outline, links, annotations, form fields, layers, fonts,
              attachments, images (printcraft `inspect.rs`)
    layer     one page for the reader: text runs to lay over the page image, links, images, annotations

View space (as in PrintCraft): points, origin at the top-left of the page as displayed, `/Rotate` applied, y down.
The reader scales it to the screen with a single factor.

MuPDF must not run on two threads of one process: the server calls these through app.pdf_worker."""
from .inspect import document_info, embedded_file, image_bytes
from .layer import page_layer
from .text import Glyph, PageText, extract_page, layout

__all__ = ["Glyph", "PageText", "document_info", "embedded_file", "extract_page", "image_bytes", "layout", "page_layer"]
