import { useMemo, useState } from "react";
import { CandleChart, type BarsPayload, type Marker } from "../components/Charts";
import { Badge, Empty, Panel, Seg, Table, useApi } from "../components/ui";
import { healthTone, label, price, ratio, time } from "../lib/format";
import { tzOf } from "../state/prefs";
import { useInstrument } from "../state/instrument";
import { useTerminal } from "../state/store";

type Tf = "1min" | "5min" | "15min";

export function Charts() {
  const { state, prefs, setPrefs } = useTerminal();
  const { instrument, setInstrument } = useInstrument();
  const [tf, setTf] = useState<Tf>(prefs.defaultTimeframe);
  const [autoScale, setAutoScale] = useState(true);
  const tz = tzOf(prefs);
  const meta = state.meta;
  const m = state.market?.instruments[instrument];
  const tick = Math.floor((state.lastUpdate ?? 0) / 5000) + state.activityTick; // bars: every 5 s or on new activity
  const { data, error, loading } = useApi<BarsPayload>(meta ? `/api/bars?root=${instrument}&tf=${tf}&limit=600` : null, tick, 2000);
  const show = prefs.indicators;
  const markers = useMemo(() => (data?.markers ?? []).filter((x) => x.kind === "signal" || x.kind === "rejected" || x.kind === "authorized"), [data]);
  const disconnected = state.api === "error" || state.live === "closed";
  const n = data?.bars.time.length ?? 0;
  const toggle = (k: keyof typeof show) => setPrefs({ ...prefs, indicators: { ...show, [k]: !show[k] } });

  return (
    <div className="stack">
      <div className="page-head">
        <h1>Live Charts</h1>
        <span className="sub">{meta?.mode} · {meta?.data_label} data · indicators computed by the backend</span>
        <div className="tools">
          <Seg label="Instrument" value={instrument} onChange={setInstrument} options={(meta?.instruments ?? ["MES", "MNQ"]).map((r) => ({ value: r, label: r }))} />
          <Seg<Tf> label="Timeframe" value={tf} onChange={setTf} options={(meta?.timeframes ?? ["1min", "5min", "15min"]).map((t) => ({ value: t as Tf, label: t.replace("min", "m") }))} />
          <Seg label="Scale" value={autoScale ? "auto" : "manual"} onChange={(v) => setAutoScale(v === "auto")} options={[{ value: "auto", label: "Auto" }, { value: "manual", label: "Manual" }]} />
        </div>
      </div>
      <Panel title={<>{instrument} <span className="mono dim">{meta?.contracts?.[instrument]}</span>{m?.contract_expiry && <span className="dim"> · expires {m.contract_expiry}</span>}</>}
        right={<>
          <span className="mono">{price(m?.last)}</span>
          <Badge tone={healthTone(m?.health)}>{m ? label(m.health) : "no data"}</Badge>
          {(["vwap", "emaFast", "emaSlow", "volume", "signals", "fills", "levels"] as const).map((k) => (
            <label key={k} className="check"><input type="checkbox" checked={show[k]} onChange={() => toggle(k)} />
              {{ vwap: "VWAP", emaFast: data?.indicator_names.ema_fast ?? "EMA fast", emaSlow: data?.indicator_names.ema_slow ?? "EMA slow",
                volume: "Volume", signals: "Signals", fills: "Sim fills", levels: "Stop/Target" }[k]}</label>))}
        </>} flush>
        <div className="chart-wrap">
          <div className="legend"><span><i style={{ background: "#eab308" }} />VWAP</span><span><i style={{ background: "#38bdf8" }} />{data?.indicator_names.ema_fast}</span>
            <span><i style={{ background: "#a78bfa" }} />{data?.indicator_names.ema_slow}</span><span>{tf} · times {tz}</span></div>
          <CandleChart data={data} show={show} tz={tz} autoScale={autoScale} live={m?.last ?? null} />
          {(loading && !data) && <div className="chart-overlay">Loading bars…</div>}
          {error && <div className="chart-overlay"><div><strong>Chart unavailable</strong><div>{error}</div></div></div>}
          {!loading && !error && n === 0 && <div className="chart-overlay"><div><strong>NO DATA</strong><div>No completed bars for {instrument} {tf} yet.</div></div></div>}
          {disconnected && n > 0 && <div className="chart-overlay" style={{ background: "rgba(11,15,20,.45)" }}><strong className="neg">DISCONNECTED — chart frozen at last update</strong></div>}
          <span className="chart-attrib">Charts: TradingView Lightweight Charts</span>
        </div>
      </Panel>
      <Panel title="Strategy signals on this chart" right={<span className="note">a signal is NOT a trade · fills are SIMULATED</span>} flush>
        <SignalTable markers={markers} tz={tz} />
      </Panel>
    </div>
  );
}

