import { describe, expect, it, vi } from "vitest";
import { LiveConnection, BACKOFF_MS, WATCHDOG_MS } from "../src/api/live";
import { initToken, qs } from "../src/api/client";
import { reducer, type TerminalState } from "../src/state/store";
import { DEFAULT_PREFS, validatePrefs } from "../src/state/prefs";
import { money, pct, price, healthTone, UNAVAILABLE } from "../src/lib/format";
import { toMarkers } from "../src/components/Charts";
import { signalRows } from "../src/pages/Charts";
import { snapshot } from "./fixtures";

function init(): TerminalState {
  return { api: "loading", apiError: null, live: "connecting", liveInfo: null, lastUpdate: null, meta: null, market: null,
    status: null, activity: [], notifications: [], unread: 0, statusTick: 0, activityTick: 0 };
}

describe("store reducer", () => {
  it("deduplicates activity and notifications across snapshot + live updates", () => {
    let s = reducer(init(), { type: "snapshot", snap: snapshot() });
    expect(s.activity).toHaveLength(2);
    s = reducer(s, { type: "msg", msg: { type: "activity", seq: 10, boot_id: "boot1", data: [snapshot().activity[1], { ...snapshot().activity[1], seq: 3 }] } });
    expect(s.activity.map((a) => a.seq)).toEqual([1, 2, 3]);
    s = reducer(s, { type: "snapshot", snap: snapshot() }); // reconnect: same boot -> merge, no duplicates
    expect(s.activity.map((a) => a.seq)).toEqual([1, 2, 3]);
    expect(s.notifications).toHaveLength(1);
  });
  it("replaces derived lists when the backend restarted (new boot id)", () => {
    let s = reducer(init(), { type: "snapshot", snap: snapshot() });
    const fresh = snapshot();
    fresh.meta.boot_id = "boot2";
    fresh.activity = [{ ...fresh.activity[0], seq: 1, summary: "new run" }];
    s = reducer(s, { type: "snapshot", snap: fresh });
    expect(s.activity.map((a) => a.summary)).toEqual(["new run"]);
  });
  it("bounds activity history", () => {
    let s = reducer(init(), { type: "snapshot", snap: snapshot() });
    const many = Array.from({ length: 1000 }, (_, i) => ({ ...snapshot().activity[0], seq: 100 + i }));
    s = reducer(s, { type: "msg", msg: { type: "activity", seq: 11, boot_id: "boot1", data: many } });
    expect(s.activity.length).toBeLessThanOrEqual(400);
  });
});

class FakeWS {
  static last: FakeWS;
  sent: string[] = [];
  onopen: (() => void) | null = null; onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null; onerror: (() => void) | null = null;
  constructor(public url: string) { FakeWS.last = this; }
  send(t: string) { this.sent.push(t); }
  close() { this.onclose?.({ code: 1000 }); }
  emit(m: object) { this.onmessage?.({ data: JSON.stringify(m) }); }
}

describe("LiveConnection", () => {
  it("authenticates first, applies messages in order, resyncs on gaps, ignores duplicates, reconnects with back-off", () => {
    vi.useFakeTimers();
    const msgs: string[] = [], resyncs: string[] = [], states: string[] = [];
    const c = new LiveConnection("ws://x/ws", () => "TOKEN", {
      onState: (s) => states.push(s), onMessage: (m) => msgs.push(`${m.type}:${m.seq}`), onResync: (r) => resyncs.push(r),
    }, (u) => new FakeWS(u) as unknown as WebSocket);
    c.start();
    const ws = FakeWS.last;
    ws.onopen?.();
    expect(JSON.parse(ws.sent[0])).toEqual({ type: "auth", token: "TOKEN" });
    ws.emit({ type: "hello", seq: 5, boot_id: "b" });
    ws.emit({ type: "market", seq: 6, boot_id: "b", data: {} });
    ws.emit({ type: "market", seq: 6, boot_id: "b", data: {} }); // duplicate
    ws.emit({ type: "status", seq: 9, boot_id: "b", data: {} });  // gap -> resync, not applied
    ws.emit({ type: "status", seq: 10, boot_id: "other", data: {} }); // restart -> resync
    expect(msgs).toEqual(["market:6"]);
    expect(resyncs).toEqual(["connected", "message gap", "backend restarted"]);
    ws.close();
    expect(states[states.length - 1]).toBe("closed");
    vi.advanceTimersByTime(BACKOFF_MS[0] + 1);
    expect(FakeWS.last).not.toBe(ws);
    const ws2 = FakeWS.last;
    ws2.onopen?.();
    vi.advanceTimersByTime(WATCHDOG_MS + 1); // silent server -> watchdog closes and reconnects
    expect(states).toContain("closed");
    c.stop();
    vi.useRealTimers();
  });
});

