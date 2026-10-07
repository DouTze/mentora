"""Document inspection: everything that is not pixels (after printcraft crates/render/src/inspect.rs).

Tolerant like PrintCraft: a part MuPDF cannot read leaves its list empty and adds a line to `warnings`, the rest of
the document is still described."""
from __future__ import annotations

import re

SUBSET_RE = re.compile(r"^[A-Z]{6}\+")


def fix_text(s: str | None) -> str | None:
    """UTF-8 bytes some producers store as PDFDocEncoding ("è³\x87æ\x96\x99" for "資料") back to their text. A real
    Latin-1 string ("café") is not valid UTF-8 and stays as it is."""
    if not s or s.isascii() or any(ord(c) > 255 for c in s):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return s


def view_rect(page, rect) -> list[float]:
    """A rectangle in PDF page space (MuPDF's unrotated, y-down space) → view space, rounded."""
    import pymupdf

    r = pymupdf.Rect(rect) * page.rotation_matrix
    r.normalize()
    return [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]


def link_target(doc, page, ln: dict) -> dict:
    """A PyMuPDF link as the reader uses it: an internal page (1-based, with the y of the destination in that page's
    view space when there is one), a URI, a file of another document, or another action."""
    import pymupdf

    kind = ln.get("kind")
    if kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and isinstance(ln.get("page"), int) and ln["page"] >= 0:
        out = {"kind": "page", "page": ln["page"] + 1}
        to = ln.get("to")
        if to is not None and 0 <= ln["page"] < doc.page_count:
            target = doc[ln["page"]]
            y = (pymupdf.Point(to) * target.rotation_matrix).y
            if 0 <= y <= target.rect.height:
                out["y"] = round(y, 1)
        return out
    if kind == pymupdf.LINK_URI and ln.get("uri"):
        return {"kind": "uri", "uri": ln["uri"]}
    if kind == pymupdf.LINK_GOTOR:
        return {"kind": "file", "file": ln.get("file") or "", "page": (ln.get("page") or 0) + 1}
    if kind == pymupdf.LINK_LAUNCH:
        return {"kind": "launch", "file": ln.get("file") or ""}
    return {"kind": "other", "name": ln.get("name") or ""}


def page_links(doc, page) -> list[dict]:
    out = []
    for ln in page.get_links():
        if ln.get("from") is None:
            continue
        out.append({"rect": view_rect(page, ln["from"]), **link_target(doc, page, ln)})
    return out


def page_annotations(page) -> list[dict]:
    """Comments and marks on the page (links and form widgets are listed elsewhere)."""
    out = []
    for k, a in enumerate(page.annots() or []):
        kind = a.type[1]
        if kind in ("Link", "Widget", "Popup"):
            continue
        info = a.info
        quads = []
        if a.vertices and kind in ("Highlight", "Underline", "StrikeOut", "Squiggly"):
            v = a.vertices
            quads = [view_rect(page, (min(p[0] for p in v[i:i + 4]), min(p[1] for p in v[i:i + 4]),
                                      max(p[0] for p in v[i:i + 4]), max(p[1] for p in v[i:i + 4])))
                     for i in range(0, len(v) - 3, 4)]
        stroke = (a.colors or {}).get("stroke")
        out.append({"index": k, "type": kind, "rect": view_rect(page, a.rect), "quads": quads,
                    "content": info.get("content") or None, "author": info.get("title") or None,
                    "modified": info.get("modDate") or None,
                    "color": "#%02x%02x%02x" % tuple(int(c * 255) for c in stroke[:3]) if stroke and len(stroke) >= 3 else None,
                    "file": a.file_info.get("filename") if kind == "FileAttachment" else None})
    return out


def page_images(page) -> list[dict]:
    """Images drawn on the page: where (view space), the stored image (xref; 0 for an inline image) and its size."""
    out = []
    for im in page.get_image_info(xrefs=True):
        r = view_rect(page, im["bbox"])
        if r[2] - r[0] < 1 or r[3] - r[1] < 1:
            continue
        out.append({"rect": r, "xref": im.get("xref") or 0, "width": im.get("width"), "height": im.get("height")})
    return out


def _outline(doc) -> list[dict]:
    """The bookmarks as a tree: {title, page (1-based or None), y, children}."""
    root: list[dict] = []
    stack: list[tuple[int, list[dict]]] = [(0, root)]
    for lvl, title, page, *rest in doc.get_toc(simple=False):
        dest = rest[0] if rest else {}
        item = {"title": (title or "").strip(), "page": page if page and page > 0 else None, "children": []}
        to = dest.get("to") if isinstance(dest, dict) else None
        if item["page"] and to is not None and item["page"] <= doc.page_count:
            item["y"] = round(to.y if hasattr(to, "y") else to[1], 1)
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        (stack[-1][1] if stack else root).append(item)
        stack.append((lvl, item["children"]))
    return root


def _catalog_key(doc, key: str) -> str | None:
    try:
        kind, value = doc.xref_get_key(doc.pdf_catalog(), key)
        return None if kind == "null" else value
    except Exception:  # noqa: BLE001
        return None


