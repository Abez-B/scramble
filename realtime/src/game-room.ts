/**
 * GameRoom Durable Object — real-time coordination for one game.
 *
 * KEY FIX: We no longer use an in-memory `clients` Map to track connected
 * WebSockets.  The DO uses the Hibernation API (`state.acceptWebSocket`),
 * which means the DO can be evicted from memory between requests.  When it
 * wakes up for a /notify call the in-memory Map would be empty → sent=0 →
 * players never see the event even though they are connected.
 *
 * Instead we:
 *   1. Store per-connection metadata via `ws.serializeAttachment()` — this
 *      survives hibernation and is accessible on any wake-up.
 *   2. Enumerate live sockets via `this.state.getWebSockets()` — the runtime
 *      keeps track of all accepted sockets across hibernations.
 */

interface Env {
  NOTIFY_SECRET: string;
  BACKEND_URL: string;
  LEAD_TIME_MS: string;
}

interface ClientInfo {
  pid: string;
  role: string; // "player" | "host"
  team?: number;
  gameId: string;
}

interface GameEvent {
  type: string;
  sequence: number;
  executeAt?: number;
  [key: string]: unknown;
}

export class GameRoom implements DurableObject {
  private state: DurableObjectState;
  private env: Env;
  private sequence: number = 0;

  constructor(state: DurableObjectState, env: Env) {
    this.state = state;
    this.env = env;

    // Restore sequence from storage on every cold-start / wake-up.
    this.state.blockConcurrencyWhile(async () => {
      const stored = await this.state.storage.get<number>("sequence");
      if (stored !== undefined) this.sequence = stored;
    });
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/ws") {
      return this.handleWebSocket(request);
    }

    if (url.pathname === "/notify" && request.method === "POST") {
      return this.handleNotify(request);
    }

    if (url.pathname === "/connections") {
      const sockets = this.state.getWebSockets();
      return Response.json({
        count: sockets.length,
        players: sockets.map(ws => {
          const info = ws.deserializeAttachment() as ClientInfo | null;
          return { pid: info?.pid ?? "?", role: info?.role ?? "?" };
        }),
      });
    }

    return new Response("Not found", { status: 404 });
  }

  /**
   * Accept a WebSocket upgrade. The client connects with:
   *   wss://worker/ws/GAME_ID?pid=PID&role=player
   *
   * We store ClientInfo as a WebSocket attachment so it survives hibernation.
   */
  private handleWebSocket(request: Request): Response {
    const url = new URL(request.url);
    const pid = url.searchParams.get("pid") || "anonymous";
    const role = url.searchParams.get("role") || "player";
    const gameId = url.searchParams.get("gameId") || "";

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);

    // Register with the Hibernation API — the runtime keeps this socket alive
    // across DO hibernations.
    this.state.acceptWebSocket(server);

    // Persist client metadata in the attachment so we can read it on wake-up.
    server.serializeAttachment({ pid, role, gameId } satisfies ClientInfo);

    // Send initial acknowledgment.
    server.send(JSON.stringify({
      type: "CONNECTED",
      sequence: this.sequence,
      serverTime: Date.now(),
      connectedClients: this.state.getWebSockets().length,
    }));

    return new Response(null, { status: 101, webSocket: client });
  }

  /**
   * Handle incoming WebSocket messages from clients.
   * Called by the Durable Object runtime (Hibernation API).
   */
  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    if (typeof message !== "string") return;

    let data: { type: string; [key: string]: unknown };
    try {
      data = JSON.parse(message);
    } catch {
      return;
    }

    switch (data.type) {
      case "PING":
        // Clock synchronization: respond with server timestamp.
        ws.send(JSON.stringify({
          type: "PONG",
          serverTime: Date.now(),
          clientTime: data.clientTime,
        }));
        break;

      case "RESUME": {
        // Client reconnecting — tell it to refetch if it missed events.
        const lastSeq = (data.lastSequence as number) || 0;
        if (lastSeq < this.sequence) {
          ws.send(JSON.stringify({
            type: "STATE_SYNC",
            sequence: this.sequence,
            serverTime: Date.now(),
          }));
        } else {
          ws.send(JSON.stringify({
            type: "CAUGHT_UP",
            sequence: this.sequence,
          }));
        }
        break;
      }

      case "IDENTIFY": {
        // Update the attachment (e.g. after rejoin with a new team).
        const existing = ws.deserializeAttachment() as ClientInfo | null;
        if (existing) {
          const updated: ClientInfo = { ...existing };
          if (data.pid) updated.pid = data.pid as string;
          if (data.role) updated.role = data.role as string;
          if (data.team !== undefined) updated.team = data.team as number;
          ws.serializeAttachment(updated);
        }
        break;
      }

      default:
        break;
    }
  }

  /**
   * Handle WebSocket close.
   * No-op: `getWebSockets()` automatically excludes closed sockets.
   */
  async webSocketClose(_ws: WebSocket, _code: number, _reason: string, _wasClean: boolean): Promise<void> {
    // Nothing to do — the runtime removes the socket from getWebSockets() automatically.
  }

  /**
   * Handle WebSocket error.
   */
  async webSocketError(_ws: WebSocket, _error: unknown): Promise<void> {
    // Nothing to do — the runtime removes the socket from getWebSockets() automatically.
  }

  /**
   * Receive a notify event from the backend (FastAPI).
   * Increments sequence, adds executeAt for synced events, and broadcasts to
   * all live sockets via getWebSockets() — safe across hibernations.
   */
  private async handleNotify(request: Request): Promise<Response> {
    const body = await request.json() as Record<string, unknown>;
    const eventType = body.type as string;

    if (!eventType) {
      return Response.json({ error: "Missing event type" }, { status: 400 });
    }

    // Increment and persist sequence.
    this.sequence++;
    await this.state.storage.put("sequence", this.sequence);

    const leadTime = parseInt(this.env.LEAD_TIME_MS || "1000", 10);

    // Synced events get an executeAt so all clients act at the same moment.
    const syncedEvents = new Set(["NEXT", "BACK", "REDEAL", "GAME_START", "GAME_END"]);
    const event: GameEvent = {
      ...body,
      type: eventType,
      sequence: this.sequence,
    };

    if (syncedEvents.has(eventType)) {
      event.executeAt = Date.now() + leadTime;
    }

    const message = JSON.stringify(event);

    // ── Broadcast ──────────────────────────────────────────────────────────
    // IMPORTANT: use getWebSockets() not an in-memory Map.
    // After DO hibernation the in-memory Map is empty; getWebSockets() always
    // returns the live sockets maintained by the Cloudflare runtime.
    const sockets = this.state.getWebSockets();
    let sent = 0;

    for (const ws of sockets) {
      try {
        // Read per-connection metadata from the hibernation-safe attachment.
        const info = ws.deserializeAttachment() as ClientInfo | null;

        // Team-targeted events: only send to matching team (host always gets it).
        if (body.targetTeam !== undefined && info?.role !== "host") {
          if (info?.team !== body.targetTeam) continue;
        }

        ws.send(message);
        sent++;
      } catch {
        // Socket is dead — close it cleanly; runtime will drop it.
        try { ws.close(1011, "Send failed"); } catch { /* ignore */ }
      }
    }

    return Response.json({
      ok: true,
      sequence: this.sequence,
      sent,
      connected: sockets.length,
    });
  }
}
