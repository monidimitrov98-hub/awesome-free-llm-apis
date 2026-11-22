import { Badge, Empty, ErrorBox, HBar, Kv, Loading, Panel, Table, useApi } from "../components/ui";
import { int, label, money, newsTone, pct, time } from "../lib/format";
import { tzOf } from "../state/prefs";
import { useTerminal } from "../state/store";

export function RiskCenter() {
  const { state, prefs } = useTerminal();
  const tz = tzOf(prefs);
  const { data, error, loading } = useApi<any>("/api/risk", state.statusTick, 1500);
  if (error) return <ErrorBox error={`Risk state unavailable: ${error}`} />;
  if (loading && !data) return <Loading what="Loading risk state" />;
  const g = data.account_guard, r = data.risk_manager, n = data.news_guard, e = data.entries;
  const gTone = g.state === "OK" ? "pos" : g.state === "DISABLED" ? "off" : "neg";
  const bufferMax = g.max_drawdown ?? null;
  const readiness = n.readiness ?? {};
  const anyNewsBlock = Object.values(n.instruments ?? {}).some((x: any) => !x.safe);
  return (
    <div className="stack">
      <div className="page-head"><h1>Risk Center</h1><span className="sub">Backend protections are authoritative; this screen cannot change them.</span>
        <div className="tools"><Badge tone={gTone as never}>AccountGuard {g.state}</Badge>{anyNewsBlock && <Badge tone="neg">NewsGuard blocking entries</Badge>}
          {r.emergency_stop && <Badge tone="neg">EMERGENCY STOP</Badge>}</div></div>
      {readiness.verdict && readiness.verdict !== "OPERATIONAL READY" && readiness.mode === "operational" &&
        <div className="banner warning" style={{ borderRadius: 3 }}>NEWS CALENDAR: {readiness.verdict} — every new entry is BLOCKED by the operational NewsGuard
          <span className="why">{(readiness.findings ?? []).slice(0, 2).map((f: any) => (typeof f === "string" ? f : f.message ?? JSON.stringify(f))).join(" · ")}</span></div>}
      <div className="grid g3">
        <Panel title="AccountGuard" right={<Badge tone={gTone as never}>{g.state}</Badge>}>
          {!g.enabled ? <Empty title="Disabled">Account protection is disabled in the backtest account profile.</Empty> : (
            <div className="stack" style={{ gap: 8 }}>
              <Kv rows={[
                ["Profile", g.profile], ["Drawdown type", label(g.drawdown_type)], ["Account size", money(g.account_size)],
                ["Equity", money(g.equity)], ["Peak (drawdown base)", money(g.peak)], ["High-water mark", money(g.high_water)],
                ["Drawdown floor", money(g.floor)], ["Current drawdown", money(g.current_drawdown)],
                ["Remaining buffer", money(g.remaining_buffer)], ["Safety buffer (internal reserve)", money(g.safety_buffer)],
                ["Flatten buffer", money(g.flatten_buffer)], ["Gap reserve", `${int(g.gap_reserve_ticks)} ticks`],
                ["Entry permission", g.entry_block_reason ? <span className="neg">{label(g.entry_block_reason)}</span> : <span className="pos">allowed</span>],
                ["Lock after flatten", label(g.lock_after_flatten)],
                ["Locked", g.locked_permanently ? "PERMANENT" : g.locked_session ? `session ${String(g.locked_session).slice(0, 10)}` : "no"],
                ["Breached", g.breached ? <span className="neg">YES</span> : "no"],
              ]} />
              <div><div className="note">Drawdown buffer remaining</div><HBar value={g.remaining_buffer} max={bufferMax} tone={(g.remaining_buffer ?? 0) <= (g.safety_buffer ?? 0) ? "neg" : "pos"} /></div>
            </div>)}
        </Panel>
        <Panel title="RiskManager">
          <div className="stack" style={{ gap: 8 }}>
            <Kv rows={[
              ["Sizing", `${r.sizing_method}${r.risk_per_trade !== null ? ` · ${money(r.risk_per_trade)} per trade` : ""}`],
              ["Max daily loss", money(r.max_daily_loss)], ["Daily loss basis", label(r.daily_loss_basis)], ["On breach", label(r.daily_loss_action)],
              ["Session", r.session_date ? String(r.session_date).slice(0, 10) : null], ["Daily P&L", money(r.daily_pnl, { sign: true })],
              ["Daily loss used", pct(r.daily_loss_used_pct)], ["Daily loss remaining", money(r.daily_loss_remaining)],
              ["Trades today", `${int(r.daily_trades)}${r.max_daily_trades ? ` / ${r.max_daily_trades}` : ""}`],
              ["Consecutive losses", `${int(r.consecutive_losses)}${r.max_consecutive_losses ? ` / ${r.max_consecutive_losses}` : ""}`],
              ["Max position size", r.max_position_size === null ? "not limited" : int(r.max_position_size)],
              ["Exposure", Object.entries(r.exposure ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ")],
              ["Daily loss breached", r.daily_loss_breached ? <span className="neg">YES</span> : "no"],
              ["Emergency stop", r.emergency_stop ? <span className="neg">{r.emergency_reason}</span> : "inactive"],
            ]} />
            <div><div className="note">Daily loss used</div><HBar value={r.daily_loss_used_pct} max={100} tone={(r.daily_loss_used_pct ?? 0) >= 75 ? "neg" : "warn"} /></div>
          </div>
        </Panel>
        <Panel title="Entry authorization">
          <Kv rows={[
            ["Run mode", <>{e.mode}{!e.mode_authorizes_entries && <Badge tone="warn">never authorizes</Badge>}</>],
            ["Accepting entries", e.accepting_entries ? "yes" : <span className="neg">no</span>],
            ["Reconciliation", e.reconciliation_ok ? "ok" : <span className="neg">FAILED</span>],
            ["Persistence", e.persistence_ok ? "ok" : <span className="neg">FAILING</span>],
            ...Object.entries(e.instruments ?? {}).map(([k, v]: [string, any]) => [`${k} data & system`,
              v.data_and_system_ok ? <span className="pos">ok</span> : <span className="neg">{label(v.reason)}</span>] as [string, JSX.Element]),
          ]} />
        </Panel>
      </div>
      <Panel title="NewsGuard" right={<span className="note">readiness: {readiness.verdict ?? (readiness.mode === "research" ? "research calendar (replay)" : "—")}</span>} flush>
        <Table rows={Object.entries(n.instruments ?? {}).map(([k, v]: [string, any]) => ({ root: k, ...v }))} rowKey={(x: any) => x.root}
          columns={[
            { key: "root", label: "Instr." },
            { key: "state", label: "State", render: (x: any) => <Badge tone={newsTone(x.state)}>{label(x.state)}</Badge> },
            { key: "safe", label: "Entries", render: (x: any) => x.safe ? <span className="pos">allowed by news</span> : <span className="neg">BLOCKED</span> },
            { key: "next", label: "Next known event", render: (x: any) => x.next_event ? `${x.next_event.name}${x.next_event.category ? ` (${x.next_event.category})` : ""}` : "none known" },
            { key: "when", label: "Starts", render: (x: any) => x.next_event ? <span className="mono">{time(x.next_event.window_start, true, tz)}</span> : "" },
            { key: "in", label: "In", num: true, render: (x: any) => x.next_event?.minutes_until !== null && x.next_event ? `${int(x.next_event.minutes_until)} min` : "" },
            { key: "age", label: "Calendar age", num: true, render: (x: any) => x.calendar_age_minutes === null || x.calendar_age_minutes === undefined ? "UNAVAILABLE" : `${int(x.calendar_age_minutes)} min` },
            { key: "reasons", label: "Reasons", render: (x: any) => <span className="mono dim" title={(x.reasons ?? []).join("\n")}>{(x.reasons ?? [])[0] ?? ""}</span> },
          ]} />
      </Panel>
      <Panel title="Protection timeline" flush right={<span className="note">entry blocks · data health · news · daily loss · flatten</span>}>
        <Table rows={[...(data.timeline ?? [])].reverse()} rowKey={(x: any) => String(x.seq)}
          empty={<Empty title="No protection events in this session" />}
          columns={[
            { key: "time", label: "Time", render: (x: any) => <span className="mono">{time(x.time, true, tz)}</span> },
            { key: "kind", label: "Event", render: (x: any) => <Badge tone={["account_guard", "daily_loss_limit", "emergency_stop", "system_error", "flatten_requested"].includes(x.kind) ? "neg" : "warn"}>{label(x.kind)}</Badge> },
            { key: "root", label: "Instr.", render: (x: any) => x.root ?? "all" },
            { key: "summary", label: "Detail", render: (x: any) => <span className="mono">{x.summary}</span> },
          ]} />
      </Panel>
    </div>
  );
}
