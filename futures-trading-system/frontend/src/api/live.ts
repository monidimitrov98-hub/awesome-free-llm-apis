// WebSocket client: first-message auth, heartbeat watchdog, bounded exponential back-off, and
// gap / restart detection (seq + boot_id) that triggers a fresh REST snapshot instead of guessing.

export type LiveState = "connecting" | "open" | "closed";
export interface LiveMsg { type: string; seq: number; boot_id: string; ts?: string; data: unknown }

export interface LiveHandlers {
  onState(state: LiveState, info?: string): void;
  onMessage(msg: LiveMsg): void;
  onResync(reason: string): void;
}

export const BACKOFF_MS = [500, 1000, 2000, 4000, 8000, 15000];
export const WATCHDOG_MS = 15000; // the server sends a heartbeat every 5 s

export class LiveConnection {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private watchdog: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  lastSeq = 0;
  bootId: string | null = null;
  lastMessageAt = 0;

  constructor(private url: string, private token: () => string | null, private h: LiveHandlers,
              private factory: (url: string) => WebSocket = (u) => new WebSocket(u)) {}

  start(): void { this.stopped = false; this.open(); }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    if (this.watchdog) clearTimeout(this.watchdog);
    this.ws?.close();
    this.ws = null;
  }

  private open(): void {
    this.h.onState("connecting");
    let ws: WebSocket;
    try { ws = this.factory(this.url); } catch { this.schedule("connect failed"); return; }
    this.ws = ws;
    ws.onopen = () => { ws.send(JSON.stringify({ type: "auth", token: this.token() ?? "" })); this.kick(); };
    ws.onmessage = (ev) => {
      let msg: LiveMsg;
      try { msg = JSON.parse(String(ev.data)) as LiveMsg; } catch { return; }
      this.kick();
      this.lastMessageAt = Date.now();
      if (msg.type === "hello") {
        this.attempt = 0;
        const restarted = this.bootId !== null && msg.boot_id !== this.bootId;
        this.bootId = msg.boot_id;
        this.lastSeq = msg.seq;
        this.h.onState("open");
        this.h.onResync(restarted ? "backend restarted" : "connected");
        return;
      }
      if (msg.type === "resync") { this.lastSeq = msg.seq; this.h.onResync("client fell behind"); return; }
      if (msg.boot_id !== this.bootId) { this.bootId = msg.boot_id; this.lastSeq = msg.seq; this.h.onResync("backend restarted"); return; }
      if (msg.seq <= this.lastSeq && msg.type !== "pong") return; // duplicate / already applied
      if (msg.type !== "pong" && this.lastSeq && msg.seq !== this.lastSeq + 1) {
        this.lastSeq = msg.seq;
        this.h.onResync("message gap");
        return;
      }
      if (msg.type !== "pong") this.lastSeq = msg.seq;
      this.h.onMessage(msg);
    };
    ws.onclose = (ev) => { if (this.ws === ws) this.schedule(ev.code === 4401 ? "unauthorized" : "connection closed"); };
    ws.onerror = () => { /* onclose follows */ };
  }

  private kick(): void {
    if (this.watchdog) clearTimeout(this.watchdog);
    this.watchdog = setTimeout(() => { this.ws?.close(); }, WATCHDOG_MS);
  }

  private schedule(why: string): void {
    if (this.watchdog) clearTimeout(this.watchdog);
    this.ws = null;
    this.h.onState("closed", why);
    if (this.stopped) return;
    const delay = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)];
    this.attempt += 1;
    this.timer = setTimeout(() => this.open(), delay);
  }
}

export function wsUrl(loc: Location = window.location): string {
  return `${loc.protocol === "https:" ? "wss" : "ws"}://${loc.host}/ws`;
}
