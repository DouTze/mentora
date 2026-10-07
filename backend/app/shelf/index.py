"""The readable-file index behind the home page's bookshelf: one fast, bounded walk over the folders a person keeps
books in, cached on disk so the shelf can be drawn before any model has been asked anything.

The walk is the "fixed way" the scout explores the machine: known roots only, hidden / system / build folders
skipped, code projects reduced to their PDFs, a cap on directories and on wall time. No file content is read here;
PDF metadata is read later and only for the few candidates that may reach the shelf."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

KINDS = {".pdf": "pdf", ".md": "md", ".markdown": "md", ".txt": "txt"}
SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "venv", ".venv", "env", "build", "dist", "target", "out",
             "Library", "AppData", "Applications", "Pictures", "Movies", "Music", "Photos Library.photoslibrary",
             "vendor", "bower_components", "Pods", "DerivedData", "tmp", "temp", "cache", "Caches", "logs"}
PROJECT_MARKERS = {"package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", ".git", "setup.py"}
NOISE_NAMES = re.compile(r"^(readme|changelog|license|licence|contributing|code_of_conduct|security|notice|authors|todo|"
                         r"requirements|cmakelists|robots|copying|history)\b", re.I)
# below these a file is a note, a config or an export, not something to read through
MIN_BYTES = {"pdf": 120_000, "md": 6_000, "txt": 12_000}
MAX_BYTES = 400_000_000
INDEX_VERSION = 3   # bump when the walk's rules change, so an index.json from older rules is not reused
MAX_DEPTH = 6
MAX_DIRS = 6_000
TIME_BUDGET_S = 3.0
SF_DATALESS = 0x40000000                 # macOS: file content lives in the cloud (iCloud Drive, OneDrive)
WIN_CLOUD_ATTRS = 0x00400000 | 0x00040000 | 0x00001000   # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE


def is_cloud_placeholder(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS or getattr(st, "st_file_attributes", 0) & WIN_CLOUD_ATTRS)


def candidate_roots() -> list[tuple[Path, bool, bool]]:
    """Folders offered in the shelf's scope settings, as (path, is a cloud drive, starts switched on). Only the
    library folder (~/.mentora, where every import is copied) starts on: the shelf looks nowhere else until the
    person says so. The usual book folders and cloud drives are offered, switched off."""
    env = os.environ.get("MENTOR_SHELF_ROOTS")
    if env is not None:
        return [(Path(p).expanduser(), False, True) for p in env.split(os.pathsep) if p.strip()]
    from .. import config
    home = Path.home()
    roots = [(config.LIBRARY_DIR, False, True)] + [(home / n, False, False) for n in ("Books", "Documents", "Downloads", "Desktop")]
    if sys.platform == "darwin":
        roots.append((home / "Library" / "Mobile Documents" / "com~apple~CloudDocs", True, False))   # iCloud Drive
    roots += [(home / n, True, False) for n in ("OneDrive", "Dropbox", "Google Drive")]
    return [r for r in roots if r[2] or r[0].is_dir()]


def default_roots() -> list[Path]:
    return [r for r, _, on in candidate_roots() if on]


def _under(path: str, folders: list[str]) -> bool:
    return any(path == f or path.startswith(f.rstrip(os.sep) + os.sep) for f in folders)


@dataclass
class Entry:
    id: str
    path: str
    name: str          # file name without extension, cleaned for display
    kind: str
    size: int
    mtime: float
    folder: str        # the parent folder's name: often the best hint of what a file is about
    cloud: bool = False   # an iCloud / OneDrive placeholder: opening it would download it, so the scout never does
    parts: int = 0        # a series: `path` is a folder of numbered chapter files (a web novel), read as one book


def entry_id(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


_JUNK = re.compile(r"[\(\[\{](?:z-?lib(?:rary)?(?:\.org)?|libgen|ebook|pdfdrive|www\.[^\)\]\}]+|[0-9a-f]{8,})[\)\]\}]", re.I)


MACHINE_DIR = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-|^[0-9a-f]{24,}$", re.I)   # uuid / hash: generated output
CHAPTER = re.compile(r"^(.*?)[\s_\-.]*(?:第\s*)?(\d{1,5})(?:\s*[章話回集节節])?$")          # 書名 0476 / 書名 第12章
LEADING_CHAPTER = re.compile(r"^(?:第\s*)?(\d{1,5})(?:\s*[章話回集节節])?[\s_\-.、]+\S")          # 026 靈魂棋
SERIES_MIN = 5


def chapter_of(stem: str) -> tuple[str, int] | None:
    """(series name, chapter number); a leading number names no series, so the folder's name is used."""
    if m := LEADING_CHAPTER.match(stem.strip()):
        return "", int(m.group(1))
    m = CHAPTER.match(stem.strip())
    return (m.group(1).strip(" -_.") or "", int(m.group(2))) if m else None


def series_files(folder: str) -> list[str]:
    """The chapter files of a series folder, in reading order (by chapter number)."""
    files = []
    for n in os.listdir(folder):
        stem, ext = os.path.splitext(n)
        ch = chapter_of(stem)
        if ext.lower() in (".txt", ".md", ".markdown") and ch:
            files.append((ch[1], os.path.join(folder, n)))
    return [p for _, p in sorted(files)]


