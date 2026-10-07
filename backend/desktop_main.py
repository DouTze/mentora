"""Entry point of the desktop sidecar (the PyInstaller-built backend that the Tauri app starts).

The Tauri shell picks a free port, points MENTOR_DATA_DIR at the per-user app data directory and keeps our stdin
open for as long as it runs. When stdin closes — the app quit, crashed or was force-killed — this process exits too,
so no backend is left running in the background. (PyInstaller's one-file bootloader runs us as a child process;
killing only the bootloader would orphan us, but the inherited stdin still closes.)

Run from source: python desktop_main.py --port 8000
"""
from __future__ import annotations

import argparse
import os
import sys
import threading


def _exit_when_stdin_closes() -> None:
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except (OSError, ValueError):
        pass
    os._exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mentor backend (desktop sidecar)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", help="where the database, imported materials and reading files live")
    parser.add_argument("--watch-stdin", action="store_true", help="exit when stdin closes (set by the Tauri app)")
    args = parser.parse_args()

    # config reads the env at import time, so set it before importing the app
    if args.data_dir:
        os.environ["MENTOR_DATA_DIR"] = args.data_dir
        os.makedirs(args.data_dir, exist_ok=True)
    if args.watch_stdin:
        threading.Thread(target=_exit_when_stdin_closes, name="stdin-watchdog", daemon=True).start()

    import uvicorn

    from app.main import app

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
