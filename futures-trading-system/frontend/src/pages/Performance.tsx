import { useMemo, useState } from "react";
import { SeriesChart } from "../components/Charts";
import { Badge, Empty, ErrorBox, Loading, Panel, Seg, Stat, useApi } from "../components/ui";
import { int, label, money, pct, pnlClass, ratio } from "../lib/format";
import { tzOf } from "../state/prefs";
import { useTerminal } from "../state/store";

export function Performance() {
  const { state, prefs } = useTerminal();
  const tz = tzOf(prefs);
  const [source, setSource] = useState<"session" | "backtest">("session");
  const { data, error, loading } = useApi<any>(`/api/performance?source=${source}`, source === "session" ? state.activityTick : 0, 3000);
  const curves = data?.curves;
  const equity = useMemo(() => (curves?.equity ?? []).map((p: any) => ({ time: p.time, value: p.equity })), [curves]);
  const dd = useMemo(() => (curves?.drawdown ?? []).map((p: any) => ({ time: p.time, value: p.drawdown })), [curves]);
  const daily = useMemo(() => (curves?.daily ?? []).map((p: any) => ({ time: Date.parse(`${p.date}T00:00:00Z`) / 1000, value: p.net })), [curves]);
  const o = data?.overall ?? {};
  const n = data?.trades ?? 0;
  return (
    <div className="stack">
      <div className="page-head"><h1>Performance</h1>
        <span className="sub">Sources are never combined: each view shows one run kind only.</span>
        <div className="tools"><Seg<"session" | "backtest"> value={source} onChange={setSource} label="Source"
          options={[{ value: "session", label: `This session (${state.meta?.mode ?? "—"})` }, { value: "backtest", label: "Historical backtest" }]} /></div></div>
      {error && <ErrorBox error={`Performance unavailable: ${error}`} />}
      {loading && !data ? <Loading what="Loading performance" /> : data && data.available === false ? (
        <Panel title="Historical backtest"><Empty title="Not available">{data.reason}</Empty></Panel>
      ) : data && (
        <>
          <div className="row">
            <Badge tone={source === "backtest" ? "info" : data.source === "paper" ? "pos" : "warn"}>{data.label}</Badge>
            <Badge tone={n >= 30 ? "pos" : "warn"}>{data.sample_status}</Badge>
            <span className="note">Simulated results. Not evidence of future profitability.</span>
          </div>
          <Panel title="Summary" flush>
            <div className="stats">
              <Stat label="Net P&L" value={money(o.net_pnl, { sign: true })} tone={pnlClass(o.net_pnl)} />
              <Stat label="Gross P&L" value={money(o.gross_pnl, { sign: true })} tone={pnlClass(o.gross_pnl)} />
              <Stat label="Total costs" value={o.trades ? money((o.commissions ?? 0) + (o.slippage_cost ?? 0)) : null} />
              <Stat label="Trades" value={int(n)} sub={`${int(data.wins)} W / ${int(data.losses)} L`} />
              <Stat label="Win rate" value={o.trades ? pct(o.win_rate * 100) : null} />
              <Stat label="Profit factor" value={n >= 5 ? ratio(o.profit_factor) : null} sub={n < 5 ? "needs ≥ 5 trades" : undefined} />
              <Stat label="Expectancy" value={money(o.net_expectancy, { sign: true })} />
              <Stat label="Avg winner" value={money(o.average_win)} />
              <Stat label="Avg loser" value={money(o.average_loss)} />
              <Stat label="Avg R" value={ratio(o.average_r_multiple)} />
              <Stat label="Max DD (trades)" value={money(o.max_drawdown_of_trade_sequence)} />
              <Stat label="Max DD (account)" value={money(data.max_drawdown_account ?? data.backtest_metrics?.max_drawdown)} />
            </div>
          </Panel>
          {n === 0 ? <Panel title="Charts"><Empty title="No closed trades">{state.meta?.mode === "MOCK" ? "MOCK mode never authorizes entries, so it has no trades." : "Charts appear after the first closed trade."}</Empty></Panel> : (
            <>
              <div className="grid g2">
                <Panel title="Equity curve" flush><div className="chart-wrap sm"><SeriesChart points={equity} kind="area" tone="info" tz={tz} /></div></Panel>
                <Panel title="Drawdown" flush><div className="chart-wrap sm"><SeriesChart points={dd} kind="area" tone="neg" tz={tz} /></div></Panel>
              </div>
              <div className="grid g2">
                <Panel title="Daily P&L" flush><div className="chart-wrap sm"><SeriesChart points={daily} kind="histogram" tone="info" dateOnly /></div></Panel>
                <Panel title="Long vs short"><Groups groups={{ long: data.long, short: data.short }} /></Panel>
              </div>
              <div className="grid g3">
                <Panel title="By instrument"><Groups groups={data.by_instrument} /></Panel>
                <Panel title="By market regime"><Groups groups={data.by_regime} /></Panel>
                <Panel title="By session segment / hour"><Groups groups={{ ...data.by_segment, ...data.by_hour }} /></Panel>
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}

function Groups({ groups }: { groups: Record<string, any> }) {
  const entries = Object.entries(groups ?? {}).filter(([, v]) => v && v.trades > 0);
  if (!entries.length) return <Empty title="No trades" />;
  const max = Math.max(...entries.map(([, v]) => Math.abs(v.net_pnl ?? 0)), 1);
  return (
    <div className="cbars">
      {entries.map(([k, v]) => (
        <div key={k} className="cbar" title={`${v.trades} trades, win rate ${pct((v.win_rate ?? 0) * 100)}`}>
          <span>{label(k)} <span className="dim">({v.trades}{v.sample_warning ? ", small" : ""})</span></span>
          <div className="hbar"><i style={{ width: `${(Math.abs(v.net_pnl ?? 0) / max) * 100}%`, background: `var(--${(v.net_pnl ?? 0) >= 0 ? "pos" : "neg"})` }} /></div>
          <span className={`num ${pnlClass(v.net_pnl)}`}>{money(v.net_pnl, { sign: true })}</span>
        </div>
      ))}
    </div>
  );
}
