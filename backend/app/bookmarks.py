"""Bookmarks: pages (lines, for a text) the reader marked in a material, each with the chapter it falls in.

The chapter is named by the reader when the bookmark is made (it knows the PDF's outline or the Markdown headings);
the server keeps it as given, so the sidebar lists a book's bookmarks without reading the book again."""
from __future__ import annotations

from .db import Database, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmarks (
  material_id TEXT NOT NULL,
  unit_no INTEGER NOT NULL,
  chapter TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (material_id, unit_no)
);
"""


class Bookmarks:
    def __init__(self, db: Database):
        self.db = db
        with db.conn() as c:
            c.executescript(SCHEMA)

    def all(self) -> dict[str, list[dict]]:
        """Every material's bookmarks, in page order."""
        out: dict[str, list[dict]] = {}
        with self.db.conn() as c:
            for r in c.execute("SELECT material_id, unit_no, chapter, created_at FROM bookmarks ORDER BY material_id, unit_no"):
                out.setdefault(r["material_id"], []).append({"unit_no": r["unit_no"], "chapter": r["chapter"], "created_at": r["created_at"]})
        return out

    def of(self, mid: str) -> list[dict]:
        return self.all().get(mid, [])

    def add(self, mid: str, unit_no: int, chapter: str | None) -> list[dict]:
        with self.db.conn() as c:
            c.execute("INSERT INTO bookmarks (material_id, unit_no, chapter, created_at) VALUES (?,?,?,?) "
                      "ON CONFLICT(material_id, unit_no) DO UPDATE SET chapter=excluded.chapter",
                      (mid, unit_no, (chapter or "").strip() or None, now_iso()))
        return self.of(mid)

    def remove(self, mid: str, unit_no: int) -> list[dict]:
        with self.db.conn() as c:
            c.execute("DELETE FROM bookmarks WHERE material_id=? AND unit_no=?", (mid, unit_no))
        return self.of(mid)

    def forget(self, mid: str) -> None:
        with self.db.conn() as c:
            c.execute("DELETE FROM bookmarks WHERE material_id=?", (mid,))
