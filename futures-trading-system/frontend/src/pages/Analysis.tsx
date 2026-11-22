import { Badge, Empty, ErrorBox, Kv, Loading, Panel, Stat, Table, useApi } from "../components/ui";
import { int, label, money, pct, ratio, time, type Tone } from "../lib/format";
import { tzOf } from "../state/prefs";
import { useInstrument } from "../state/instrument";
import { useTerminal } from "../state/store";

const SUIT_TONE: Record<string, Tone> = { FAVORABLE: "pos", NEUTRAL: "info", UNFAVORABLE: "warn", NO_TRADE: "neg" };

export function Analysis() {
  const { state, prefs } = useTerminal();
  const { instrument } = useInstrument();
  const tz = tzOf(prefs);
  const { data, error, loading } = useApi<any>(`/api/analysis?root=${instrument}`, state.activityTick, 3000);
  if (error) return <ErrorBox error={`Regime analysis unavailable: ${error}`} />;
  if (loading && !data) return <Loading what="Loading market analysis" />;
  const cur = data?.current;
  const reg = cur?.regime;
  const cls = reg?.classification;
  const ctx = reg?.context;
  return (
    <div className="stack">
      <div className="page-head"><h1>AI Market Analysis</h1><span className="sub">{instrument} · market-regime engine ({data?.classifier}) · enforcement {data?.enforcement}</span></div>
      <div className="note">{data?.note}</div>
      {!data?.available ? (
        <Panel title="Regime engine"><Empty title="Analysis unavailable">{data?.reason}</Empty></Panel>
      ) : (
        <>
          {(reg && !reg.valid) && <ErrorBox error={`Regime snapshot INVALID (fail-safe: NO_TRADE): ${(reg.reasons ?? []).join("; ") || reg.error || "unknown"}`} />}
          {data.errors > 0 && <ErrorBox error={`Regime engine errors in this session: ${data.errors} (each failed closed)`} />}
          <div className="grid g4">
            <Panel title="Current regime" flush>
              <div className="stats">
                <Stat label="Trend" value={label(cls?.trend) || null} />
                <Stat label="Range" value={label(cls?.range) || null} />
                <Stat label="Volatility" value={label(cls?.volatility) || null} sub={label(cls?.volatility_change)} />
                <Stat label="Structure" value={label(cls?.structure) || null} />
              </div>
            </Panel>
            <Panel title="Bias & strength" flush>
              <div className="stats">
                <Stat label="Direction" value={reg?.direction ?? null} />
                <Stat label="Trend strength (ATR)" value={ratio(cur?.trend_strength_atr)} />
                <Stat label="Trend score" value={ratio(cls?.trend_score)} title="raw evidence score in [0,1] — not a probability" />
                <Stat label="HTF context" value={label(ctx?.trend) || "unavailable"} />
              </div>
            </Panel>
            <Panel title="Confidence" flush>
              <div className="stats">
                <Stat label="Calibrated confidence" value={cur?.confidence === null || cur?.confidence === undefined ? null : pct(cur.confidence * 100)}
                  sub={cur?.confidence === null ? "uncalibrated: no confidence is claimed" : "calibrated probability"} />
                <Stat label="Level" value={cur?.confidence_level ?? null} />
                <Stat label="Data quality" value={reg?.valid ? "valid" : "invalid"} tone={reg?.valid ? "pos" : "neg"} />
                <Stat label="Regime duration" value={data.regime_duration_minutes === null ? null : `${int(data.regime_duration_minutes)} min`} />
              </div>
            </Panel>
            <Panel title="Suitability (VWAP pullback)">
              {(["long", "short"] as const).map((d) => {
                const s = cur?.suitability?.[d];
                return (
                  <div key={d} className="row" style={{ marginBottom: 6 }}>
                    <span className="upper muted" style={{ width: 44 }}>{d}</span>
                    <Badge tone={SUIT_TONE[s?.suitability] ?? "off"}>{s?.suitability ?? "unavailable"}</Badge>
                    <span className="note">{label(s?.environment)}</span>
                  </div>
                );
              })}
              <div className="note">As of bar {time(cur?.bar_time, true, tz)}</div>
            </Panel>
          </div>
          <div className="grid g3">
            <Panel title="Why: factors">
              <Factors cur={cur} />
            </Panel>
            <Panel title="Explanation">
              {cur?.explanation ? <p style={{ margin: 0 }}>{cur.explanation}</p>
                : <Empty title="No explanation yet">An explanation is produced when the strategy emits a signal.</Empty>}
            </Panel>
            <Panel title="Features (causal, from completed bars)">
              <Kv rows={Object.entries(reg?.features ?? {}).map(([k, v]) => [k.replace(/^rf_/, ""), typeof v === "number" ? ratio(v, 3) : String(v)])} />
            </Panel>
          </div>
          <div className="grid g2">
            <Panel title="Recent regime history" flush>
              <Table rows={data.history ?? []} rowKey={(r: any, i) => `${r.time}-${i}`}
                columns={[
                  { key: "time", label: "Time", render: (r: any) => <span className="mono">{time(r.time, true, tz)}</span> },
                  { key: "trend", label: "Trend", render: (r: any) => label(r.trend) },
                  { key: "range", label: "Range", render: (r: any) => label(r.range) },
                  { key: "volatility", label: "Vol", render: (r: any) => label(r.volatility) },
                  { key: "long", label: "Long", render: (r: any) => <Badge tone={SUIT_TONE[r.long] ?? "off"}>{r.long}</Badge> },
                  { key: "short", label: "Short", render: (r: any) => <Badge tone={SUIT_TONE[r.short] ?? "off"}>{r.short}</Badge> },
                ]} />
            </Panel>
            <Panel title="Transitions & performance by regime" flush>
              <Table rows={cur?.recent_transitions ?? []} rowKey={(r: any, i) => `${r.time}-${i}`}
                empty={<Empty title="No transitions yet" />}
                columns={[
                  { key: "time", label: "Time", render: (r: any) => <span className="mono">{time(r.time, true, tz)}</span> },
                  { key: "from", label: "From", render: (r: any) => (r.from ?? []).map(label).join(" / ") },
                  { key: "to", label: "To", render: (r: any) => (r.to ?? []).map(label).join(" / ") },
                ]} />
              <div className="panel-b">
                {Object.keys(data.performance_by_regime ?? {}).length === 0 ? <div className="note">No closed paper trades yet: no performance by regime.</div> :
                  <Kv rows={Object.entries(data.performance_by_regime).map(([k, v]: [string, any]) => [label(k), `${v.trades} trades · ${money(v.net_pnl, { sign: true })}${v.sample_warning ? " · small sample" : ""}`])} />}
              </div>
            </Panel>
          </div>
        </>
      )}
    </div>
  );
}

function Factors({ cur }: { cur: any }) {
  const reg = cur?.regime;
  const evidence: string[] = reg?.classification?.evidence ?? [];
  const negatives = new Set<string>();
  for (const d of ["long", "short"]) for (const r of cur?.suitability?.[d]?.reasons ?? []) negatives.add(r);
  const missing: string[] = [...(reg?.reasons ?? [])];
  if (cur && cur.confidence === null) missing.push("confidence not calibrated");
  if (!reg?.context) missing.push("higher-timeframe context unavailable");
  return (
    <div className="stack" style={{ gap: 8 }}>
      <div><div className="note upper">Evidence observed</div>{evidence.length ? evidence.map((e) => <div key={e} className="pos">+ {label(e)}</div>) : <div className="dim">none recorded</div>}</div>
      <div><div className="note upper">Against / warnings</div>{negatives.size ? [...negatives].map((e) => <div key={e} className="warn">− {label(e)}</div>) : <div className="dim">none</div>}</div>
      <div><div className="note upper">Missing information</div>{missing.length ? missing.map((e) => <div key={e} className="dim">? {label(e)}</div>) : <div className="dim">none</div>}</div>
    </div>
  );
}
