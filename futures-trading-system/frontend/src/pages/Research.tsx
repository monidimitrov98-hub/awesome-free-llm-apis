// Quant Research (Sprint 11): READ-ONLY view of the research engine -- strategy registry, immutable parameter
// versions, human promotion states, experiments (failures included), one-time OOS reveals, research runs,
// Monte Carlo summaries and compliance coverage. Every value comes from the backend (/api/research/*).
// There is no control to run research, reveal out-of-sample data, register, change a state or trade.
import { useState } from "react";
import { Badge, Empty, ErrorBox, Kv, Loading, Panel, Seg, Table, useApi } from "../components/ui";
import { int, isNum, money, pct, time, type Tone } from "../lib/format";
import { tzOf } from "../state/prefs";
import { useTerminal } from "../state/store";

export function evidenceTone(e?: string | null): Tone {
  // never green: a positive in-sample label is information for a human, not an approval
  if (e === "POSITIVE EXPECTANCY SUPPORTED IN THIS SAMPLE") return "info";
  if (e === "NEGATIVE EXPECTANCY IN THIS SAMPLE") return "neg";
  if (e === "INCONCLUSIVE" || e === "INSUFFICIENT EVIDENCE") return "warn";
  return "off";
}
export function stateTone(s?: string | null): Tone {
  if (s === "REJECTED" || s === "SUSPENDED") return "neg";
  if (s === "SHADOW_APPROVED" || s === "PAPER_APPROVED") return "info";
  if (s === "OOS_VALIDATED" || s === "BACKTESTED") return "warn";
  return "off";
}
const coverageTone = (v?: string | null): Tone => (v === "COMPLETE FOR SUPPORTED RULES" ? "info" : v === "NO PROP PROFILE" ? "off" : "warn");
const frac = (x: unknown) => (isNum(x) ? pct(x * 100) : "—");    // backend sends fractions (0..1)
const ci = (x?: number[] | null) => (x && x.length === 2 ? `[${money(x[0])}, ${money(x[1])}]` : "—");
type View = "runs" | "versions" | "experiments" | "oos";

export function Research() {
  const { prefs } = useTerminal();
  const tz = tzOf(prefs);
  const [view, setView] = useState<View>("runs");
  const [sel, setSel] = useState<string | null>(null);
  const ov = useApi<any>("/api/research/overview", 0, 15000);
  const d = ov.data;
  return (
    <div className="stack">
      <div className="page-head"><h1>Quant Research</h1>
        <Badge tone="warn">READ-ONLY · RESEARCH SIMULATION</Badge>
        <span className="sub">No result here is evidence of future profitability. Promotion is a human CLI action; there is no live state.</span>
      </div>
      {ov.error && <ErrorBox error={`Research view unavailable: ${ov.error}`} />}
      {ov.loading && !d && <Loading what="Loading research" />}
      {d && <>
        <Panel title="Limitations">
          <ul className="notes">{(d.limitations ?? []).map((t: string) => <li key={t}>{t}</li>)}</ul>
        </Panel>
        <Panel title="Strategy registry" flush>
          <Table rows={d.strategies ?? []} rowKey={(r: any) => r.id} columns={[
            { key: "id", label: "Id", render: (r: any) => <span className="mono">{r.id}</span> },
            { key: "title", label: "Strategy" },
            { key: "research_status", label: "Status" },
            { key: "description", label: "Description" }]} />
        </Panel>
        <Seg<View> value={view} onChange={setView} label="Research view" options={[
          { value: "runs", label: "Runs" }, { value: "versions", label: "Versions & promotion" },
          { value: "experiments", label: "Experiments" }, { value: "oos", label: "Out-of-sample ledger" }]} />
        {view === "runs" && <RunsView runs={d.runs ?? []} sel={sel} setSel={setSel} tz={tz} />}
        {view === "versions" && <VersionsView rows={d.versions ?? []} error={d.versions_error} />}
        {view === "experiments" && <ExperimentsView e={d.experiments} tz={tz} />}
        {view === "oos" && <OosView o={d.oos} tz={tz} />}
      </>}
    </div>
  );
}

