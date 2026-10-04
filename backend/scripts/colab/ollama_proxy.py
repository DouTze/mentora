"""Token-checking proxy in front of a remote Ollama (runs on the Colab VM, behind a cloudflared tunnel).

    PROXY_TOKEN=... python ollama_proxy.py          # listens on 127.0.0.1:8000, forwards to 127.0.0.1:11434

Only the four endpoints the mentor adapter uses are forwarded (chat, show, tags, version); pull, delete, create and
everything else are refused, and every request needs `Authorization: Bearer <PROXY_TOKEN>`. Cloudflare closes a
request whose response has not started within ~100 s, so /api/chat answers 200 at once, sends a space every 15 s
while the model works, then the JSON body (leading whitespace is valid JSON); an upstream failure arrives as
{"error": ...}, which the adapter (app/llm/ollama.py) turns into an LLMError. Needs fastapi/starlette, httpx, uvicorn."""
from __future__ import annotations

import asyncio
import hmac
import json
import os

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

UPSTREAM = os.environ.get("OLLAMA_UPSTREAM", "http://127.0.0.1:11434")
HEARTBEAT_S = float(os.environ.get("PROXY_HEARTBEAT_S", "15"))
ALLOWED = {("POST", "/api/chat"), ("POST", "/api/show"), ("GET", "/api/tags"), ("GET", "/api/version")}


def authorized(request: Request, token: str) -> bool:
    return bool(token) and hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}")


def build_app(token: str, upstream: str = UPSTREAM, transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    client = httpx.AsyncClient(base_url=upstream, timeout=httpx.Timeout(None, connect=10.0), transport=transport)

    async def chat(body: bytes) -> bytes:
        try:
            r = await client.post("/api/chat", content=body, headers={"content-type": "application/json"})
        except httpx.HTTPError as e:
            return json.dumps({"error": f"upstream unreachable: {e}"}).encode()
        if r.status_code != 200:
            try:
                msg = r.json().get("error") or r.text
            except ValueError:
                msg = r.text
            return json.dumps({"error": f"upstream {r.status_code}: {msg[:500]}"}).encode()
        return r.content

    async def handle(request: Request) -> Response:
        if (request.method, request.url.path) not in ALLOWED:
            return JSONResponse({"error": "not allowed"}, status_code=404)
        if not authorized(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.body()
        if request.url.path != "/api/chat":
            r = await client.request(request.method, request.url.path, content=body or None,
                                     headers={"content-type": "application/json"})
            return Response(r.content, status_code=r.status_code, media_type="application/json")

        async def stream():
            task = asyncio.ensure_future(chat(body))
            while True:
                done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_S)
                if done:
                    break
                yield b" "
            yield task.result()
        return StreamingResponse(stream(), media_type="application/json")

    routes = [Route(path, handle, methods=[method]) for method, path in ALLOWED]
    return Starlette(routes=routes)


if __name__ == "__main__":
    import uvicorn
    token = os.environ.get("PROXY_TOKEN", "")
    if len(token) < 24:
        raise SystemExit("set PROXY_TOKEN (at least 24 characters)")
    uvicorn.run(build_app(token), host="127.0.0.1", port=int(os.environ.get("PROXY_PORT", "8000")), log_level="warning")
