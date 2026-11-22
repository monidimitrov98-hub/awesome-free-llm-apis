import { Badge, Empty, ErrorBox, Kv, Loading, Panel, Table, useApi } from "../components/ui";
import { UNAVAILABLE, int, isNum, label, price, ratio, time, type Tone } from "../lib/format";

function withStatus(value: string, status: unknown): string | null {
  const s = label(status);
  if (value === UNAVAILABLE) return s ? s : null;
  return s ? `${value} (${s})` : value;
}
import { tzOf } from "../state/prefs";
import { useInstrument } from "../state/instrument";
import { useTerminal } from "../state/store";

const STEP_TONE: Record<string, Tone> = { pass: "pos", fail: "neg", not_evaluated: "off" };

export function StrategyMonitor() {
  const { state, prefs } = useTerminal();
  const { instrument } = useInstrument();
  const tz = tzOf(prefs);
  const { data, error, loading } = useApi<any>(`/api/strategy?root=${instrument}`, state.activityTick, 2000);
  const risk = useApi<any>("/api/risk", state.statusTick, 3000).data;
  if (error) return <ErrorBox error={`Strategy state unavailable: ${error}`} />;
  if (loading && !data) return <Loading what="Loading strategy state" />;
  const news = risk?.news_guard?.instruments?.[instrument];
  const guard = risk?.account_guard;
  return (
    <div className="stack">
      <div className="page-head"><h1>Strategy Monitor</h1><span className="sub">{data?.strategy} · {instrument} · signal {data?.signal_timeframe} · context {data?.context_timeframe}</span>
        <div className="tools"><Badge tone={data?.status === "ACTIVE" ? "pos" : "off"}>{data?.status}</Badge>{data?.regime_gate && <Badge tone="info">regime gate on</Badge>}</div></div>
      <div className="grid g3">
        <Panel title="Status">
          <Kv rows={[
            ["Evaluations", int(data?.evaluations)], ["Signals", int(data?.signals)],
            ["Last signal", data?.last_signal ? `${(data.last_signal.direction ?? "").toUpperCase()} · ${time(data.last_signal.bar_time, true, tz)}` : "none yet"],
            ["Market data now", <Badge tone={data?.data_ok_now ? "pos" : "neg"}>{data?.data_ok_now ? "valid" : label(data?.data_reason_now)}</Badge>],
            ["NewsGuard now", <Badge tone={news?.safe ? "pos" : "neg"}>{label(news?.state) || "—"}</Badge>],
            ["AccountGuard now", <Badge tone={guard?.state === "OK" ? "pos" : guard?.state === "DISABLED" ? "off" : "neg"}>{guard?.state ?? "—"}</Badge>],
          ]} />
        </Panel>
        {(["long", "short"] as const).map((d) => <Pipeline key={d} dir={d} item={data?.latest?.[d]} tz={tz} />)}
      </div>
      <Panel title="Recent evaluations" flush right={<span className="note">exact strategy rejection codes</span>}>
        <Table rows={data?.recent ?? []} rowKey={(r: any, i) => `${r.time}-${r.direction}-${i}`}
          empty={<Empty title="No evaluations yet">The strategy evaluates each completed {data?.signal_timeframe} bar.</Empty>}
          columns={[
            { key: "bar_time", label: "Bar", render: (r: any) => <span className="mono">{time(r.bar_time, true, tz)}</span> },
            { key: "direction", label: "Dir", render: (r: any) => (r.direction ?? "").toUpperCase() },
            { key: "decision", label: "Decision", render: (r: any) => <Badge tone={r.decision === "signal" ? "info" : r.decision === "reject" ? "warn" : "off"}>{r.decision}</Badge> },
            { key: "rejection_reason", label: "First rejection", render: (r: any) => <span className="mono">{r.rejection_reason}</span> },
            { key: "reasons", label: "All codes", render: (r: any) => <span className="mono dim">{String(r.reasons ?? "").split(";").filter(Boolean).length}</span>, num: true },
            { key: "order_status", label: "Order", render: (r: any) => r.order_status ? <span className="mono">{r.order_status}{r.order_reason ? `: ${r.order_reason}` : ""}</span> : "" },
          ]} />
      </Panel>
      <Panel title="Entry authorization decisions" flush right={<span className="note">mode · data · request · position · NewsGuard · sizing · session · AccountGuard · RiskManager</span>}>
        <Table rows={data?.decisions ?? []} rowKey={(r: any, i) => `${r.signal_id}-${i}`}
          empty={<Empty title="No authorization decisions yet" />}
          columns={[
            { key: "time", label: "Time", render: (r: any) => <span className="mono">{time(r.time, true, tz)}</span> },
            { key: "side", label: "Dir", render: (r: any) => (r.side ?? "").toUpperCase() },
            { key: "allowed", label: "Result", render: (r: any) => <Badge tone={r.allowed ? "pos" : "neg"}>{r.allowed ? "authorized (paper)" : "rejected"}</Badge> },
            { key: "reason", label: "Reason", render: (r: any) => <span className="mono">{r.reason}</span> },
            { key: "stages", label: "Stages passed", render: (r: any) => <span className="mono dim">{(r.stages ?? []).filter((s: any) => s.ok).map((s: any) => s.stage).join(" › ")}</span> },
          ]} />
      </Panel>
    </div>
  );
}

function Pipeline({ dir, item, tz }: { dir: string; item: any; tz: string }) {
  const row = item?.row;
  return (
    <Panel title={`Decision pipeline — ${dir}`} right={row ? <span className="mono note">{time(row.bar_time, true, tz)}</span> : undefined}>
      {!row ? <Empty title="Not evaluated yet" /> : (
        <div className="stack" style={{ gap: 6 }}>
          <div className="steps">
            {item.pipeline.map((s: any, i: number) => (
              <div key={s.step} className="step">
                <span className="n">{i + 1}</span>
                <span>{s.label}{s.codes.length > 0 && <div className="mono note neg">{s.codes.join(", ")}</div>}</span>
                <Badge tone={STEP_TONE[s.status]}>{label(s.status)}</Badge>
              </div>
            ))}
            {item.authorization.map((s: any, i: number) => (
              <div key={s.stage} className="step">
                <span className="n">{item.pipeline.length + i + 1}</span>
                <span>{s.label}{!s.ok && s.reason && <div className="mono note neg">{s.reason}</div>}</span>
                <Badge tone={s.ok ? "pos" : "neg"}>{s.ok ? "pass" : "fail"}</Badge>
              </div>
            ))}
            {item.authorization.length === 0 && row.decision === "signal" && <div className="note">Authorization record not available.</div>}
          </div>
          <Kv rows={[
            ["VWAP relation", row.vwap_relation ?? null], ["EMA relation", row.ema_relation ?? null], ["Trend strength (ATR)", ratio(row.trend_strength_atr)],
            ["Structure", label(row.structure) || null], ["HTF context", label(row.context_status) || null], ["Pullback", label(row.pullback_status) || null],
            ["Candle", label(row.candle_status) || null], ["RSI", withStatus(ratio(row.rsi, 1), row.rsi_status)],
            ["Rel. volume", withStatus(ratio(row.rvol), row.rvol_status)],
            ["Stop", withStatus(price(row.stop_price), isNum(row.stop_distance) ? `dist ${ratio(row.stop_distance)}` : "")],
            ["Target", withStatus(price(row.target_price), isNum(row.target_distance) ? `dist ${ratio(row.target_distance)}` : "")],
          ]} />
        </div>
      )}
    </Panel>
  );
}
