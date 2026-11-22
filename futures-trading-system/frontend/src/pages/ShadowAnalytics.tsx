// Shadow Analytics (Sprint 8): READ-ONLY research views of recorded paper sessions. Every figure comes from
// the backend (/api/shadow/*); this page only formats. Session types (MOCK / REPLAY / SHADOW) are never
// combined into one headline figure: the backend refuses mixed requests and the page shows why.
import { useMemo, useState, type ReactNode } from "react";
import { downloadCsv, qs } from "../api/client";
import { Badge, Empty, ErrorBox, Kv, Loading, Panel, Seg, Stat, Table, useApi } from "../components/ui";
import { int, isNum, label, money, pct, pnlClass, ratio, time, type Tone } from "../lib/format";
import { tzOf } from "../state/prefs";
import { useTerminal } from "../state/store";

export const SECTIONS = [
  { id: "sessions", label: "Sessions" }, { id: "compare", label: "Live vs Replay" }, { id: "execution", label: "Execution" },
  { id: "performance", label: "Performance" }, { id: "instruments", label: "By Instrument" }, { id: "regimes", label: "By Regime" },
  { id: "blocked", label: "Blocked Signals" }, { id: "reliability", label: "Data Reliability" }, { id: "account", label: "Account Survival" },
  { id: "validation", label: "Validation" },
] as const;
type SectionId = (typeof SECTIONS)[number]["id"];
const EXPORT_OF: Partial<Record<SectionId, string>> = { sessions: "sessions", execution: "execution", performance: "trades",
  instruments: "trades", regimes: "regimes", blocked: "blocked", reliability: "reliability", account: "account" };

interface F { root: string; session_type: string; start: string; end: string; strategy_version: string; regime: string;
  provider: string; session_id: string }
const EMPTY: F = { root: "", session_type: "", start: "", end: "", strategy_version: "", regime: "", provider: "", session_id: "" };

export function typeTone(t?: string | null): Tone {
  return t === "SHADOW" ? "pos" : t === "REPLAY" ? "info" : t === "MOCK" ? "warn" : "off";
}
const ci = (v: any, fmt: (x: unknown) => string) => (Array.isArray(v) && v.length === 2 ? `${fmt(v[0])} … ${fmt(v[1])}` : null);
const sampleTone = (s?: string): Tone => (s?.startsWith("ADEQUATE") ? "info" : "warn");
const sampleShort = (s?: string) => (s?.startsWith("INSUFFICIENT") ? "n < 30" : s?.startsWith("LIMITED") ? "n < 100" : s?.startsWith("ADEQUATE") ? "n ≥ 100" : "none");

