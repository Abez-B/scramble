/**
 * Scramble Realtime Worker — entry point.
 *
 * Routes:
 *   GET  /ws/:gameId?pid=...&role=...   → WebSocket upgrade → GameRoom DO
 *   POST /notify/:gameId                → Notify GameRoom DO → broadcast
 *   GET  /health                        → Health check
 *   GET  /connections/:gameId           → Connection count for a game
 *
 * The Worker itself is stateless; all per-game state lives in the
 * GameRoom Durable Object, keyed by gameId.
 */

import { GameRoom } from "./game-room";

interface Env {
  GAME_ROOM: DurableObjectNamespace;
  NOTIFY_SECRET: string;
  BACKEND_URL: string;
  LEAD_TIME_MS: string;
  ALLOWED_ORIGINS: string; // comma-separated, e.g. "https://scrambles.fly.dev"
}

export { GameRoom };

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // CORS preflight
    if (request.method === "OPTIONS") {
      return corsResponse(env, new Response(null, { status: 204 }));
    }

    // Route: /health
    if (url.pathname === "/health") {
      return corsResponse(env, Response.json({ ok: true, ts: Date.now() }));
    }

    // Extract gameId from path: /ws/:gameId or /notify/:gameId or /connections/:gameId
    const parts = url.pathname.split("/").filter(Boolean);

    if (parts.length < 2) {
      return corsResponse(env, new Response("Not found", { status: 404 }));
    }

    const [action, gameId] = parts;

    if (!gameId) {
      return corsResponse(env, Response.json({ error: "Missing gameId" }, { status: 400 }));
    }

    // Get the Durable Object stub for this gameId
    const doId = env.GAME_ROOM.idFromName(gameId);
    const stub = env.GAME_ROOM.get(doId);

    // Route: GET /ws/:gameId — WebSocket upgrade
    if (action === "ws" && request.method === "GET") {
      const upgradeHeader = request.headers.get("Upgrade");
      if (upgradeHeader !== "websocket") {
        return corsResponse(env, new Response("Expected WebSocket upgrade", { status: 426 }));
      }

      // Forward to DO with query params intact
      const doUrl = new URL(request.url);
      doUrl.pathname = "/ws";
      doUrl.searchParams.set("gameId", gameId);

      return stub.fetch(new Request(doUrl.toString(), request));
    }

    // Route: POST /notify/:gameId — Backend notification
    if (action === "notify" && request.method === "POST") {
      // Authenticate: check shared secret
      const authHeader = request.headers.get("Authorization");
      const expectedAuth = `Bearer ${env.NOTIFY_SECRET}`;

      if (!authHeader || authHeader !== expectedAuth) {
        return corsResponse(env, Response.json({ error: "Unauthorized" }, { status: 401 }));
      }

      // Forward to DO
      const doUrl = new URL(request.url);
      doUrl.pathname = "/notify";

      const resp = await stub.fetch(new Request(doUrl.toString(), {
        method: "POST",
        headers: request.headers,
        body: request.body,
      }));

      return corsResponse(env, resp);
    }

    // Route: GET /connections/:gameId — connection info
    if (action === "connections" && request.method === "GET") {
      const doUrl = new URL(request.url);
      doUrl.pathname = "/connections";

      const resp = await stub.fetch(new Request(doUrl.toString()));
      return corsResponse(env, resp);
    }

    return corsResponse(env, new Response("Not found", { status: 404 }));
  },
};

/**
 * Wrap a response with CORS headers.
 */
function corsResponse(env: Env, response: Response): Response {
  const headers = new Headers(response.headers);
  const allowed = env.ALLOWED_ORIGINS || "*";

  headers.set("Access-Control-Allow-Origin", allowed);
  headers.set("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  headers.set("Access-Control-Allow-Headers", "Content-Type, Authorization");
  headers.set("Access-Control-Max-Age", "86400");

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}
