import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Badge, Dot } from "./components/ui";
import { healthTone, time, type Tone } from "./lib/format";
import { tzOf } from "./state/prefs";
import { useTerminal } from "./state/store";
import { InstrumentContext } from "./state/instrument";
import { Dashboard } from "./pages/Dashboard";
import { Charts } from "./pages/Charts";
import { Analysis } from "./pages/Analysis";
import { StrategyMonitor } from "./pages/Strategy";
import { Positions } from "./pages/Positions";
import { RiskCenter } from "./pages/Risk";
import { Journal } from "./pages/Journal";
import { Performance } from "./pages/Performance";
import { ShadowAnalytics } from "./pages/ShadowAnalytics";
import { MarketData } from "./pages/MarketData";
import { SystemHealth } from "./pages/System";
import { Settings } from "./pages/Settings";

export const PAGES: { id: string; label: string; ico: string; render(): ReactNode }[] = [
  { id: "dashboard", label: "Dashboard", ico: "DB", render: () => <Dashboard /> },
  { id: "charts", label: "Live Charts", ico: "CH", render: () => <Charts /> },
  { id: "analysis", label: "AI Market Analysis", ico: "AI", render: () => <Analysis /> },
  { id: "strategy", label: "Strategy Monitor", ico: "SM", render: () => <StrategyMonitor /> },
  { id: "positions", label: "Positions", ico: "PO", render: () => <Positions /> },
  { id: "risk", label: "Risk Center", ico: "RC", render: () => <RiskCenter /> },
  { id: "journal", label: "Trade Journal", ico: "TJ", render: () => <Journal /> },
  { id: "performance", label: "Performance", ico: "PF", render: () => <Performance /> },
  { id: "shadow", label: "Shadow Analytics", ico: "SA", render: () => <ShadowAnalytics /> },
  { id: "market", label: "Market Data", ico: "MD", render: () => <MarketData /> },
  { id: "system", label: "System Health", ico: "SH", render: () => <SystemHealth /> },
  { id: "settings", label: "Settings", ico: "ST", render: () => <Settings /> },
];

export function modeTone(mode?: string | null, dataLabel?: string | null): Tone {
  if (mode === "SHADOW") return "pos";
  if (mode === "REPLAY") return "info";
  if (dataLabel === "SYNTHETIC" || mode === "MOCK") return "warn";
  return "off";
}

