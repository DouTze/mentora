"""The person's book folder (~/.mentora): one copy of every imported book under a name of its own.

A copy is named after its content (the first 24 hex digits of its SHA-256, plus the extension), so two books never
share a name and the same file is recognised whatever it was called. What the name stands for is recorded here:
the original file name, where it came from (known for books imported from the home bookshelf; a browser upload
gives only the name), its hash and size, and — written by the model when one is connected — a short description and
an alias. A book removed from the library takes its copy with it."""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from .db import Database, now_iso

log = logging.getLogger("mentor.library")

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_files (
  material_id TEXT PRIMARY KEY,
  stored_name TEXT NOT NULL,
  original_name TEXT NOT NULL,
  original_path TEXT,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL,
  alias TEXT,
  description TEXT,
  described_by TEXT,
  described_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS library_files_sha ON library_files(sha256);
"""
FIELDS = ("stored_name", "original_name", "original_path", "sha256", "size", "alias", "description", "described_by",
          "described_at")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stored_name(digest: str, filename: str) -> str:
    """The copy's name: its content hash and the original extension."""
    ext = (Path(filename).suffix or ".bin").lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        ext = ".bin"
    return f"{digest[:24]}{ext}"


class Library:
    def __init__(self, db: Database, folder: Path):
        self.db = db
        self.folder = folder
        with db.conn() as c:
            c.executescript(SCHEMA)

    # -- records ----------------------------------------------------------------------------
    def get(self, mid: str) -> dict | None:
        with self.db.conn() as c:
            r = c.execute(f"SELECT {', '.join(FIELDS)} FROM library_files WHERE material_id=?", (mid,)).fetchone()
        return dict(r) if r else None

    def all(self) -> dict[str, dict]:
        with self.db.conn() as c:
            return {r["material_id"]: dict(r) for r in c.execute(f"SELECT material_id, {', '.join(FIELDS)} FROM library_files")}

    def by_hash(self, digest: str) -> str | None:
        """The material whose file has this content, if any."""
        with self.db.conn() as c:
            r = c.execute("SELECT material_id FROM library_files WHERE sha256=? ORDER BY created_at LIMIT 1", (digest,)).fetchone()
        return r["material_id"] if r else None

    def stored_names(self) -> set[str]:
        with self.db.conn() as c:
            return {r["stored_name"] for r in c.execute("SELECT stored_name FROM library_files")}

    def undescribed(self) -> list[str]:
        with self.db.conn() as c:
            return [r["material_id"] for r in c.execute("SELECT material_id FROM library_files WHERE described_at IS NULL "
                                                        "ORDER BY created_at")]

    def describe(self, mid: str, alias: str, description: str, model: str) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE library_files SET alias=?, description=?, described_by=?, described_at=? WHERE material_id=?",
                      (alias, description, model, now_iso(), mid))

    # -- the copy -----------------------------------------------------------------------------
    def keep(self, mid: str, filename: str, data: bytes, digest: str, original_path: str | None) -> dict:
        """Copy the file into the folder under its content name and record it. A failure to write (permissions, a
        full disk) is logged and the record kept: the copy is written again at the next start (adopt)."""
        name = stored_name(digest, filename)
        self._write(name, data)
        with self.db.conn() as c:
            c.execute("INSERT OR REPLACE INTO library_files (material_id, stored_name, original_name, original_path, sha256, "
                      "size, created_at) VALUES (?,?,?,?,?,?,?)",
                      (mid, name, Path(filename).name, original_path, digest, len(data), now_iso()))
        return self.get(mid) or {}

    def _write(self, name: str, data: bytes) -> Path | None:
        target = self.folder / name
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.stat().st_size == len(data):
                return target
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
            return target
        except OSError:
            log.warning("could not copy %s into %s", name, self.folder, exc_info=True)
            return None

    def remove(self, mid: str) -> None:
        """Forget the material's file and delete its copy (unless another record still points at the same copy, as
        two materials imported before duplicates were refused can)."""
        rec = self.get(mid)
        if not rec:
            return
        with self.db.conn() as c:
            c.execute("DELETE FROM library_files WHERE material_id=?", (mid,))
            shared = c.execute("SELECT 1 FROM library_files WHERE stored_name=?", (rec["stored_name"],)).fetchone()
        if not shared:
            try:
                (self.folder / rec["stored_name"]).unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove %s from %s", rec["stored_name"], self.folder, exc_info=True)

    # -- materials from before ------------------------------------------------------------------
    def adopt(self, materials: list[dict], own_copy) -> int:
        """Record the materials imported before this folder kept content names: hash the app's own copy, rename the
        folder's copy (found by its old name or by size and hash) to its content name, or write it again. Returns how
        many were recorded. `own_copy(material) -> Path` is the app's copy of a material."""
        known = self.all()
        done = 0
        for m in materials:
            if m["id"] in known:
                if not (self.folder / known[m["id"]]["stored_name"]).exists():   # a copy that could not be written
                    path = own_copy(m)
                    if path.exists():
                        self._write(known[m["id"]]["stored_name"], path.read_bytes())
                continue
            path = own_copy(m)
            if not path.exists():
                continue
            data = path.read_bytes()
            digest = sha256(data)
            name = stored_name(digest, m["filename"])
            old = self._old_copy(m["filename"], len(data), digest)
            try:
                if old and old.name != name and not (self.folder / name).exists():
                    old.rename(self.folder / name)
                elif old and old.name != name:
                    old.unlink()
            except OSError:
                log.warning("could not rename %s", old, exc_info=True)
            if not (self.folder / name).exists():
                self._write(name, data)
            with self.db.conn() as c:
                c.execute("INSERT OR IGNORE INTO library_files (material_id, stored_name, original_name, original_path, "
                          "sha256, size, created_at) VALUES (?,?,?,?,?,?,?)",
                          (m["id"], name, Path(m["filename"]).name, None, digest, len(data), m.get("created_at") or now_iso()))
            done += 1
        return done

    def _old_copy(self, filename: str, size: int, digest: str) -> Path | None:
        """The folder's copy of a file under its old naming (its own name, or "name (2)"…), checked by size and hash."""
        if not self.folder.is_dir():
            return None
        stem, ext = Path(filename).stem, Path(filename).suffix
        named = re.compile(rf"^{re.escape(stem)}( \(\d+\))?{re.escape(ext)}$")
        try:
            files = [p for p in self.folder.iterdir() if p.is_file()]
        except OSError:
            return None
        for p in sorted(files, key=lambda p: (not named.match(p.name), p.name)):
            try:
                if p.stat().st_size == size and sha256(p.read_bytes()) == digest:
                    return p
            except OSError:
                continue
        return None
