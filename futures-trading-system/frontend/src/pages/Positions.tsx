import { Badge, Empty, ErrorBox, Loading, Panel, Table, useApi } from "../components/ui";
import { int, label, money, pnlClass, price, ratio, time } from "../lib/format";
import { tzOf } from "../state/prefs";
import { useTerminal } from "../state/store";

export function Positions() {
  const { state, prefs } = useTerminal();
  const tz = tzOf(prefs);
  const { data, error, loading } = useApi<any>("/api/positions", state.statusTick + state.activityTick, 1500);
  if (error) return <ErrorBox error={`Positions unavailable: ${error}`} />;
  if (loading && !data) return <Loading what="Loading positions" />;
  return (
    <div className="stack">
      <div className="page-head"><h1>Positions</h1><Badge tone="warn">PAPER / SIMULATED</Badge>
        <span className="sub">{data?.label}. This screen is for monitoring only: there are no order controls.</span></div>
      <Panel title="Open simulated positions" flush>
        <Table rows={data?.positions ?? []} rowKey={(r: any) => r.root}
          empty={<Empty title="No open simulated position" />}
          columns={[
            { key: "root", label: "Instrument" },
            { key: "direction", label: "Direction", render: (r: any) => <Badge tone={r.direction === "LONG" ? "pos" : "neg"}>{r.direction}</Badge> },
            { key: "quantity", label: "Qty", num: true, render: (r: any) => int(r.quantity) },
            { key: "entry_price", label: "Sim. entry", num: true, render: (r: any) => price(r.entry_price) },
            { key: "reference_price", label: "Reference", num: true, render: (r: any) => price(r.reference_price) },
            { key: "stop_loss", label: "Stop", num: true, render: (r: any) => price(r.stop_loss) },
            { key: "take_profit", label: "Target", num: true, render: (r: any) => price(r.take_profit) },
            { key: "unrealized", label: "Unrealized", num: true, render: (r: any) => <span className={pnlClass(r.unrealized)}>{money(r.unrealized, { sign: true })}</span> },
            { key: "entry_time", label: "Entry time", render: (r: any) => <span className="mono">{time(r.entry_time, true, tz)}</span> },
            { key: "status", label: "Status", render: (r: any) => <Badge tone="info">{r.status}</Badge> },
          ]} />
      </Panel>
      <Panel title="Working simulated orders" flush right={<span className="note">entries, protective stops, targets, exits</span>}>
        <Table rows={data?.working_orders ?? []} rowKey={(r: any) => r.order_id}
          empty={<Empty title="No working simulated orders" />}
          columns={[
            { key: "order_id", label: "Order", render: (r: any) => <span className="mono">{r.order_id}</span> },
            { key: "root", label: "Instr." }, { key: "role", label: "Role", render: (r: any) => label(r.role) },
            { key: "type", label: "Type" }, { key: "side", label: "Side" },
            { key: "quantity", label: "Qty", num: true, render: (r: any) => `${int(r.filled)}/${int(r.quantity)}` },
            { key: "price", label: "Price", num: true, render: (r: any) => (r.price === null ? "market" : price(r.price)) },
            { key: "state", label: "State", render: (r: any) => <Badge tone="info">{label(r.state)}</Badge> },
            { key: "created_at", label: "Created", render: (r: any) => <span className="mono">{time(r.created_at, true, tz)}</span> },
          ]} />
      </Panel>
      <Panel title="Recently closed simulated trades" flush>
        <Table rows={data?.recent_closed ?? []} rowKey={(r: any) => r.trade_id}
          empty={<Empty title="No closed simulated trades yet" />}
          columns={[
            { key: "trade_id", label: "ID", render: (r: any) => <span className="mono">{r.trade_id}</span> },
            { key: "root", label: "Instr." }, { key: "direction", label: "Dir" },
            { key: "entry_price", label: "Entry", num: true, render: (r: any) => price(r.entry_price) },
            { key: "exit_price", label: "Exit", num: true, render: (r: any) => price(r.exit_price) },
            { key: "net_pnl", label: "Net (realized)", num: true, render: (r: any) => <span className={pnlClass(r.net_pnl)}>{money(r.net_pnl, { sign: true })}</span> },
            { key: "r_multiple", label: "R", num: true, render: (r: any) => ratio(r.r_multiple) },
            { key: "exit_reason", label: "Exit reason", render: (r: any) => label(r.exit_reason) },
            { key: "exit_time", label: "Exit time", render: (r: any) => <span className="mono">{time(r.exit_time, true, tz)}</span> },
          ]} />
      </Panel>
    </div>
  );
}