export function ShadowAnalytics() {
  const { state, prefs } = useTerminal();
  const tz = tzOf(prefs);
  const [f, setF] = useState<F>(EMPTY);
  const [section, setSection] = useState<SectionId>("sessions");
  const [exportError, setExportError] = useState<string | null>(null);
  const set = (k: keyof F, v: string) => setF({ ...f, [k]: v });
  const q = qs({ ...f });
  // the session list drives the filter options; refreshed slowly (analysis is not a real-time view)
  const sessions = useApi<any>(`/api/shadow/sessions${qs({ ...f, session_id: "" })}`, state.activityTick, 15000);
  const validation = useApi<any>("/api/shadow/validation", state.activityTick, 30000);
  const rows: any[] = sessions.data?.report?.sessions ?? [];
  const opts = useMemo(() => ({
    strategy: [...new Set(rows.map((r) => r.strategy_version).filter(Boolean))] as string[],
    provider: [...new Set(rows.map((r) => r.provider).filter(Boolean))] as string[],
    session: rows.map((r) => r.session_id) as string[],
  }), [rows]);
  const sel = (k: keyof F, options: string[], all: string) => (
    <select value={f[k]} onChange={(e) => set(k, e.target.value)} aria-label={all}>
      <option value="">{all}</option>{options.map((o) => <option key={o} value={o}>{label(o)}</option>)}
    </select>);
  const types: string[] = sessions.data?.report?.session_types ?? [];
  const exp = EXPORT_OF[section];
  const download = (fmt: "csv" | "json") => exp && downloadCsv(`/api/shadow/export/${exp}.${fmt}${q}`, `shadow_${exp}.${fmt}`)
    .catch((e: Error) => setExportError(e.message));

  return (
    <div className="stack">
      <div className="page-head"><h1>Shadow Analytics</h1>
        <Badge tone="warn">READ-ONLY · RESEARCH</Badge>
        {validation.data && <Badge tone={validation.data.report.overall === "PASS" ? "pos" : validation.data.report.overall === "FAIL" ? "neg" : "warn"}
          title="Evidence validation status (see the Validation section)">Validation {validation.data.report.overall}</Badge>}
        <span className="sub">Recorded sessions analysed from their journals. Simulated fills; nothing here changes any setting.</span>
        {exp && <div className="tools">
          <button className="btn" onClick={() => download("csv")}>Export CSV</button>
          <button className="btn" onClick={() => download("json")}>Export JSON</button></div>}
      </div>
      {exportError && <ErrorBox error={`Export failed: ${exportError}`} />}
      <Panel title="Filters">
        <div className="row">
          <Seg<string> value={f.session_type} onChange={(v) => set("session_type", v)} label="Session type"
            options={[{ value: "", label: "All types" }, ...["SHADOW", "REPLAY", "MOCK", "MONITOR"].map((t) => ({ value: t, label: t }))]} />
          {sel("root", ["MES", "MNQ"], "MES & MNQ")}
          <label className="check">From <input type="date" value={f.start} onChange={(e) => set("start", e.target.value)} /></label>
          <label className="check">To <input type="date" value={f.end} onChange={(e) => set("end", e.target.value)} /></label>
          {sel("strategy_version", opts.strategy, "All strategy versions")}
          {sel("regime", ["strong_bullish", "moderate_bullish", "strong_bearish", "moderate_bearish", "weak_or_deteriorating", "no_trend", "unavailable"], "All regimes")}
          {sel("provider", opts.provider, "All providers")}
          {sel("session_id", opts.session, "All sessions")}
          <button className="btn" onClick={() => setF(EMPTY)}>Reset</button>
        </div>
        <div className="row note">
          Types in view: {types.length ? types.map((t) => <Badge key={t} tone={typeTone(t)}>{t}</Badge>) : "none"}
          {types.length > 1 && !f.session_type && <span> — choose one session type for performance figures (types are never combined).</span>}
        </div>
      </Panel>
      <Seg<SectionId> value={section} onChange={setSection} label="Section" options={SECTIONS.map((s) => ({ value: s.id, label: s.label }))} />
      {sessions.error && <ErrorBox error={`Shadow analytics unavailable: ${sessions.error}`} />}
      <Section id={section} q={q} f={f} tz={tz} tick={state.activityTick} sessions={sessions} />
    </div>
  );
}

function useReport(path: string | null, tick: unknown) {
  const r = useApi<any>(path, tick, 10000);
  return { ...r, rep: r.data?.report };
}

function Refused({ rep, children }: { rep: any; children: ReactNode }) {
  if (rep?.refused) return <Panel title="Not combined"><Empty title="Choose one session type">{rep.refused}</Empty></Panel>;
  return <>{children}</>;
}

function Wrap({ r, what, children }: { r: { error: string | null; loading: boolean; rep: any }; what: string; children: (rep: any) => ReactNode }) {
  if (r.error) return <ErrorBox error={`${what} unavailable: ${r.error}`} />;
  if (r.loading && !r.rep) return <Loading what={`Loading ${what.toLowerCase()}`} />;
  if (!r.rep) return <Empty title="No data" />;
  return <Refused rep={r.rep}>{children(r.rep)}</Refused>;
}

