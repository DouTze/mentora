# PyInstaller spec of the desktop sidecar. Build with `bun run desktop:sidecar` from the repo root
# (scripts/build-sidecar.ts), which runs this and copies the binary to src-tauri/binaries/.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# The app package also ships as data files: prompts, strategies and content are read from disk, and
# app/reading/gates.py hashes its own source files (and its neighbours') to version the gate rules.
datas = collect_data_files("app", include_py_files=True, excludes=["**/__pycache__/**"])

hiddenimports = (
    collect_submodules("app")
    # uvicorn picks its loop / protocol implementations by name at runtime
    + collect_submodules("uvicorn")
    + collect_submodules("langgraph")
    + collect_submodules("langgraph.checkpoint.sqlite")
    + ["aiosqlite", "multipart", "python_multipart"]
)

a = Analysis(
    ["desktop_main.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=hiddenimports,
    # optional heavy backends are not part of the desktop build
    excludes=["openvino", "openvino_genai", "tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="mentor-backend",
    debug=False,
    strip=False,
    upx=False,
    # keep a console build: a windowed build has no stdin/stdout on Windows, and the Tauri app needs both (it spawns
    # the sidecar with CREATE_NO_WINDOW, so no console window shows up)
    console=True,
    runtime_tmpdir=None,
)
