import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from "react";
import { getJSON, getToken } from "../api/client";
import { LiveConnection, wsUrl, type LiveMsg, type LiveState } from "../api/live";
import type { Activity, Market, Meta, Notification, Snapshot, Status } from "../api/types";
import { loadPrefs, savePrefs, type Prefs } from "./prefs";

const MAX_ACTIVITY = 400;
const MAX_NOTIFICATIONS = 200;

export interface TerminalState {
  api: "loading" | "ok" | "error";
  apiError: string | null;
  live: LiveState;
  liveInfo: string | null;
  lastUpdate: number | null;
  meta: Meta | null;
  market: Market | null;
  status: Status | null;
  activity: Activity[];
  notifications: Notification[];
  unread: number;
  statusTick: number;
  activityTick: number;
}

const initial: TerminalState = {
  api: "loading", apiError: null, live: "connecting", liveInfo: null, lastUpdate: null, meta: null, market: null,
  status: null, activity: [], notifications: [], unread: 0, statusTick: 0, activityTick: 0,
};

type Action =
  | { type: "snapshot"; snap: Snapshot }
  | { type: "apiError"; error: string }
  | { type: "live"; state: LiveState; info?: string }
  | { type: "msg"; msg: LiveMsg }
  | { type: "readAll" };

function mergeActivity(prev: Activity[], items: Activity[]): Activity[] {
  const seen = new Set(prev.map((a) => a.seq));
  const add = items.filter((a) => !seen.has(a.seq));
  return add.length ? [...prev, ...add].slice(-MAX_ACTIVITY) : prev;
}

function mergeNotes(prev: Notification[], items: Notification[]): [Notification[], number] {
  const seen = new Set(prev.map((n) => n.id));
  const add = items.filter((n) => !seen.has(n.id));
  return [add.length ? [...prev, ...add].slice(-MAX_NOTIFICATIONS) : prev, add.length];
}

export function reducer(s: TerminalState, a: Action): TerminalState {
  switch (a.type) {
    case "snapshot": {
      // a fresh snapshot REPLACES derived lists (no duplicates after reconnect / backend restart)
      const sameBoot = s.meta?.boot_id === a.snap.meta.boot_id;
      const activity = sameBoot ? mergeActivity(s.activity, a.snap.activity) : a.snap.activity.slice(-MAX_ACTIVITY);
      const [notifications, added] = sameBoot ? mergeNotes(s.notifications, a.snap.notifications)
        : [a.snap.notifications.slice(-MAX_NOTIFICATIONS), a.snap.notifications.length];
      return { ...s, api: "ok", apiError: null, meta: a.snap.meta, market: a.snap.market, status: a.snap.status,
        activity, notifications, unread: sameBoot ? s.unread + added : added, lastUpdate: Date.now(),
        statusTick: s.statusTick + 1, activityTick: s.activityTick + 1 };
    }
    case "apiError": return { ...s, api: "error", apiError: a.error };
    case "live": return { ...s, live: a.state, liveInfo: a.info ?? null };
    case "msg": {
      const m = a.msg;
      const base = { ...s, lastUpdate: Date.now() };
      if (m.type === "market") return { ...base, market: m.data as Market };
      if (m.type === "status") return { ...base, status: m.data as Status, statusTick: s.statusTick + 1 };
      if (m.type === "activity") return { ...base, activity: mergeActivity(s.activity, m.data as Activity[]), activityTick: s.activityTick + 1 };
      if (m.type === "notification") {
        const [notifications, added] = mergeNotes(s.notifications, [m.data as Notification]);
        return { ...base, notifications, unread: s.unread + added };
      }
      return base;
    }
    case "readAll": return { ...s, unread: 0 };
  }
}

interface Ctx { state: TerminalState; prefs: Prefs; setPrefs(p: Prefs): void; refresh(): void; markRead(): void }
const TerminalContext = createContext<Ctx | null>(null);

export function TerminalProvider({ children, connect = true }: { children: ReactNode; connect?: boolean }) {
  const [state, dispatch] = useReducer(reducer, initial);
  const [prefs, setPrefsState] = useReducer((_: Prefs, p: Prefs) => p, undefined, loadPrefs);
  const inflight = useRef(false);

  const refresh = useCallback(() => {
    if (inflight.current) return;
    inflight.current = true;
    getJSON<Snapshot>("/api/snapshot")
      .then((snap) => dispatch({ type: "snapshot", snap }))
      .catch((e: Error) => dispatch({ type: "apiError", error: e.message }))
      .finally(() => { inflight.current = false; });
  }, []);

  useEffect(() => {
    if (!connect) return;
    refresh();
    const live = new LiveConnection(wsUrl(), getToken, {
      onState: (st, info) => dispatch({ type: "live", state: st, info }),
      onMessage: (msg) => dispatch({ type: "msg", msg }),
      onResync: () => refresh(),
    });
    live.start();
    return () => live.stop();
  }, [connect, refresh]);

  const setPrefs = useCallback((p: Prefs) => { savePrefs(p); setPrefsState(p); }, []);
  const markRead = useCallback(() => dispatch({ type: "readAll" }), []);
  const value = useMemo(() => ({ state, prefs, setPrefs, refresh, markRead }), [state, prefs, setPrefs, refresh, markRead]);
  return <TerminalContext.Provider value={value}>{children}</TerminalContext.Provider>;
}

export function useTerminal(): Ctx {
  const c = useContext(TerminalContext);
  if (!c) throw new Error("useTerminal outside TerminalProvider");
  return c;
}

export { TerminalContext };