function useRoute(start: string): [string, (p: string) => void] {
  const read = () => { const h = window.location.hash.replace(/^#\/?/, ""); return PAGES.some((p) => p.id === h) ? h : start; };
  const [page, setPage] = useState(read);
  useEffect(() => {
    const on = () => setPage(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  });
  return [page, (p) => { window.location.hash = `/${p}`; setPage(p); }];
}

function useClock(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => { const t = setInterval(() => setNow(new Date()), 1000); return () => clearInterval(t); }, []);
  return now;
}

export function feedProblem(state: ReturnType<typeof useTerminal>["state"]): { tone: Tone; text: string } | null {
  const m = state.market;
  if (!m) return null;
  const bad = Object.entries(m.instruments).filter(([, v]) => v.health !== "ok");
  if (!bad.length) return null;
  const allDown = bad.length === Object.keys(m.instruments).length;
  return { tone: allDown ? "neg" : "warn", text: bad.map(([r, v]) => `${r}: ${v.health}`).join(" · ") };
}

export function App() {
  const { state, prefs, setPrefs, markRead } = useTerminal();
  const [page, go] = useRoute(prefs.startPage);
  const [drawer, setDrawer] = useState(false);
  const now = useClock();
  const tz = tzOf(prefs);
  const meta = state.meta;
  const [instrument, setInstrument] = useState<string>(prefs.defaultInstrument);

  useEffect(() => {
    document.documentElement.dataset.theme = prefs.theme;
    document.documentElement.dataset.density = prefs.density;
  }, [prefs.theme, prefs.density]);

  const feed = feedProblem(state);
  const apiDown = state.api === "error" || state.live === "closed";
  const alerts = useMemo(() => {
    const a: Record<string, Tone> = {};
    if (feed) { a.market = feed.tone; a.dashboard = feed.tone; }
    const st = state.status;
    if (st && (st.guard.breached || st.guard.locked_permanently)) a.risk = "neg";
    else if (st && Object.values(st.news).some((n) => !n.safe)) a.risk = "warn";
    if (apiDown || meta?.state === "failed") a.system = "neg";
    if (st && Object.values(st.account.positions).some((q) => q !== 0)) a.positions = "info";
    return a;
  }, [feed, state.status, apiDown, meta?.state]);

  const shownNotes = state.notifications.filter((n) => prefs.notifications.enabled &&
    ["info", "success", "warning", "critical"].indexOf(n.severity) >= ["info", "info", "warning", "critical"].indexOf(prefs.notifications.minSeverity));
  const current = PAGES.find((p) => p.id === page) ?? PAGES[0];
  const lastUpd = state.lastUpdate ? new Date(state.lastUpdate).toISOString() : null;

  return (
    <div className={`shell ${prefs.sidebarCollapsed ? "collapsed" : ""}`}>
      <aside className="sidebar" aria-label="Navigation">
        <div className="brand"><div className="monogram">FTS</div><span className="brand-name">FTS Terminal</span></div>
        <nav className="nav">
          {PAGES.map((p) => (
            <button key={p.id} className={p.id === page ? "active" : ""} onClick={() => go(p.id)} title={p.label}
              aria-current={p.id === page ? "page" : undefined}>
              <span className="ico">{p.ico}</span><span className="nav-label">{p.label}</span>
              {alerts[p.id] && <span className="alert" style={{ background: `var(--${alerts[p.id]})` }} aria-label="warning" />}
            </button>
          ))}
        </nav>
        <div className="side-foot">
          <button onClick={() => setPrefs({ ...prefs, sidebarCollapsed: !prefs.sidebarCollapsed })}
            aria-label={prefs.sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}>
            {prefs.sidebarCollapsed ? "»" : "«"} <span className="side-foot-label">Collapse</span>
          </button>
        </div>
      </aside>

      <header className="topbar">
        <div className="top-item" title={meta?.description ?? ""}>
          <Badge tone={modeTone(meta?.mode, meta?.data_label)} className="mode" >{meta ? meta.mode : "…"}</Badge>
          {meta?.data_label && <Badge tone={meta.data_label === "LIVE" ? "pos" : "warn"}>{meta.data_label} DATA</Badge>}
          <Badge tone="off" title="No order can be sent from this application" className="hide-md">READ-ONLY · NO ORDERS</Badge>
        </div>
        <div className="top-item hide-md">
          <span className="top-label">Feed</span>
          <Dot tone={apiDown ? "neg" : feed ? feed.tone : state.market ? "pos" : "off"} />
          <span>{apiDown ? "API offline" : feed ? "degraded" : state.market ? "healthy" : "—"}</span>
        </div>
        <div className="top-item">
          <span className="top-label">Instrument</span>
          <select value={instrument} onChange={(e) => setInstrument(e.target.value)} aria-label="Selected instrument">
            {(meta?.instruments ?? ["MES", "MNQ"]).map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <span className="mono dim hide-md">{meta?.contracts?.[instrument] ?? ""}</span>
        </div>
        <div className="spacer" />
        <div className="top-item mono hide-sm" title={`Time zone: ${tz}`}>{time(now.toISOString(), false, tz)} <span className="dim">{tz === "UTC" ? "UTC" : tz.split("/").pop()}</span></div>
        <div className="top-item">
          <Dot tone={meta?.state === "running" && !apiDown ? "pos" : meta?.state === "failed" || apiDown ? "neg" : "warn"} />
          <span className="hide-sm">{apiDown ? "backend offline" : meta?.state ?? "…"}</span>
        </div>
        <button className="iconbtn" onClick={() => { setDrawer(!drawer); markRead(); }} aria-label="Notifications">
          Alerts{state.unread > 0 && <span className="count">{state.unread > 99 ? "99+" : state.unread}</span>}
        </button>
        <button className="iconbtn" onClick={() => go("settings")} aria-label="Settings">Settings</button>
      </header>

      <div className="banners">
        {apiDown && <div className="banner critical" role="alert">BACKEND DISCONNECTED — values below may be stale
          <span className="why">{state.apiError ?? state.liveInfo ?? ""} · last update {time(lastUpd, true, tz)}</span></div>}
        {meta?.state === "failed" && <div className="banner critical" role="alert">SESSION FAILED <span className="why">{meta.error ?? meta.outcome}</span></div>}
        {!apiDown && feed && <div className={`banner ${feed.tone === "neg" ? "critical" : "warning"}`} role="alert">MARKET FEED DEGRADED — entries blocked
          <span className="why">{feed.text}</span></div>}
        {meta?.data_label === "SYNTHETIC" && <div className="banner warning">SYNTHETIC DATA — NOT MARKET PRICES
          <span className="why">{meta.description}</span></div>}
      </div>

      <main className="main" id="main">
        <InstrumentContext.Provider value={{ instrument, setInstrument }}>{current.render()}</InstrumentContext.Provider>
      </main>

      <footer className="statusbar">
        <span className="sb-item"><Dot tone={state.api === "ok" ? "pos" : state.api === "error" ? "neg" : "warn"} /> API {state.api}</span>
        <span className="sb-item"><Dot tone={state.live === "open" ? "pos" : state.live === "closed" ? "neg" : "warn"} /> Stream {state.live}</span>
        <span className="sb-item"><Dot tone={healthTone(feed ? "degraded" : state.market ? "ok" : null)} /> Feed {String(state.market?.feed?.provider ?? state.market?.feed?.feed ?? "—")}</span>
        <span className="sb-item">Mode {meta?.mode ?? "—"} ({meta?.run_kind ?? "—"})</span>
        <span className="sb-item">Last update {time(lastUpd, false, tz)}</span>
        <span className="sb-item">Warnings {Object.keys(alerts).length}</span>
        <span className="sb-item" style={{ marginLeft: "auto" }}>{meta ? `${meta.app} v${meta.version}` : ""}</span>
      </footer>

      {drawer && (
        <aside className="drawer" aria-label="Notifications">
          <header className="panel-h"><h2>Notifications</h2><div className="right"><button className="iconbtn" onClick={() => setDrawer(false)}>Close</button></div></header>
          <div className="list">
            {shownNotes.length === 0 && <div className="empty"><strong>No notifications</strong></div>}
            {[...shownNotes].reverse().map((n) => (
              <div key={n.id} className="notif">
                <Dot tone={n.severity === "critical" ? "neg" : n.severity === "warning" ? "warn" : n.severity === "success" ? "pos" : "info"} />
                <div>
                  <div className="row"><strong>{n.title}</strong>{n.root && <span className="tag">{n.root}</span>}
                    <span className="dim mono" style={{ marginLeft: "auto" }}>{time(n.time, false, tz)}</span></div>
                  <div className="muted">{n.description}</div>
                  <div className="note upper">{n.category} · {n.severity}</div>
                </div>
              </div>
            ))}
          </div>
        </aside>
      )}
    </div>
  );
}