interface SignalRow { key: string; time: string | null; direction: string; reference: number | null; stop: number | null;
  target: number | null; rr: number | null; outcome: "authorized" | "rejected" | "pending"; reason: string }

/** One row per strategy signal, joined with its authorization decision (by signal id). */
export function signalRows(markers: Marker[]): SignalRow[] {
  const decisions = new Map<string, Marker>();
  for (const m of markers) if ((m.kind === "authorized" || m.kind === "rejected") && m.signal_id) decisions.set(m.signal_id, m);
  const used = new Set<string>();
  const rows: SignalRow[] = [];
  for (const m of markers) {
    if (m.kind !== "signal") continue;
    const d = m.signal_id ? decisions.get(m.signal_id) : undefined;
    if (d?.signal_id) used.add(d.signal_id);
    rows.push({ key: m.signal_id ?? `${m.time}`, time: m.bar_time ?? m.time, direction: (m.direction ?? "").toUpperCase(),
      reference: m.reference ?? null, stop: m.stop ?? null, target: m.target ?? null, rr: m.reward_risk ?? null,
      outcome: d ? (d.kind === "authorized" ? "authorized" : "rejected") : "pending",
      reason: d?.reason || m.order_reason || m.order_status || "" });
  }
  for (const d of decisions.values()) {
    if (d.signal_id && used.has(d.signal_id)) continue;
    rows.push({ key: d.signal_id ?? `${d.time}`, time: d.time, direction: (d.direction ?? "").toUpperCase(), reference: null, stop: null,
      target: null, rr: null, outcome: d.kind === "authorized" ? "authorized" : "rejected", reason: d.reason ?? "" });
  }
  return rows.sort((a, b) => String(b.time).localeCompare(String(a.time)));
}

function SignalTable({ markers, tz }: { markers: Marker[]; tz: string }) {
  const rows = signalRows(markers).slice(0, 50);
  return (
    <Table rows={rows} rowKey={(r) => r.key}
      empty={<Empty title="No strategy signals in view">The strategy has not produced a setup on the visible bars.</Empty>}
      columns={[
        { key: "time", label: "Signal bar", render: (r) => <span className="mono">{time(r.time, true, tz)}</span> },
        { key: "direction", label: "Dir" },
        { key: "reference", label: "Entry ref", num: true, render: (r) => price(r.reference) },
        { key: "stop", label: "Stop ref", num: true, render: (r) => price(r.stop) },
        { key: "target", label: "Target ref", num: true, render: (r) => price(r.target) },
        { key: "rr", label: "R:R", num: true, render: (r) => ratio(r.rr) },
        { key: "outcome", label: "Authorization", render: (r) => <Badge tone={r.outcome === "authorized" ? "pos" : r.outcome === "rejected" ? "warn" : "off"}>
          {r.outcome === "authorized" ? "authorized paper order" : r.outcome === "rejected" ? "rejected" : "no decision"}</Badge> },
        { key: "reason", label: "Reason", render: (r) => <span className="mono">{r.reason}</span> },
      ]} />
  );
}