function Section({ id, q, f, tz, tick, sessions }: { id: SectionId; q: string; f: F; tz: string; tick: unknown; sessions: any }) {
  const path = (rep: string) => `/api/shadow/${rep}${q}`;
  const want = (x: SectionId[]) => x.includes(id);
  const perf = useReport(want(["performance", "instruments"]) ? path("performance") : null, tick);
  const costs = useReport(want(["performance"]) ? path("costs") : null, tick);
  const exec = useReport(want(["execution"]) ? path("execution") : null, tick);
  const reg = useReport(want(["regimes"]) ? path("regimes") : null, tick);
  const blk = useReport(want(["blocked"]) ? path("blocked") : null, tick);
  const rel = useReport(want(["reliability"]) ? path("reliability") : null, tick);
  const acct = useReport(want(["account"]) ? path("account") : null, tick);
  const val = useReport(want(["validation"]) ? "/api/shadow/validation" : null, tick);
  const [cmpRoot, setCmpRoot] = useState<"MES" | "MNQ">("MES");
  const cmp = useReport(want(["compare"]) ? `/api/shadow/compare${qs({ root: cmpRoot, start: f.start, end: f.end })}` : null, tick);

  switch (id) {
    case "sessions": return <SessionsView s={sessions} tz={tz} />;
    case "compare": return (
      <div className="stack">
        <div className="row"><Seg<"MES" | "MNQ"> value={cmpRoot} onChange={setCmpRoot} label="Instrument"
          options={[{ value: "MES", label: "MES" }, { value: "MNQ", label: "MNQ" }]} />
          <span className="note">REPLAY vs SHADOW, one instrument at a time, only for identical strategy / regime / contract / session definitions.</span></div>
        <Wrap r={cmp} what="Comparison">{(rep) => <CompareView rep={rep} />}</Wrap>
      </div>);
    case "execution": return <Wrap r={exec} what="Execution quality">{(rep) => <ExecutionView rep={rep} />}</Wrap>;
    case "performance": return <Wrap r={perf} what="Performance">{(rep) => <PerformanceView rep={rep} costs={costs.rep} />}</Wrap>;
    case "instruments": return <Wrap r={perf} what="Performance by instrument">{(rep) => <Groups title="By instrument (MES and MNQ are separate)" groups={rep.by_instrument} />}</Wrap>;
    case "regimes": return <Wrap r={reg} what="Regime analytics">{(rep) => <RegimeView rep={rep} />}</Wrap>;
    case "blocked": return <Wrap r={blk} what="Blocked signals">{(rep) => <BlockedView rep={rep} />}</Wrap>;
    case "reliability": return <Wrap r={rel} what="Data reliability">{(rep) => <ReliabilityView rep={rep} />}</Wrap>;
    case "account": return <Wrap r={acct} what="Account survival">{(rep) => <AccountView rep={rep} />}</Wrap>;
    case "validation": return <Wrap r={val} what="Validation status">{(rep) => <ValidationView rep={rep} />}</Wrap>;
  }
}

function SessionsView({ s, tz }: { s: any; tz: string }) {
  if (s.loading && !s.data) return <Loading what="Loading sessions" />;
  const rep = s.data?.report;
  if (!rep) return null;
  const bad = (rep.journals ?? []).filter((j: any) => j.error);
  return (
    <div className="stack">
      {bad.length > 0 && <ErrorBox error={`Excluded journals (not analysed): ${bad.map((j: any) => `${j.run}: ${j.error}`).join("; ")}`} />}
      <Panel title={`Sessions (${int(rep.count)})`} flush>
        <Table rows={rep.sessions} rowKey={(r: any) => r.session_id}
          empty={<Empty title="No recorded sessions">Run a session (shadow, or mock / replay with --keep-state) to analyse it here.</Empty>}
          columns={[
            { key: "trading_date", label: "Date" },
            { key: "instrument", label: "Instr." },
            { key: "contract", label: "Contract", render: (r: any) => r.contract ?? <span className="dim">not reported</span> },
            { key: "session_type", label: "Type", render: (r: any) => <Badge tone={typeTone(r.session_type)}>{r.session_type}</Badge> },
            { key: "data_label", label: "Data" },
            { key: "provider", label: "Provider" },
            { key: "strategy_version", label: "Strategy", render: (r: any) => <span className="mono">{r.strategy_version ?? "Unavailable"}</span> },
            { key: "regime_identity", label: "Regime model" },
            { key: "start", label: "First event", render: (r: any) => time(r.start, true, tz) },
            { key: "end", label: "Last event", render: (r: any) => time(r.end, true, tz) },
            { key: "signals", label: "Signals", num: true },
            { key: "authorized_entries", label: "Authorized", num: true },
            { key: "rejected_signals", label: "Blocked", num: true },
            { key: "simulated_fills", label: "Sim. fills", num: true },
            { key: "completed_trades", label: "Trades", num: true },
            { key: "cancelled_orders", label: "Cxl", num: true },
            { key: "net_pnl", label: "Net P&L (sim.)", num: true, render: (r: any) => <span className={pnlClass(r.net_pnl)}>{money(r.net_pnl, { sign: true })}</span> },
            { key: "session_drawdown", label: "DD", num: true, render: (r: any) => money(r.session_drawdown) },
            { key: "market_data_quality", label: "Data quality" },
            { key: "news_readiness", label: "NewsGuard" },
            { key: "account_guard_status", label: "AccountGuard" },
            { key: "status", label: "Status" },
          ]} />
      </Panel>
    </div>
  );
}

