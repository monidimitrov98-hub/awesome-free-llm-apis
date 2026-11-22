import { Badge, Kv, Panel, Seg } from "../components/ui";
import { DEFAULT_PREFS, type Prefs } from "../state/prefs";
import { useTerminal } from "../state/store";

export function Settings() {
  const { state, prefs, setPrefs } = useTerminal();
  const set = <K extends keyof Prefs>(k: K, v: Prefs[K]) => setPrefs({ ...prefs, [k]: v });
  const meta = state.meta;
  return (
    <div className="stack">
      <div className="page-head"><h1>Settings</h1><span className="sub">Interface preferences only, stored in this browser.</span>
        <div className="tools"><button className="btn" onClick={() => setPrefs(DEFAULT_PREFS)}>Reset to defaults</button></div></div>
      <div className="grid g3">
        <Panel title="Appearance">
          <Kv rows={[
            ["Theme", <Seg value={prefs.theme} onChange={(v) => set("theme", v)} options={[{ value: "dark", label: "Dark" }, { value: "light", label: "Light" }]} />],
            ["Density", <Seg value={prefs.density} onChange={(v) => set("density", v)} options={[{ value: "compact", label: "Compact" }, { value: "comfortable", label: "Comfortable" }]} />],
            ["Sidebar", <Seg value={prefs.sidebarCollapsed ? "c" : "e"} onChange={(v) => set("sidebarCollapsed", v === "c")} options={[{ value: "e", label: "Expanded" }, { value: "c", label: "Collapsed" }]} />],
            ["Time zone", <select value={prefs.timezone} onChange={(e) => set("timezone", e.target.value as Prefs["timezone"])} aria-label="Time zone">
              <option value="America/New_York">New York (exchange session)</option><option value="UTC">UTC</option><option value="local">This computer</option></select>],
          ]} />
        </Panel>
        <Panel title="Charts & defaults">
          <Kv rows={[
            ["Default instrument", <Seg value={prefs.defaultInstrument} onChange={(v) => set("defaultInstrument", v)} options={[{ value: "MES", label: "MES" }, { value: "MNQ", label: "MNQ" }]} />],
            ["Default timeframe", <Seg value={prefs.defaultTimeframe} onChange={(v) => set("defaultTimeframe", v)} options={[{ value: "1min", label: "1m" }, { value: "5min", label: "5m" }, { value: "15min", label: "15m" }]} />],
            ["Start page", <select value={prefs.startPage} onChange={(e) => set("startPage", e.target.value)} aria-label="Start page">
              {["dashboard", "charts", "analysis", "strategy", "positions", "risk", "journal", "performance", "market", "system"].map((p) => <option key={p} value={p}>{p}</option>)}</select>],
            ...(Object.keys(prefs.indicators) as (keyof Prefs["indicators"])[]).map((k) => [`Show ${k}`,
              <input type="checkbox" checked={prefs.indicators[k]} onChange={() => set("indicators", { ...prefs.indicators, [k]: !prefs.indicators[k] })} aria-label={`Show ${k}`} />] as [string, JSX.Element]),
          ]} />
        </Panel>
        <Panel title="Notifications">
          <Kv rows={[
            ["In-app notifications", <input type="checkbox" checked={prefs.notifications.enabled} aria-label="Enable notifications"
              onChange={() => set("notifications", { ...prefs.notifications, enabled: !prefs.notifications.enabled })} />],
            ["Minimum severity", <Seg value={prefs.notifications.minSeverity} onChange={(v) => set("notifications", { ...prefs.notifications, minSeverity: v })}
              options={[{ value: "info", label: "Info" }, { value: "warning", label: "Warning" }, { value: "critical", label: "Critical" }]} />],
          ]} />
          <p className="note">Notifications only inform; they never change trading behaviour. The backend de-duplicates repeats.</p>
        </Panel>
      </div>
      <div className="grid g2">
        <Panel title="Data provider & startup (read-only)">
          <Kv rows={[["Current mode", meta?.mode], ["Data", meta?.data_label], ["Run kind", meta?.run_kind],
            ...Object.entries(meta?.modes ?? {}).map(([k, v]) => [`Mode ${k}`, v.available ? <span>{v.note ?? "available"}</span> : <Badge tone="off">{v.note ?? "unavailable"}</Badge>] as [string, JSX.Element])]} />
          <p className="note">The mode is chosen when the terminal starts (<span className="mono">python run_terminal.py --mode mock|replay|shadow</span>). It never switches automatically: a failed live feed stays visibly failed.</p>
        </Panel>
        <Panel title="Not configurable here (by design)">
          <p style={{ marginTop: 0 }}>AccountGuard limits, NewsGuard protection, risk limits, strategy parameters, execution permissions and broker credentials
            cannot be changed from the GUI. They live in the configuration files and are reviewed outside this application.</p>
          <Badge tone="off">Read-only application · no order entry</Badge>
        </Panel>
      </div>
    </div>
  );
}
