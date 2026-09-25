// Dev server: serves the React app with HMR and proxies /api/* to the FastAPI backend.
// Binds to all interfaces by default so teammates on the same LAN can open http://<this-machine-ip>:5173;
// the backend itself stays on localhost and is only reached through this proxy. Set HOST=127.0.0.1 to keep it private.
import { networkInterfaces } from "node:os";
import index from "./src/index.html";

const BACKEND = process.env.MENTOR_BACKEND_URL ?? "http://localhost:8000";
const PORT = Number(process.env.PORT ?? 5173);
const HOST = process.env.HOST ?? "0.0.0.0";

Bun.serve({
  hostname: HOST,
  port: PORT,
  // Bun closes an idle connection after 10 s by default; a judging call on a small model can take longer.
  // 導讀 generation itself runs as background jobs (the page polls), so nothing needs more than this.
  idleTimeout: 255,
  development: { hmr: true, console: true },
  routes: {
    "/api/*": async (req) => {
      const url = new URL(req.url);
      const target = BACKEND + url.pathname + url.search;
      try {
        const res = await fetch(target, {
          method: req.method,
          headers: req.headers,
          body: req.method === "GET" || req.method === "HEAD" ? undefined : await req.arrayBuffer(),
        });
        return new Response(res.body, { status: res.status, headers: res.headers });
      } catch (e) {
        return Response.json({ code: "backend_unreachable", detail: `後端未啟動（${BACKEND}）：${e}`, retryable: true }, { status: 502 });
      }
    },
    "/*": index,
  },
});

function lanAddresses(): string[] {
  return Object.values(networkInterfaces())
    .flat()
    .filter((i) => i && i.family === "IPv4" && !i.internal)
    .map((i) => i!.address);
}

console.log(`mentor frontend → http://localhost:${PORT}  (api → ${BACKEND})`);
if (HOST === "0.0.0.0") {
  const lan = lanAddresses();
  if (lan.length) console.log(`                  區網夥伴可開：${lan.map((a) => `http://${a}:${PORT}`).join("  ")}`);
} else {
  console.log(`                  只綁定 ${HOST}，區網無法連入（拿掉 HOST 或設 HOST=0.0.0.0 才會開放）`);
}