function RunsView({ runs, sel, setSel, tz }: { runs: any[]; sel: string | null; setSel(s: string | null): void; tz: string }) {
  if (!runs.length) return <Panel title="Research runs"><Empty title="No research runs yet">Runs are created with run_research.py (simulation only).</Empty></Panel>;
  return (
    <div className="stack">
      <Panel title="Research runs (newest first; not ranked)" flush>
        <Table rows={runs} rowKey={(r: any) => r.attempt} onRow={(r: any) => setSel(r.attempt)} columns={[
          { key: "strategy", label: "Strategy", render: (r: any) => <span className="mono">{r.strategy}@{r.version}{r.overrides ? "*" : ""}</span> },
          { key: "purpose", label: "Purpose" },
          { key: "instrument", label: "Instrument", render: (r: any) => <>{r.instrument}{r.synthetic && <Badge tone="warn">SYNTHETIC</Badge>}</> },
          { key: "period", label: "Period", render: (r: any) => `${r.period?.start ?? "?"} … ${r.period?.end ?? "?"}` },
          { key: "trades", label: "Trades", num: true, render: (r: any) => int(r.trades) },
          { key: "mean_daily", label: "Mean/day", num: true, render: (r: any) => money(r.mean_daily) },
          { key: "ci", label: "95% CI (days)", render: (r: any) => ci(r.mean_daily_ci95) },
          { key: "p_holm", label: "p (Holm)", num: true, render: (r: any) => (isNum(r.p_holm) ? r.p_holm.toFixed(3) : "—") },
          { key: "evidence", label: "Evidence", render: (r: any) => <Badge tone={evidenceTone(r.evidence)}>{r.evidence}</Badge> },
          { key: "compliance", label: "Prop coverage", render: (r: any) => <Badge tone={coverageTone(r.compliance)}>{r.compliance}</Badge> },
          { key: "mc", label: "MC", render: (r: any) => (r.monte_carlo ? "yes" : "—") }]} />
      </Panel>
      {sel && <RunDetail attempt={sel} tz={tz} />}
    </div>
  );
}

