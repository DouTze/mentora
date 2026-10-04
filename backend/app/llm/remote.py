"""The remote model endpoint chosen in the settings: a URL and an access token for an Ollama behind the token proxy
(scripts/colab/: Colab, or any server running the same proxy). Choosing "remote" in the settings is the explicit
consent to send course text to that host.

The endpoint is a server-side setting, separate from ModelConfig: sessions record backend "remote" and the model,
never the URL (a Colab tunnel gets a new URL on every restart; the sessions keep working after it is updated) and
never the token. The token never leaves the server: the settings API only reports whether one is set."""
from __future__ import annotations

from ..db import Database

KEY = "remote_endpoint"
_db: Database | None = None


def configure(db: Database) -> None:
    global _db
    _db = db


def get() -> dict:
    """{"url", "token"} as saved ("" when unset)."""
    d = (_db.get_setting(KEY) if _db is not None else None) or {}
    return {"url": (d.get("url") or "").strip().rstrip("/"), "token": d.get("token") or ""}


def public() -> dict:
    d = get()
    return {"url": d["url"], "has_token": bool(d["token"])}


def save(url: str, token: str | None) -> dict:
    """Saves the URL; the token is replaced only when a new one is given (None / "" keeps the saved one)."""
    if _db is None:
        raise RuntimeError("remote endpoint store is not configured")
    cur = get()
    _db.set_setting(KEY, {"url": (url or "").strip().rstrip("/"), "token": token.strip() if token else cur["token"]})
    return public()
