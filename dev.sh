#!/usr/bin/env bash
# Same as `bun run dev`: start backend (FastAPI :8000) and frontend (bun :5173). Ctrl-C stops both.
cd "$(dirname "$0")"
exec bun scripts/dev.ts