function PerformanceView({ rep, costs }: { rep: any; costs: any }) {
  const o = rep.overall ?? {};
  return (
    <div className="stack">
      <div className="row">
        <Badge tone={typeTone(rep.session_type)}>{rep.session_type ?? "—"}</Badge>
        <Badge tone="warn">{rep.label}</Badge>
        <Badge tone={sampleTone(o.sample)}>{o.sample}</Badge>
        <span className="note">Not evidence of future profitability. {rep.exploratory_note}</span>
      </div>
      <Panel title="Summary (95% intervals)" flush>
        <div className="stats">
          <Stat label="Trades" value={int(o.trades)} sub={`${int(o.trading_days)} trading days`} />
          <Stat label="Net P&L" value={money(o.net_pnl, { sign: true })} tone={pnlClass(o.net_pnl)} />
          <Stat label="Win rate" value={isNum(o.win_rate) ? pct(o.win_rate * 100) : null} sub={ci(o.win_rate_ci95, (x) => pct((x as number) * 100)) ?? undefined} title="Wilson interval" />
          <Stat label="Expectancy" value={money(o.expectancy, { sign: true })} sub={ci(o.expectancy_ci95, (x) => money(x)) ?? "interval needs ≥ 2 trades"} title="Wider of Student-t and trading-day cluster bootstrap" />
          <Stat label="Profit factor" value={o.profit_factor === null ? null : ratio(o.profit_factor)} sub={ci(o.profit_factor_ci95, (x) => ratio(x)) ?? (o.trades && o.profit_factor === null ? "undefined without losses" : undefined)} />
          <Stat label="Avg R" value={ratio(o.average_r)} />
          <Stat label="Avg winner" value={money(o.average_winner)} />
          <Stat label="Avg loser" value={money(o.average_loser)} />
          <Stat label="Max drawdown" value={money(o.max_drawdown)} sub="closed-trade sequence" />
          <Stat label="Loss streak" value={int(o.max_consecutive_losses)} sub="max consecutive" />
          <Stat label="Avg MFE (R)" value={ratio(o.average_mfe_r)} sub={`MAE ${ratio(o.average_mae_r)} R · ex-post`} title="From 1-minute bars after the fact" />
          <Stat label="Costs" value={money((o.commissions ?? 0) + (o.slippage_cost ?? 0))} sub="commission + slippage" />
        </div>
      </Panel>
      <div className="grid g3">
        <Groups title="Long vs short" groups={{ long: rep.long, short: rep.short }} />
        <Groups title="By session segment" groups={rep.by_segment} />
        <Groups title="By weekday" groups={rep.by_weekday} />
      </div>
      {costs && !costs.refused && (
        <Panel title="Cost & execution scenarios" right={<Badge tone="warn">{costs.label}</Badge>} flush>
          <Table rows={costs.scenarios ?? []} rowKey={(r: any) => r.scenario} columns={[
            { key: "scenario", label: "Scenario" },
            { key: "net_pnl", label: "Net P&L", num: true, render: (r: any) => <span className={pnlClass(r.net_pnl)}>{money(r.net_pnl, { sign: true })}</span> },
            { key: "expectancy", label: "Expectancy", num: true, render: (r: any) => money(r.expectancy, { sign: true }) },
            { key: "profit_factor", label: "PF", num: true, render: (r: any) => ratio(r.profit_factor) },
            { key: "max_drawdown", label: "Max DD", num: true, render: (r: any) => money(r.max_drawdown) },
            { key: "average_r", label: "Avg R", num: true, render: (r: any) => ratio(r.average_r) },
            { key: "min_drawdown_buffer", label: "Min DD buffer", num: true, render: (r: any) => (r.account_limit_check ? r.account_limit_check : money(r.min_drawdown_buffer)) },
            { key: "drawdown_floor_touches", label: "Floor touches", num: true, render: (r: any) => int(r.drawdown_floor_touches) },
          ]} />
          <p className="note pad">{costs.method}</p>
        </Panel>)}
      {rep.caveats && <Panel title="How to read these numbers"><ul className="notes">{rep.caveats.map((c: string) => <li key={c}>{c}</li>)}</ul></Panel>}
    </div>
  );
}

