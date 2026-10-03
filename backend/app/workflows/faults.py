"""Fault injection for restart tests: MENTOR_FAULT=<point>[,<point>] hard-exits the process (as a power loss would)
when execution reaches that point. Unset in normal runs; the points are no-ops then."""
from __future__ import annotations

import os

EXIT_CODE = 77


def hit(point: str) -> None:
    spec = os.environ.get("MENTOR_FAULT")
    if spec and point in spec.split(","):
        os._exit(EXIT_CODE)