function RunDetail({ attempt }: { attempt: string; tz: string }) {
  const { data, error, loading } = useApi<any>(`/api/research/run?attempt=${attempt}`, attempt, 1000);
  if (error) return <ErrorBox error={`Run unavailable: ${error}`} />;
  if (loading || !data) return <Loading what="Loading run" />;
  const m = data.manifest, ev = data.evaluation, cov = data.compliance_coverage;
  return (
    <div className="stack">
      <Panel title={`Run ${attempt}`} right={<Badge tone={data.integrity.ok ? "off" : "neg"}>{data.integrity.ok ? "MANIFEST VERIFIED" : "INTEGRITY FAILURE"}</Badge>}>
        {!data.integrity.ok && <ErrorBox error={`Files changed since the run: ${data.integrity.problems.join("; ")}`} />}
        <Kv rows={[["Strategy", <span className="mono">{m.strategy.id}@{m.strategy.version}</span>],
          ["Version SHA-256", <span className="mono">{m.strategy.sha256}</span>],
          ["Registered", m.strategy.registered ? "yes" : "NO (exploration only)"],
          ["Overrides", m.strategy.overrides ? JSON.stringify(m.strategy.overrides) : "none"],
          ["Scope", ev?.scope_text ?? m.purpose],
          ["Data", <span className="mono">{String(m.data?.sha256 ?? "").slice(0, 16)}</span>],
          ["Code", `${String(m.code?.commit ?? "").slice(0, 12)} ${m.code?.trusted ? "(clean)" : "(UNTRUSTED: " + (m.code?.untrusted_reasons ?? []).join(", ") + ")"}`],
          ["OOS", m.oos ? `ledger seq ${m.oos.ledger_seq}${m.oos.replay ? " (replay)" : ""}` : "not an OOS reveal"]]} />
        <p className="note">{m.disclaimer}</p>
      </Panel>
      {ev && <Panel title="Evaluation" right={<Badge tone={evidenceTone(ev.evidence)}>{ev.evidence}</Badge>}>
        <Kv rows={[["Trades / days", `${int(ev.sample.trades)} / ${int(ev.sample.trading_days_with_trades)} (${ev.sample.level})`],
          ["Net (after costs)", money(ev.net_profit)], ["Expectancy / trade", money(ev.ev_per_trade)],
          ["Expectancy CI (conservative)", ci(ev.ev_per_trade_ci95_conservative)],
          ["Mean daily P&L, 95% day bootstrap", `${money(ev.mean_daily_pnl)} ${ci(ev.mean_daily_pnl_ci95_day_bootstrap)}`],
          ["Win rate (Wilson)", `${frac(ev.win_rate)} ${ev.win_rate_ci95_wilson ? `[${frac(ev.win_rate_ci95_wilson[0])}, ${frac(ev.win_rate_ci95_wilson[1])}]` : ""}`],
          ["Average win / loss", `${money(ev.average_win)} / ${money(ev.average_loss)}`],
          ["Profit factor", isNum(ev.profit_factor) ? ev.profit_factor.toFixed(2) : "—"],
          ["Max drawdown (equity)", money(ev.max_drawdown_equity)],
          ["Sharpe / Sortino", ev.ratios.sharpe == null ? ev.ratios.note : `${ev.ratios.sharpe?.toFixed(2)} / ${ev.ratios.sortino?.toFixed(2) ?? "—"}`],
          ["p raw / Holm / BH", `${ev.tests.p_raw?.toFixed?.(3) ?? "—"} / ${ev.tests.p_holm?.toFixed?.(3) ?? "—"} / ${ev.tests.p_bh?.toFixed?.(3) ?? "—"} (family ${ev.tests.family_size})`],
          ["Days needed (80% power)", ev.power.days_needed_80pct_at_observed_effect ?? "—"],
          ["Costs", `${money(ev.costs.total_costs)} (commission ${money(ev.costs.commission)}, slippage ${money(ev.costs.slippage)})`]]} />
        <ul className="notes">{(ev.limitations ?? []).map((t: string) => <li key={t}>{t}</li>)}</ul>
      </Panel>}
      <Panel title="Prop firm compliance coverage" right={<Badge tone={coverageTone(cov?.verdict ?? "NO PROP PROFILE")}>{cov?.verdict ?? "NO PROP PROFILE"}</Badge>}>
        {!cov ? <p className="note">No prop firm profile in this run: firm rules were NOT simulated.</p> : <>
          <Table rows={cov.rules} rowKey={(r: any) => r.rule} columns={[
            { key: "rule", label: "Rule", render: (r: any) => <span className="mono">{r.rule}</span> },
            { key: "status", label: "In this run" }, { key: "verification", label: "Verification" },
            { key: "detail", label: "Detail" }]} />
          <ul className="notes">{(cov.issues ?? []).map((t: string) => <li key={t}>{t}</li>)}</ul>
          <p className="note">{cov.note}</p></>}
      </Panel>
      {m.cost_sensitivity && <Panel title="Cost sensitivity (full re-runs)" flush>
        <Table rows={m.cost_sensitivity} rowKey={(r: any) => r.scenario} columns={[
          { key: "scenario", label: "Scenario" }, { key: "trades", label: "Trades", num: true, render: (r: any) => int(r.trades) },
          { key: "net_profit", label: "Net", num: true, render: (r: any) => money(r.net_profit) },
          { key: "mean_daily_pnl", label: "Mean/day", num: true, render: (r: any) => money(r.mean_daily_pnl) },
          { key: "ci", label: "95% CI", render: (r: any) => ci(r.mean_daily_ci95) }]} />
      </Panel>}
      <Panel title="Monte Carlo (resampled historical trades; not a forecast)" flush>
        {data.monte_carlo?.length ? <Table rows={data.monte_carlo} rowKey={(r: any, i: number) => `${r.file}:${r.method}:${i}`} columns={[
          { key: "method", label: "Method" }, { key: "paths", label: "Paths", num: true, render: (r: any) => int(r.paths) },
          { key: "p", label: "P(final < 0)", num: true, render: (r: any) => frac(r.prob_final_negative) },
          { key: "ps", label: "P(final < 0) stress", num: true, render: (r: any) => frac(r.prob_final_negative_stress) },
          { key: "dd", label: "Max DD p50 / p95", render: (r: any) => `${money(r.max_dd_p50)} / ${money(r.max_dd_p95)}` },
          { key: "ruin", label: "Risk of ruin", num: true, render: (r: any) => frac(r.risk_of_ruin) },
          { key: "prop", label: "Prop rules", render: (r: any) => r.prop_label ?? "—" }]} />
          : <Empty title="No Monte Carlo report">run_research.py monte-carlo --run …</Empty>}
      </Panel>
    </div>
  );
}