def document_info(doc) -> dict:
    """Everything the reader's panels need about one document."""
    warnings: list[str] = []
    md = doc.metadata or {}
    pages, labels = [], []
    links = images = annots = 0
    fonts: dict[tuple[str, str], dict] = {}   # Type3 and subset fonts repeat per page: one entry per name and kind
    fields, attachments = [], []
    seen_images: set[int] = set()
    for i in range(doc.page_count):
        try:
            p = doc[i]
            pages.append([round(p.rect.width, 1), round(p.rect.height, 1)])
            labels.append(p.get_label() or str(i + 1))
            links += len(p.get_links())
            for im in doc.get_page_images(i):
                if im[0] not in seen_images:
                    seen_images.add(im[0])
            for k, a in enumerate(p.annots() or []):
                if a.type[1] == "FileAttachment":
                    fi = a.file_info
                    attachments.append({"name": fix_text(fi.get("filename")) or f"attachment-{i + 1}-{k}", "description": fix_text(fi.get("description")) or None,
                                        "size": fi.get("size"), "source": {"kind": "annotation", "page": i + 1, "index": k}})
                if a.type[1] not in ("Link", "Widget", "Popup"):
                    annots += 1
            for w in p.widgets() or []:
                fields.append({"name": w.field_name, "kind": w.field_type_string, "value": w.field_value if w.field_value not in ("", None) else None,
                               "page": i + 1, "tooltip": w.field_label or None})
            for _xref, ext, kind, base, *_ in doc.get_page_fonts(i):
                name = SUBSET_RE.sub("", base or "") or "(unnamed)"
                f = fonts.setdefault((name, kind), {"name": name, "kind": kind, "embedded": False, "subset": False})
                f["embedded"] |= ext not in ("n/a", "")
                f["subset"] |= bool(SUBSET_RE.match(base or ""))
        except Exception as e:  # noqa: BLE001 — one broken page does not hide the rest of the document
            warnings.append(f"第 {i + 1} 頁：{e}")
            if len(pages) == i:
                pages.append([595.0, 842.0])
                labels.append(str(i + 1))
    images = len(seen_images)
    try:
        for name in doc.embfile_names():
            info = doc.embfile_info(name)
            attachments.append({"name": fix_text(info.get("ufilename") or info.get("filename")) or name, "description": fix_text(info.get("description")) or None,
                                "size": info.get("size"), "source": {"kind": "document", "key": name}})
    except Exception as e:  # noqa: BLE001
        warnings.append(f"附件：{e}")
    try:
        outline = _outline(doc)
    except Exception as e:  # noqa: BLE001
        outline, warnings = [], [*warnings, f"目錄：{e}"]
    layers = []
    try:
        layers = [{"name": c.get("text") or "", "visible": bool(c.get("on"))} for c in doc.layer_ui_configs()]
    except Exception:  # noqa: BLE001
        pass
    fmt = md.get("format") or ""
    md = {k: fix_text(v) if isinstance(v, str) else v for k, v in md.items()}
    return {
        "title": (md.get("title") or "").strip() or None, "author": (md.get("author") or "").strip() or None,
        "subject": (md.get("subject") or "").strip() or None, "keywords": (md.get("keywords") or "").strip() or None,
        "creator": (md.get("creator") or "").strip() or None, "producer": (md.get("producer") or "").strip() or None,
        "created": md.get("creationDate") or None, "modified": md.get("modDate") or None,
        "pdf_version": fmt.removeprefix("PDF ").strip() or None, "encrypted": bool(md.get("encryption")),
        "tagged": (_catalog_key(doc, "MarkInfo/Marked") or "") == "true",
        "has_javascript": _catalog_key(doc, "Names/JavaScript") is not None or _catalog_key(doc, "OpenAction/JS") is not None,
        "page_count": doc.page_count, "pages": pages,
        "labels": labels if any(lb != str(i + 1) for i, lb in enumerate(labels)) else None,
        "outline": outline, "attachments": attachments, "fields": fields, "layers": layers,
        "fonts": sorted(fonts.values(), key=lambda f: f["name"].lower()),
        "counts": {"links": links, "images": images, "annotations": annots, "attachments": len(attachments), "fields": len(fields)},
        "warnings": warnings,
    }


def image_bytes(doc, xref: int) -> tuple[bytes, str]:
    """A stored image as a browser can show it: its own encoding when that is png / jpeg / gif / webp, PNG otherwise
    (CMYK, JBIG2, raw samples). A soft mask is applied so transparency survives."""
    import pymupdf

    info = doc.extract_image(xref)
    if not info:
        raise ValueError("不是圖片")
    if info.get("ext") in ("png", "jpeg", "jpg", "gif", "webp") and not info.get("smask"):
        return info["image"], "jpeg" if info["ext"] == "jpg" else info["ext"]
    pix = pymupdf.Pixmap(doc, xref)
    if info.get("smask"):
        try:
            pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, info["smask"]))
        except Exception:  # noqa: BLE001 — a mask of another size: keep the image opaque
            pass
    if pix.colorspace and pix.colorspace.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return pix.tobytes("png"), "png"


def embedded_file(doc, source: dict) -> tuple[str, bytes]:
    """(file name, bytes) of an attachment listed by document_info."""
    if source.get("kind") == "document":
        info = doc.embfile_info(source["key"])
        return fix_text(info.get("ufilename") or info.get("filename")) or source["key"], doc.embfile_get(source["key"])
    page = doc[int(source["page"]) - 1]
    for k, a in enumerate(page.annots() or []):
        if k == int(source["index"]) and a.type[1] == "FileAttachment":
            return fix_text(a.file_info.get("filename")) or "attachment", a.get_file()
    raise KeyError("找不到這個附件")
