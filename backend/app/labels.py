"""Names the person gives to things in the sidebar, kept apart from the things themselves: a guided reading (a
session) shows its range ("第 3–5 頁") unless it was named. Only what the sidebar shows changes; the session is
left as it is (its workflow owns it)."""
from __future__ import annotations

from .db import Database, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
  kind TEXT NOT NULL,
  ref TEXT NOT NULL,
  label TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (kind, ref)
);
"""
KINDS = ("session",)


class Labels:
    def __init__(self, db: Database):
        self.db = db
        with db.conn() as c:
            c.executescript(SCHEMA)

    def of(self, kind: str) -> dict[str, str]:
        with self.db.conn() as c:
            return {r["ref"]: r["label"] for r in c.execute("SELECT ref, label FROM labels WHERE kind=?", (kind,))}

    def set(self, kind: str, ref: str, label: str | None) -> dict[str, str]:
        """Name it; an empty name removes the name (back to the default)."""
        name = (label or "").strip()
        with self.db.conn() as c:
            if name:
                c.execute("INSERT INTO labels (kind, ref, label, updated_at) VALUES (?,?,?,?) "
                          "ON CONFLICT(kind, ref) DO UPDATE SET label=excluded.label, updated_at=excluded.updated_at",
                          (kind, ref, name, now_iso()))
            else:
                c.execute("DELETE FROM labels WHERE kind=? AND ref=?", (kind, ref))
        return self.of(kind)
