import { Badge, Dot, Empty, Kv, Panel, Stat, useApi } from "../components/ui";
import { age, healthTone, int, label, money, newsTone, pct, pnlClass, price, ratio, time } from "../lib/format";
import { tzOf } from "../state/prefs";
import { useTerminal } from "../state/store";
import type { Activity } from "../api/types";

export function ActivityFeed({ items, tz, limit = 30 }: { items: Activity[]; tz: string; limit?: number }) {
  if (!items.length) return <Empty title="No activity yet">Signals, rejections, simulated fills and warnings appear here.</Empty>;
  const tone = (k: string, p: Record<string, unknown>) =>
    k === "entry_decision" ? (p.allowed ? "info" : "warn") : k === "execution" ? "pos" : k === "trade_closed" ? "info"
      : ["system_error", "account_guard", "daily_loss_limit", "flatten_requested", "feed_disconnected"].includes(k) ? "neg"
        : ["feed_health", "data_delayed", "data_rejected", "price_level_reset"].includes(k) ? "warn" : "off";
  return (
    <div className="feed">
      {[...items].reverse().slice(0, limit).map((a) => (
        <div key={a.seq} className="feed-item">
          <span className="ts">{time(a.time, false, tz)}</span>
          <Dot tone={tone(a.kind, a.payload) as never} />
          <span>{a.summary}</span>
        </div>
      ))}
    </div>
  );
}

const INTERESTING = new Set(["entry_decision", "execution", "trade_closed", "flatten_requested", "account_guard", "daily_loss_limit",
  "feed_health", "data_delayed", "feed_disconnected", "feed_connected", "system_error", "emergency_stop", "price_level_reset"]);

export function Dashboard() {
  const { state, prefs } = useTerminal();
  const tz = tzOf(prefs);
  const { market, status, meta } = state;
  const risk = useApi<any>("/api/risk", state.statusTick, 3000).data;
  const a = status?.account;
  const rm = risk?.risk_manager;
  const ag = risk?.account_guard;
  return (
    <div className="stack">
      <div className="page-head"><h1>Dashboard</h1><span className="sub">{meta?.description}</span></div>
      <div className="grid g2">
        {(meta?.instruments ?? []).map((r) => {
          const m = market?.instruments[r];
          return (
            <Panel key={r} title={<>{r} <span className="mono dim">{m?.contract}</span></>}
              right={<><Badge tone={healthTone(m?.health)}>{m ? (m.health === "ok" ? "data ok" : label(m.health)) : "no data"}</Badge>
                <Badge tone={m?.subscription === "subscribed" ? "pos" : m?.subscription === "n/a" ? "off" : "neg"}>{m?.subscription === "n/a" ? "replay feed" : m?.subscription ?? "—"}</Badge></>} flush>
              <div className="stats">
                <Stat label="Last" value={price(m?.last)} />
                <Stat label="Bid" value={price(m?.bid)} />
                <Stat label="Ask" value={price(m?.ask)} />
                <Stat label="Spread" value={price(m?.spread)} />
                <Stat label="Freshness" value={age(m?.freshness_seconds)} tone={(m?.freshness_seconds ?? 0) > 10 ? "warn" : ""} />
                <Stat label="Session" value={m?.session_date ?? "CLOSED"} sub={m?.entries_allowed ? "entries allowed by data" : label(m?.entry_block_reason) || "—"} />
              </div>
            </Panel>
          );
        })}
      </div>
      <div className="grid g3">
        <Panel title="Trading (paper / simulated)" flush>
          <div className="stats">
            <Stat label="Session P&L" value={money(a?.session_pnl, { sign: true })} tone={pnlClass(a?.session_pnl)} />
            <Stat label="Realized net" value={money(a?.net_realized, { sign: true })} tone={pnlClass(a?.net_realized)} />
            <Stat label="Unrealized" value={money(a?.unrealized, { sign: true })} tone={pnlClass(a?.unrealized)} />
            <Stat label="Total costs" value={money(a?.total_costs)} />
            <Stat label="Trades" value={int(a?.trades)} sub={`${a?.wins ?? 0} W / ${a?.losses ?? 0} L`} />
            <Stat label="Win rate" value={pct((a?.win_rate ?? NaN) * 100)} />
            <Stat label="Profit factor" value={ratio(a?.profit_factor)} title="Shown from 5 closed trades" />
          </div>
          {a?.sample_warning && <div className="panel-b note">INSUFFICIENT SAMPLE SIZE — statistics are not meaningful yet.</div>}
        </Panel>
        <Panel title="Risk" flush>
          <div className="stats">
            <Stat label="Equity" value={money(a?.equity)} sub={a && !a.equity_complete ? "an open position has no mark" : undefined} />
            <Stat label="Daily loss used" value={pct(rm?.daily_loss_used_pct)} tone={(rm?.daily_loss_used_pct ?? 0) >= 75 ? "neg" : ""} />
            <Stat label="Daily loss left" value={money(rm?.daily_loss_remaining)} />
            <Stat label="Max drawdown" value={money(a?.max_drawdown)} />
            <Stat label="DD buffer left" value={money(ag?.remaining_buffer)} />
            <Stat label="AccountGuard" value={<Badge tone={ag?.state === "OK" ? "pos" : ag?.state === "DISABLED" ? "off" : "neg"}>{ag?.state ?? "—"}</Badge>} />
          </div>
          <div className="panel-b">
            <div className="row">
              <span className="muted">NewsGuard</span>
              {Object.entries(status?.news ?? {}).map(([r, n]) => <Badge key={r} tone={newsTone(n.state)}>{r} {label(n.state)}</Badge>)}
            </div>
            <div className="row" style={{ marginTop: 6 }}>
              <span className="muted">Entries</span>
              {!status?.mode_authorizes_entries && <Badge tone="warn">mode never authorizes</Badge>}
              {Object.entries(status?.entries ?? {}).map(([r, e]) => <Badge key={r} tone={e.ok ? "pos" : "neg"} title={e.reason}>{r} {e.ok ? "system ok" : label(e.reason)}</Badge>)}
            </div>
          </div>
        </Panel>
        <Panel title="System">
          <Kv rows={[
            ["Backend", <Badge tone={state.api === "ok" ? "pos" : "neg"}>{state.api === "ok" ? "running" : "offline"}</Badge>],
            ["Live stream", <Badge tone={state.live === "open" ? "pos" : "neg"}>{state.live}</Badge>],
            ["Market feed", <Badge tone={market && market.feed?.authenticated !== false ? "pos" : "neg"}>{String(market?.feed?.provider ?? market?.feed?.feed ?? "—")}</Badge>],
            ["Paper runtime", status?.runtime.state ?? "—"],
            ["Strategy", status?.runtime.stopped_reason ? "inactive" : "active"],
            ["Events processed", int(market?.validation.events_processed)],
            ["Rejected events", int(Object.values(market?.validation.rejected ?? {}).reduce((x, y) => x + y, 0))],
            ["Last event", time(Object.values(market?.instruments ?? {}).map((m) => m.last_event_time).filter(Boolean).sort().pop() ?? null, false, tz)],
            ["Last error", meta?.error ?? (String(market?.feed?.last_error || "") || "none")],
          ]} />
        </Panel>
      </div>
      <Panel title="Recent activity" flush right={<span className="note">signals · rejections · simulated fills · exits · risk · data health</span>}>
        <ActivityFeed items={state.activity.filter((x) => INTERESTING.has(x.kind))} tz={tz} />
      </Panel>
    </div>
  );
}
