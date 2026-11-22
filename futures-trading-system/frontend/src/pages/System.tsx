import { useState } from "react";
import { qs } from "../api/client";
import { Badge, Empty, ErrorBox, Kv, Loading, Panel, Stat, Table, useApi } from "../components/ui";
import { age, int, time } from "../lib/format";
import { tzOf } from "../state/prefs";
import { useTerminal } from "../state/store";

export function SystemHealth() {
  const { state, prefs } = useTerminal();
  const tz = tzOf(prefs);
  const { data, error, loading } = useApi<any>("/api/system", state.statusTick, 2000);
  const [level, setLevel] = useState("INFO");
  const [q, setQ] = useState("");
  const logs = useApi<any[]>(`/api/logs${qs({ level, q: q.trim(), limit: 300 })}`, state.activityTick, 1500);
  if (error) return <ErrorBox error={`System state unavailable: ${error}`} />;
  if (loading && !data) return <Loading what="Loading diagnostics" />;
  const p = data.process, t = data.terminal, s = data.session ?? {};
  const ok = (b: boolean | undefined) => <Badge tone={b ? "pos" : "neg"}>{b ? "ok" : "problem"}</Badge>;
  return (
    <div className="stack">
      <div className="page-head"><h1>System Health</h1><span className="sub">Diagnostics only. No secrets, keys or file paths are shown.</span></div>
      <Panel title="Process" flush>
        <div className="stats">
          <Stat label="Backend" value={<Badge tone={t.state === "running" ? "pos" : t.state === "failed" ? "neg" : "warn"}>{t.state}</Badge>} />
          <Stat label="Frontend" value={<Badge tone="pos">running</Badge>} />
          <Stat label="WebSocket" value={<Badge tone={state.live === "open" ? "pos" : "neg"}>{state.live}</Badge>} sub={`${int(t.ws_clients)} client(s)`} />
          <Stat label="CPU" value={p.cpu_percent === null ? null : `${p.cpu_percent}%`} />
          <Stat label="Memory" value={p.memory_mb === null ? null : `${p.memory_mb} MB`} />
          <Stat label="Uptime" value={age(p.uptime_seconds)} />
          <Stat label="Threads" value={int(p.threads)} />
          <Stat label="Python" value={p.python} sub={p.platform} />
        </div>
      </Panel>
      <div className="grid g3">
        <Panel title="Components">
          <Kv rows={[
            ["Paper runtime", <>{s.runtime?.run_kind} {ok(!s.runtime?.stopped_reason)}</>],
            ["Events processed", int(s.runtime?.events_processed)],
            ["Strategy engine", Object.entries(s.strategy ?? {}).map(([k, v]) => `${k}: ${v}`).join(" · ")],
            ["Market-regime engine", s.regime?.enabled ? `enabled · errors ${int(s.regime?.errors)}` : "disabled"],
            ["News calendar", s.news?.readiness ?? (s.news?.mode === "research" ? "research (replay)" : "—")],
            ["Journal", <>{int(s.journal?.entries)} entries {ok(s.journal?.persistence_ok)}</>],
            ["Journal integrity key", s.journal?.keyed ? "HMAC (keyed)" : "unkeyed SHA-256 chain"],
            ["Reconciliation", ok(s.runtime?.reconciliation_ok)],
            ["Market-data bridge", s.feed?.provider ? (s.feed?.authenticated ? "authenticated" : "disconnected") : s.feed?.feed],
          ]} />
        </Panel>
        <Panel title="Errors">
          <Kv rows={[["Terminal error", t.error ?? "none"], ["Session outcome", t.outcome ?? "running"], ["Last session error", s.last_error ?? "none"],
            ["Feed last error", s.feed?.last_error || "none"], ["Notifications suppressed (dedup)", int(t.notifications_suppressed)],
            ["Messages published", int(t.published)]]} />
        </Panel>
        <Panel title="Event latency">
          <Kv rows={[["Processing p50", s.latency?.processing_ms_p50 === null ? null : `${s.latency?.processing_ms_p50} ms`],
            ["Processing p99", s.latency?.processing_ms_p99 === null ? null : `${s.latency?.processing_ms_p99} ms`],
            ["Data delay p99", s.latency?.data_delay_ms_p99 === null ? null : `${s.latency?.data_delay_ms_p99} ms`],
            ["Samples", int(s.latency?.samples)]]} />
        </Panel>
      </div>
      <Panel title="Diagnostic log" flush right={<div className="row">
        <select value={level} onChange={(e) => setLevel(e.target.value)} aria-label="Minimum level">
          {["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((l) => <option key={l}>{l}</option>)}</select>
        <input type="search" value={q} maxLength={60} placeholder="Filter…" onChange={(e) => setQ(e.target.value)} aria-label="Filter log" /></div>}>
        <Table rows={[...(logs.data ?? [])].reverse()} rowKey={(r: any, i) => `${r.source}-${r.seq}-${i}`}
          empty={<Empty title="No log entries" />}
          columns={[
            { key: "time", label: "Time", render: (r: any) => <span className="mono">{time(r.time, true, tz)}</span> },
            { key: "level", label: "Level", render: (r: any) => <Badge tone={r.level === "ERROR" || r.level === "CRITICAL" ? "neg" : r.level === "WARNING" ? "warn" : "off"}>{r.level}</Badge> },
            { key: "source", label: "Source", render: (r: any) => <span className="mono dim">{r.source}</span> },
            { key: "message", label: "Message", render: (r: any) => <span className="mono">{r.message}</span> },
          ]} />
      </Panel>
    </div>
  );
}
