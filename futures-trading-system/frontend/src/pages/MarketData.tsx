import { Badge, Empty, Kv, Panel, Stat, Table } from "../components/ui";
import { age, healthTone, int, label, price, time } from "../lib/format";
import { tzOf } from "../state/prefs";
import { useTerminal } from "../state/store";

export function MarketData() {
  const { state, prefs } = useTerminal();
  const tz = tzOf(prefs);
  const m = state.market;
  const feed = (m?.feed ?? {}) as Record<string, any>;
  const lat = m?.latency ?? {};
  const rejected = Object.entries(m?.validation.rejected ?? {});
  const isBridge = feed.provider !== undefined;
  const warnings: string[] = [];
  if (state.live !== "open") warnings.push("live stream to the backend is not connected");
  if (isBridge && feed.authenticated === false) warnings.push("market-data bridge DISCONNECTED");
  for (const [r, v] of Object.entries(m?.instruments ?? {})) {
    if (v.health !== "ok") warnings.push(`${r}: ${label(v.health)}`);
    if (v.subscription !== "subscribed" && v.subscription !== "n/a") warnings.push(`${r}: not subscribed (${v.subscription})`);
  }
  if (rejected.length) warnings.push(`${rejected.reduce((s, [, n]) => s + n, 0)} market events rejected by validation`);
  if ((lat.data_delay_ms_p99 ?? 0) > 3000) warnings.push("feed delayed (p99 data delay > 3 s)");
  return (
    <div className="stack">
      <div className="page-head"><h1>Market Data</h1><span className="sub">{String(feed.provider ?? feed.feed ?? "")} · validation, data health and latency from the backend</span></div>
      {warnings.length > 0 ? <div className="banner critical" role="alert" style={{ borderRadius: 3 }}>FEED WARNINGS<span className="why">{warnings.join(" · ")}</span></div>
        : m && <div className="banner info" style={{ borderRadius: 3 }}>All instruments healthy</div>}
      <div className="grid g3">
        <Panel title="Connection">
          <Kv rows={isBridge ? [
            ["Provider", feed.provider], ["Bridge connection", <Badge tone={feed.authenticated ? "pos" : "neg"}>{feed.authenticated ? "authenticated" : "disconnected"}</Badge>],
            ["Bridge client", feed.bridge_client ?? null], ["Bridge time zone", feed.bridge_time_zone ?? null],
            ["Provider connection", <Badge tone={feed.provider_connection === "Connected" ? "pos" : feed.provider_connection === "unknown" ? "off" : "neg"}>{feed.provider_connection}</Badge>],
            ["Listener", feed.address], ["Connections / reconnections", `${int(feed.connections)} / ${int(feed.reconnections)}`],
            ["Last message", time(feed.last_message_at, true, tz)], ["Last heartbeat", time(feed.last_heartbeat_at, true, tz)],
            ["Last error", feed.last_error || "none"],
          ] : [["Feed", feed.feed], ["Kind", feed.kind], ["Connected", feed.connected ? "yes" : "no"],
            ["Note", "replay feed: historical data, not a live connection"]]} />
        </Panel>
        <Panel title="Queue & protocol">
          {isBridge ? <Kv rows={[
            ["Queue", `${int(feed.queue_depth)} / ${int(feed.queue_capacity)}`], ["Queue overflows", int(feed.queue_overflows)],
            ["Events received", int(feed.events_received)], ["Dropped by bridge", int(feed.dropped_by_bridge)],
            ["Protocol violations", int(feed.protocol_violations)], ["Refused handshakes", int(feed.refused_handshakes)],
            ["Rejected messages", Object.entries(feed.rejected_messages ?? {}).map(([k, v]) => `${k}: ${v}`).join(", ") || "none"],
          ]} /> : <Empty title="Not applicable">The replay feed has no bridge queue.</Empty>}
        </Panel>
        <Panel title="Latency & validation" flush>
          <div className="stats">
            <Stat label="Processing p50" value={lat.processing_ms_p50 === null ? null : `${lat.processing_ms_p50} ms`} />
            <Stat label="Processing p99" value={lat.processing_ms_p99 === null ? null : `${lat.processing_ms_p99} ms`} />
            <Stat label="Data delay p50" value={lat.data_delay_ms_p50 === null ? null : `${lat.data_delay_ms_p50} ms`} />
            <Stat label="Data delay p99" value={lat.data_delay_ms_p99 === null ? null : `${lat.data_delay_ms_p99} ms`} />
            <Stat label="Events processed" value={int(m?.validation.events_processed)} />
            <Stat label="Session" value={m?.market_session.in_session ? m.market_session.session_date : "CLOSED"} />
          </div>
          <div className="panel-b"><div className="note upper">Rejected events</div>
            {rejected.length ? rejected.map(([k, v]) => <div key={k} className="row"><span className="mono warn">{k}</span><span className="num" style={{ marginLeft: "auto" }}>{v}</span></div>)
              : <span className="dim">none</span>}</div>
        </Panel>
      </div>
      <Panel title="Instruments" flush>
        <Table rows={Object.entries(m?.instruments ?? {}).map(([r, v]) => ({ root: r, ...v }))} rowKey={(r) => r.root}
          empty={<Empty title="No market data yet" />}
          columns={[
            { key: "root", label: "Instr." }, { key: "contract", label: "Contract", render: (r) => <span className="mono">{r.contract}</span> },
            { key: "subscription", label: "Subscription", render: (r) => <Badge tone={r.subscription === "subscribed" ? "pos" : r.subscription === "n/a" ? "off" : "neg"}>{r.subscription}</Badge> },
            { key: "health", label: "Data health", render: (r) => <Badge tone={healthTone(r.health)}>{label(r.health)}</Badge> },
            { key: "last", label: "Last", num: true, render: (r) => price(r.last) }, { key: "bid", label: "Bid", num: true, render: (r) => price(r.bid) },
            { key: "ask", label: "Ask", num: true, render: (r) => price(r.ask) }, { key: "spread", label: "Spread", num: true, render: (r) => price(r.spread) },
            { key: "last_event_time", label: "Last event", render: (r) => <span className="mono">{time(r.last_event_time, true, tz)}</span> },
            { key: "freshness_seconds", label: "Age", num: true, render: (r) => age(r.freshness_seconds) },
            { key: "entries_allowed", label: "Entries (data)", render: (r) => r.entries_allowed ? <span className="pos">allowed</span> : <span className="neg">{label(r.entry_block_reason)}</span> },
          ]} />
      </Panel>
    </div>
  );
}