function Groups({ title, groups }: { title: string; groups: Record<string, any> }) {
  const entries = Object.entries(groups ?? {}).filter(([, v]) => v && v.trades > 0);
  return (
    <Panel title={title}>
      {!entries.length ? <Empty title="No trades" /> : (
        <Table rows={entries.map(([k, v]) => ({ k, ...v }))} rowKey={(r: any) => r.k} columns={[
          { key: "k", label: "Group", render: (r: any) => label(r.k) },
          { key: "trades", label: "Trades", num: true },
          { key: "win_rate", label: "Win %", num: true, render: (r: any) => (isNum(r.win_rate) ? pct(r.win_rate * 100) : "—") },
          { key: "expectancy", label: "Exp.", num: true, render: (r: any) => money(r.expectancy, { sign: true }) },
          { key: "net_pnl", label: "Net", num: true, render: (r: any) => <span className={pnlClass(r.net_pnl)}>{money(r.net_pnl, { sign: true })}</span> },
          { key: "sample", label: "Sample", render: (r: any) => <Badge tone={sampleTone(r.sample)} title={r.sample}>{sampleShort(r.sample)}</Badge> },
        ]} />)}
    </Panel>
  );
}

function Summ({ s, unit = "" }: { s: any; unit?: string }) {
  if (!s || !s.n) return <span className="unavail">Unavailable</span>;
  return <span className="mono">n={s.n} · median {ratio(s.median)}{unit} · p25–p75 {ratio(s.p25)}–{ratio(s.p75)}{unit} · max {ratio(s.max)}{unit}</span>;
}