def merge_series(entries: list[Entry]) -> list[Entry]:
    """A folder where most text files are one name plus a chapter number becomes a single book."""
    by_folder: dict[str, list[Entry]] = {}
    for e in entries:
        if e.kind != "pdf":
            by_folder.setdefault(os.path.dirname(e.path), []).append(e)
    out = [e for e in entries if e.kind == "pdf"]
    for folder, es in by_folder.items():
        chapters = [(e, chapter_of(os.path.splitext(os.path.basename(e.path))[0])) for e in es]
        names = [c[0] for _, c in chapters if c]
        top = max(set(names), key=names.count) if names else None
        if top is not None and len(es) >= SERIES_MIN and names.count(top) >= .6 * len(es):
            series = [e for e, c in chapters if c and c[0] == top]
            title = clean_title(top) if top else os.path.basename(folder)
            out.append(Entry(id=entry_id(folder), path=folder, name=title, kind=series[0].kind, size=sum(e.size for e in series),
                             mtime=max(e.mtime for e in series), folder=os.path.basename(os.path.dirname(folder)),
                             cloud=any(e.cloud for e in series), parts=len(series)))
            out += [e for e, c in chapters if not (c and c[0] == top)]
        else:
            out += es
    return out


def clean_title(stem: str) -> str:
    s = _JUNK.sub(" ", stem)
    s = re.sub(r"[\s_\-]+\d{12,14}$", "", s)   # export timestamps: 學習指引 20251222101907
    s = re.sub(r"[_]+|(?<=\w)\.(?=\w)|\s+-\s+", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -_.")
    return s or stem


def scan(roots: list[Path], *, exclude: list[Path] = (), budget_s: float = TIME_BUDGET_S, max_dirs: int = MAX_DIRS) -> list[Entry]:
    """Breadth-first, so the shallow folders people actually browse are covered before the budget runs out.
    `exclude`: folders the person keeps out of the app; nothing under them is listed, not even its name."""
    t0, seen_dirs, out = time.monotonic(), 0, []
    excluded = [os.path.realpath(p) for p in exclude]
    # (folder, depth, inside a code project): a project's docs/ and notes/ are its own, not reading material
    queue: list[tuple[Path, int, bool]] = [(r, 0, False) for r in roots]
    visited: set[str] = set()
    while queue and seen_dirs < max_dirs and time.monotonic() - t0 < budget_s:
        folder, depth, in_project = queue.pop(0)
        try:
            real = os.path.realpath(folder)
            if real in visited or _under(real, excluded):
                continue
            visited.add(real)
            with os.scandir(folder) as it:
                items = list(it)
        except OSError:
            continue
        seen_dirs += 1
        project = in_project or any(i.name in PROJECT_MARKERS for i in items)
        for i in items:
            if i.name.startswith(".") or i.name.startswith("~$"):
                continue
            try:
                if i.is_dir(follow_symlinks=False):
                    if (depth < MAX_DEPTH and i.name not in SKIP_DIRS and not MACHINE_DIR.search(i.name)
                            and not i.name.endswith((".app", ".photoslibrary", ".bundle"))):
                        queue.append((Path(i.path), depth + 1, project))
                    continue
                kind = KINDS.get(os.path.splitext(i.name)[1].lower())
                if not kind or (project and kind != "pdf"):
                    continue
                stem = os.path.splitext(i.name)[0]
                if NOISE_NAMES.match(stem):
                    continue
                st = i.stat(follow_symlinks=False)
                if not (MIN_BYTES[kind] <= st.st_size <= MAX_BYTES):
                    continue
                out.append(Entry(id=entry_id(i.path), path=i.path, name=clean_title(stem), kind=kind, size=st.st_size,
                                 mtime=st.st_mtime, folder=folder.name, cloud=is_cloud_placeholder(st)))
            except OSError:
                continue
    return merge_series(out)


class ShelfIndex:
    """index.json under the shelf directory; `fresh` decides whether a walk is due."""

    MAX_AGE_S = 15 * 60

    def __init__(self, directory: Path, roots: list[Path] | None = None, exclude: list[Path] | None = None):
        self.dir = directory
        self.roots = roots if roots is not None else default_roots()
        self.exclude = exclude or []
        self.entries: list[Entry] = []
        self.scanned_at = 0.0
        self._load()

    @property
    def file(self) -> Path:
        return self.dir / "index.json"

    def _scope(self) -> dict:
        return {"roots": [str(r) for r in self.roots], "exclude": [str(p) for p in self.exclude]}

    def _load(self) -> None:
        try:
            d = json.loads(self.file.read_text(encoding="utf-8"))
            if d.get("version") == INDEX_VERSION and d.get("scope") == self._scope():
                self.entries = [Entry(**e) for e in d["entries"]]
                self.scanned_at = float(d["scanned_at"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def configure(self, roots: list[Path], exclude: list[Path]) -> None:
        """A new scope: the old index no longer describes what may be shown, so it is dropped until the next walk."""
        self.roots, self.exclude = roots, exclude
        self.entries, self.scanned_at = [], 0.0

    @property
    def fresh(self) -> bool:
        return bool(self.scanned_at) and time.time() - self.scanned_at < self.MAX_AGE_S

    def rescan(self) -> list[Entry]:
        self.entries = scan(self.roots, exclude=self.exclude)
        self.scanned_at = time.time()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({"version": INDEX_VERSION, "scope": self._scope(), "scanned_at": self.scanned_at,
                                         "entries": [asdict(e) for e in self.entries]}, ensure_ascii=False), encoding="utf-8")
        return self.entries

    def get(self, eid: str) -> Entry | None:
        return next((e for e in self.entries if e.id == eid), None)

    def folders(self, limit: int = 24) -> list[dict]:
        """Folders holding the most readable files, with a few sample names: the model's view of the machine."""
        groups: dict[str, list[Entry]] = {}
        for e in self.entries:
            groups.setdefault(e.folder, []).append(e)
        top = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:limit]
        return [{"folder": f, "count": len(es), "samples": [e.name[:60] for e in sorted(es, key=lambda e: -e.mtime)[:4]]}
                for f, es in top]