describe("token handling", () => {
  it("takes the token from the URL fragment only, then removes it", () => {
    const store = new Map<string, string>();
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) } as unknown as Storage;
    window.history.replaceState(null, "", "/#token=abcdefghijklmnopqrstuvwxyz012345");
    expect(initToken(window.location, storage)).toBe("abcdefghijklmnopqrstuvwxyz012345");
    expect(window.location.hash).toBe("");
    expect(initToken(window.location, storage)).toBe("abcdefghijklmnopqrstuvwxyz012345");
    expect(qs({ a: "1", b: null, c: "" })).toBe("?a=1");
  });
});

describe("preferences", () => {
  it("rejects invalid stored values and falls back to safe defaults", () => {
    expect(validatePrefs({ theme: "neon", defaultInstrument: "ES", indicators: { vwap: "yes" }, startPage: "javascript:" }))
      .toEqual(DEFAULT_PREFS);
    expect(validatePrefs(null)).toEqual(DEFAULT_PREFS);
    expect(validatePrefs({ theme: "light" }).theme).toBe("light");
  });
});

describe("formatting never invents values", () => {
  it("shows UNAVAILABLE for missing / non-finite numbers", () => {
    for (const v of [null, undefined, NaN, Infinity, "12"]) {
      expect(price(v)).toBe(UNAVAILABLE); expect(money(v)).toBe(UNAVAILABLE); expect(pct(v)).toBe(UNAVAILABLE);
    }
    expect(money(-12.5)).toBe("-$12.50"); expect(money(3, { sign: true })).toBe("+$3.00");
    expect(healthTone("ok")).toBe("pos"); expect(healthTone("feed_disconnected")).toBe("neg"); expect(healthTone("market_data_stale")).toBe("warn");
  });
});

describe("chart markers", () => {
  const show = { vwap: true, emaFast: true, emaSlow: true, volume: true, signals: true, fills: true, levels: true };
  it("snaps markers to bars, keeps kinds distinct and drops markers outside the data", () => {
    const times = [600, 900, 1200];
    const out = toMarkers([
      { kind: "signal", time: "1970-01-01T00:20:00Z", bar_time: "1970-01-01T00:15:00Z", direction: "long" },
      { kind: "fill_entry", time: "1970-01-01T00:16:01Z", side: "BUY", direction: "long", price: 10 },
      { kind: "rejected", time: "1970-01-01T05:00:00Z", reason: "x" },
    ], times, "5min", show);
    expect(out.map((m) => [m.time, m.shape])).toEqual([[900, "arrowUp"], [900, "arrowUp"]]);
    expect(toMarkers([{ kind: "fill_entry", time: "1970-01-01T00:16:01Z" }], times, "5min", { ...show, fills: false })).toEqual([]);
  });
  it("joins every signal with its own authorization outcome", () => {
    const rows = signalRows([
      { kind: "signal", time: "t1", bar_time: "2025-01-01T10:00:00Z", signal_id: "a", direction: "long", reference: 1, stop: 0.5, target: 2 },
      { kind: "rejected", time: "2025-01-01T10:05:00Z", signal_id: "a", reason: "news_blackout_active" },
      { kind: "signal", time: "t2", bar_time: "2025-01-01T11:00:00Z", signal_id: "b", direction: "short" },
      { kind: "authorized", time: "2025-01-01T11:05:00Z", signal_id: "b" },
    ]);
    expect(rows.map((r) => [r.key, r.outcome, r.reason])).toEqual([["b", "authorized", ""], ["a", "rejected", "news_blackout_active"]]);
  });
});