function ExecutionView({ rep }: { rep: any }) {
  return (
    <div className="stack">
      <div className="row"><Badge tone="warn">{rep.label}</Badge><span className="note">No order-book or queue model: these are paper fills.</span></div>
      <div className="grid g2">
        <Panel title="Entries"><Kv rows={[
          ["Orders", int(rep.orders)],
          ["Final states", Object.entries(rep.by_final_state ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"],
          ["Partial fills", int(rep.partial_fill_orders)],
          ["Signal → order (s)", <Summ s={rep.entry?.signal_to_order_s} />],
          ["Order → fill (s)", <Summ s={rep.entry?.order_to_fill_s} />],
          ["Fill vs expected (ticks, + = adverse)", <Summ s={rep.entry?.fill_vs_expected_ticks} />],
          ["Spread at signal", <Summ s={rep.entry?.spread_at_signal} />],
          ["Spread at fill", <Summ s={rep.entry?.spread_at_fill} />],
        ]} /></Panel>
        <Panel title="Exits"><Kv rows={[
          ["Stop fills", int(rep.exits?.stop_fills)],
          ["Stop slippage (ticks, + = adverse)", <Summ s={rep.exits?.stop_slippage_ticks} />],
          ["Stops filled through a gap", int(rep.exits?.stop_gap_fills)],
          ["Target fills", int(rep.exits?.target_fills)],
          ["Exit bars with ambiguous OHLC", <span title={rep.ambiguous_ohlc_basis}>{int(rep.ambiguous_ohlc_exit_bars)}</span>],
          ["Spread at exit", <Summ s={rep.exits?.spread_at_exit} />],
        ]} /></Panel>
      </div>
      <Panel title="Notes"><ul className="notes">{(rep.notes ?? []).map((n: string) => <li key={n}>{n}</li>)}</ul></Panel>
    </div>
  );
}

function RegimeView({ rep }: { rep: any }) {
  return (
    <div className="stack">
      <p className="note">{rep.note} Trades without a regime record: {int(rep.trades_without_regime_record)}.</p>
      <Panel title="Regime buckets" flush>
        <Table rows={rep.buckets ?? []} rowKey={(r: any) => r.bucket} columns={[
          { key: "label", label: "Regime" },
          { key: "observations", label: "Observations", num: true },
          { key: "trades", label: "Trades", num: true },
          { key: "win_rate", label: "Win %", num: true, render: (r: any) => (isNum(r.win_rate) ? pct(r.win_rate * 100) : "—") },
          { key: "expectancy", label: "Expectancy", num: true, render: (r: any) => money(r.expectancy, { sign: true }) },
          { key: "net_pnl", label: "Net", num: true, render: (r: any) => <span className={pnlClass(r.net_pnl)}>{r.trades ? money(r.net_pnl, { sign: true }) : "—"}</span> },
          { key: "profit_factor", label: "PF", num: true, render: (r: any) => ratio(r.profit_factor) },
          { key: "average_mae_r", label: "MAE R", num: true, render: (r: any) => ratio(r.average_mae_r) },
          { key: "average_mfe_r", label: "MFE R", num: true, render: (r: any) => ratio(r.average_mfe_r) },
          { key: "max_drawdown", label: "Max DD", num: true, render: (r: any) => (r.trades ? money(r.max_drawdown) : "—") },
          { key: "sample", label: "Sample", render: (r: any) => <Badge tone={sampleTone(r.sample)} title={r.sample}>{sampleShort(r.sample)}</Badge> },
        ]} />
      </Panel>
    </div>
  );
}

function BlockedView({ rep }: { rep: any }) {
  const cf = rep.counterfactual ?? {};
  return (
    <div className="stack">
      <div className="grid g2">
        <Panel title={`Authorization blocks (${int(rep.authorization_blocks)})`} flush>
          <Table rows={rep.by_reason ?? []} rowKey={(r: any) => `${r.category}:${r.reason}`} columns={[
            { key: "category", label: "Category" }, { key: "reason", label: "Exact reason", render: (r: any) => <span className="mono">{r.reason}</span> },
            { key: "count", label: "Count", num: true }]} />
        </Panel>
        <Panel title="Strategy conditions not met" flush>
          <Table rows={rep.strategy_condition_failures ?? []} rowKey={(r: any) => `${r.category}:${r.reason}`} columns={[
            { key: "category", label: "Category" }, { key: "reason", label: "Exact reason", render: (r: any) => <span className="mono">{r.reason}</span> },
            { key: "count", label: "Count", num: true }]} />
        </Panel>
      </div>
      <Panel title="Missed-opportunity analysis" right={<Badge tone="warn">{cf.label}</Badge>}>
        <Kv rows={[["Blocked signals evaluated", int(cf.evaluated)], ["Target reached first", int(cf.target_first)],
          ["Stop reached first", int(cf.stop_first)], ["Ambiguous bar (both touched)", int(cf.ambiguous_bar)],
          ["Unavailable (no later bars)", int(cf.unavailable)], ["Average hypothetical R (no costs)", ratio(cf.average_r_hypothetical)]]} />
        <p className="note">{cf.method}</p>
      </Panel>
    </div>
  );
}

function ReliabilityView({ rep }: { rep: any }) {
  if (!rep.runs?.length) return <Empty title="No runs" />;
  return (
    <div className="stack">
      {rep.runs.map((r: any) => (
        <Panel key={r.run_id ?? r.run} title={<>{r.run} <Badge tone={typeTone(r.session_type)}>{r.session_type}</Badge></>}>
          <div className="grid g2">
            <Kv rows={[
              ["Provider connected", r.states?.provider_connected], ["Bridge connection", r.states?.bridge_connected],
              ["Valid events received", Object.entries(r.states?.valid_events ?? {}).map(([k, v]) => `${k} ${int(v)}`).join(" · ") || "none"],
              ["Data fresh", r.states?.data_fresh], ["System operational", r.states?.system_operational],
            ]} />
            <Kv rows={[
              ["Data availability", isNum(r.data_availability_pct) ? pct(r.data_availability_pct) : null],
              ["Disconnections / reconnections", `${int(r.disconnections)} / ${int(r.reconnections)}`],
              ["Average recovery", isNum(r.average_recovery_seconds) ? `${ratio(r.average_recovery_seconds, 1)} s` : null],
              ["Stale / gap incidents (in session)", int(r.stale_or_gap_incidents)],
              ["Heartbeat failures", int(r.heartbeat_failures)], ["Queue overloads", int(r.queue_overload_incidents)],
              ["Subscription failures", int(r.subscription_failures)],
              ["Invalid events", Object.entries(r.invalid_events ?? {}).map(([k, v]) => `${label(k)} ${v}`).join(" · ") || "0"],
            ]} />
          </div>
          <p className="note">{r.availability_basis}. {r.sequence_gap_detection}.</p>
        </Panel>))}
    </div>
  );
}

function AccountView({ rep }: { rep: any }) {
  if (!rep.runs?.length) return <Empty title="No runs" />;
  return (
    <div className="stack">
      {rep.runs.map((r: any) => (
        <Panel key={r.run} title={<>{r.run} <Badge tone={typeTone(r.session_type)}>{r.session_type}</Badge></>} right={<Badge tone="warn">SIMULATED</Badge>}>
          <p className="note">{r.label}. Rules from: {r.rules_source} (profile {r.profile ?? "unavailable"}).</p>
          <div className="stats">
            <Stat label="Equity" value={money(r.current_equity)} />
            <Stat label="DD buffer" value={money(r.remaining_drawdown_buffer)} sub="remaining" />
            <Stat label="DD floor" value={money(r.drawdown_floor)} />
            <Stat label="Max DD" value={money(r.max_observed_drawdown)} sub="observed" />
            <Stat label="Safety reserve" value={money(r.safety_reserve)} />
            <Stat label="Loss streak" value={int(r.max_consecutive_losses_observed)} sub="max observed" />
            <Stat label="Guard blocks" value={int(r.account_guard_blocks)} sub="AccountGuard" />
            <Stat label="Flattens" value={int(r.emergency_flatten_events)} sub="emergency" />
          </div>
          <div style={{ height: 10 }} />
          <Table rows={r.stress?.rows ?? []} rowKey={(x: any) => x.scenario} columns={[
            { key: "scenario", label: r.stress?.label ?? "Stress" },
            { key: "loss_per_trade_used", label: "Loss / trade", num: true, render: (x: any) => (x.result ? x.why : money(x.loss_per_trade_used)) },
            { key: "buffer_after", label: "Buffer after", num: true, render: (x: any) => money(x.buffer_after) },
            { key: "losses_to_safety_buffer", label: "Losses to safety reserve", num: true, render: (x: any) => int(x.losses_to_safety_buffer) },
            { key: "losses_to_floor", label: "Losses to floor", num: true, render: (x: any) => int(x.losses_to_floor) },
            { key: "breach", label: "Breach", render: (x: any) => (x.breach === undefined ? "—" : x.breach ? <Badge tone="neg">BREACH</Badge> : "no") },
          ]} />
        </Panel>))}
    </div>
  );
}

function CompareView({ rep }: { rep: any }) {
  if (!rep.comparable) {
    return <Panel title="Not comparable"><Empty title="No comparison shown">
      <ul className="notes">{(rep.reasons ?? []).map((r: string) => <li key={r}>{r}</li>)}</ul>{rep.note}</Empty></Panel>;
  }
  const a = rep.REPLAY ?? {}, b = rep.SHADOW ?? {};
  const line = (k: string, fmt: (s: any) => ReactNode): [ReactNode, ReactNode] => [label(k), <span>REPLAY {fmt(a[k])} │ SHADOW {fmt(b[k])}</span>];
  const s = (x: any) => (x?.n ? `median ${ratio(x.median)} (n=${x.n})` : "unavailable");
  return (
    <div className="stack">
      <Panel title={`${rep.root}: REPLAY vs SHADOW`}><Kv rows={[
        line("sessions", int), line("signals", int), line("signals_per_session", (x) => ratio(x)), line("long_share", (x) => (isNum(x) ? pct(x * 100) : "—")),
        line("stop_distance", s), line("target_distance", s), line("trend_strength_atr", s), line("rsi", s), line("rvol", s),
        line("trades", int), line("duration_min", s), line("mfe_r", s), line("mae_r", s), line("r_multiple", s),
        line("sample", (x) => x),
      ]} /></Panel>
      <Panel title="Interpretation"><ul className="notes">{(rep.notes ?? []).map((n: string) => <li key={n}>{n}</li>)}</ul></Panel>
    </div>
  );
}

function ValidationView({ rep }: { rep: any }) {
  const tone = (s: string): Tone => (s.startsWith("PASS") ? "pos" : s === "FAIL" ? "neg" : s === "WARN" ? "warn" : "off");
  return (
    <div className="stack">
      <div className="row"><Badge tone={tone(rep.overall)}>Overall {rep.overall}</Badge><span className="note">{rep.note}</span></div>
      <Panel title="Checks" flush>
        <Table rows={rep.checks ?? []} rowKey={(r: any) => r.check} columns={[
          { key: "check", label: "Check" },
          { key: "status", label: "Status", render: (r: any) => <Badge tone={tone(r.status)}>{r.status}</Badge> },
          { key: "detail", label: "Detail", render: (r: any) => (typeof r.detail === "string" ? r.detail : JSON.stringify(r.detail)) },
        ]} />
      </Panel>
    </div>
  );
}