function VersionsView({ rows, error }: { rows: any[]; error?: string }) {
  return (
    <Panel title="Parameter versions and promotion state" flush>
      {error && <ErrorBox error={`Versions unavailable: ${error}`} />}
      <Table rows={rows} rowKey={(r: any) => `${r.strategy}@${r.version}`} columns={[
        { key: "strategy", label: "Strategy", render: (r: any) => <span className="mono">{r.strategy}</span> },
        { key: "version", label: "Version", render: (r: any) => <span className="mono">{r.version}</span> },
        { key: "state", label: "File", render: (r: any) => <Badge tone={r.state === "REGISTERED" ? "off" : "neg"}>{r.state}</Badge> },
        { key: "sha256", label: "SHA-256", render: (r: any) => <span className="mono">{String(r.sha256).slice(0, 16)}</span> },
        { key: "promotion_state", label: "Promotion", render: (r: any) => <Badge tone={stateTone(r.promotion_state)}>{r.promotion_state}</Badge> },
        { key: "integrity_ok", label: "Store", render: (r: any) => (r.integrity_ok ? "ok" : <Badge tone="neg">INTEGRITY FAILURE</Badge>) },
        { key: "transitions", label: "Transitions", num: true }]} />
      <p className="note">States: RESEARCH → BACKTESTED → OOS_VALIDATED → SHADOW_APPROVED → PAPER_APPROVED (plus REJECTED / SUSPENDED). Each step is a human action with evidence; there is no live state.</p>
    </Panel>
  );
}

function ExperimentsView({ e, tz }: { e: any; tz: string }) {
  if (!e) return null;
  return (
    <Panel title={`Experiment registry (${e.total ?? "?"} attempts; ${e.failed ?? 0} failed or refused; ${e.unfinished ?? 0} unfinished)`} flush>
      {!e.integrity_ok && <ErrorBox error={`Experiment registry failed verification: ${e.error}`} />}
      <Table rows={e.recent ?? []} rowKey={(r: any) => r.attempt} columns={[
        { key: "at", label: "Time", render: (r: any) => time(r.at, true, tz) },
        { key: "strategy", label: "Configuration", render: (r: any) => <span className="mono">{r.strategy}@{r.version}{r.registered ? "" : " (unregistered)"}</span> },
        { key: "instrument", label: "Instrument" }, { key: "purpose", label: "Purpose" },
        { key: "period", label: "Period", render: (r: any) => `${r.period?.start} … ${r.period?.end}` },
        { key: "status", label: "Status", render: (r: any) => <Badge tone={r.status === "ok" ? "off" : "warn"}>{r.status}</Badge> },
        { key: "evidence", label: "Evidence", render: (r: any) => (r.evidence ? <Badge tone={evidenceTone(r.evidence)}>{r.evidence}</Badge> : r.error || "—") }]} />
      <p className="note">Every attempt counts in the multiple-testing family, including failures.</p>
    </Panel>
  );
}

function OosView({ o, tz }: { o: any; tz: string }) {
  if (!o) return null;
  return (
    <Panel title="One-time out-of-sample reveals" flush>
      {!o.ledger_ok && <ErrorBox error={`Validation ledger unavailable: ${o.error}`} />}
      {o.reveals?.length ? <Table rows={o.reveals} rowKey={(r: any) => String(r.seq)} columns={[
        { key: "seq", label: "Seq", num: true }, { key: "time", label: "Revealed", render: (r: any) => time(r.time, true, tz) },
        { key: "instrument", label: "Instrument" }, { key: "period", label: "Dates", render: (r: any) => `${r.start} … ${r.end}` },
        { key: "strategy", label: "Configuration", render: (r: any) => (r.strategy ? <span className="mono">{r.strategy}@{r.version}</span> : "run_backtest.py") },
        { key: "evidence", label: "Evidence", render: (r: any) => (r.evidence ? <Badge tone={evidenceTone(r.evidence)}>{r.evidence}</Badge> : "—") }]} />
        : <Empty title="No out-of-sample reveal recorded">Revealed dates can never be used for research again.</Empty>}
    </Panel>
  );
}
