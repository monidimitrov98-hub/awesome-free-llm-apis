import { useState } from "react";
import { downloadCsv, qs } from "../api/client";
import { Badge, Empty, ErrorBox, Kv, Modal, Panel, Table, useApi } from "../components/ui";
import { int, label, money, pnlClass, price, ratio, time } from "../lib/format";
import { tzOf } from "../state/prefs";
import { useTerminal } from "../state/store";

interface Filters { root: string; direction: string; outcome: string; start: string; end: string; strategy: string; regime: string;
  exit_reason: string; q: string }
const EMPTY: Filters = { root: "", direction: "", outcome: "", start: "", end: "", strategy: "", regime: "", exit_reason: "", q: "" };

export function Journal() {
  const { state, prefs } = useTerminal();
  const tz = tzOf(prefs);
  const [f, setF] = useState<Filters>(EMPTY);
  const [sort, setSort] = useState<{ key: string; order: "asc" | "desc" }>({ key: "exit_time", order: "desc" });
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<any>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const query = qs({ ...f, q: f.q.trim(), sort: sort.key, order: sort.order, page, page_size: 25 });
  const { data, error } = useApi<any>(`/api/journal${query}`, state.activityTick, 1000);
  const set = (k: keyof Filters, v: string) => { setF({ ...f, [k]: v }); setPage(1); };
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  const facets = data?.facets ?? { root: [], strategy: [], regime: [], exit_reason: [] };
  const sel = (k: keyof Filters, opts: string[], all: string) => (
    <select value={f[k]} onChange={(e) => set(k, e.target.value)} aria-label={all}>
      <option value="">{all}</option>{opts.map((o) => <option key={o} value={o}>{label(o)}</option>)}
    </select>);

  return (
    <div className="stack">
      <div className="page-head"><h1>Trade Journal</h1><Badge tone="warn">PAPER / SIMULATED</Badge><span className="sub">{data?.label}</span>
        <div className="tools">
          <button className="btn" onClick={() => downloadCsv(`/api/journal.csv${qs({ ...f, sort: sort.key, order: sort.order })}`, "paper_trade_journal.csv")
            .catch((e: Error) => setExportError(e.message))}>Export CSV</button></div></div>
      {exportError && <ErrorBox error={`Export failed: ${exportError}`} />}
      <Panel title="Filters">
        <div className="row">
          {sel("root", facets.root, "All instruments")}
          {sel("direction", ["long", "short"], "Long & short")}
          {sel("outcome", ["win", "loss"], "Wins & losses")}
          {sel("strategy", facets.strategy, "All strategies")}
          {sel("regime", facets.regime, "All regimes")}
          {sel("exit_reason", facets.exit_reason, "All exit reasons")}
          <label className="check">From <input type="date" value={f.start} onChange={(e) => set("start", e.target.value)} /></label>
          <label className="check">To <input type="date" value={f.end} onChange={(e) => set("end", e.target.value)} /></label>
          <input type="search" placeholder="Search…" value={f.q} maxLength={60} onChange={(e) => set("q", e.target.value)} aria-label="Search trades" />
          <button className="btn" onClick={() => { setF(EMPTY); setPage(1); }}>Reset</button>
        </div>
      </Panel>
      {error && <ErrorBox error={`Journal unavailable: ${error}`} />}
      <Panel title={`Trades (${int(data?.total ?? 0)})`} flush right={<div className="row">
        <button className="btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>Prev</button>
        <span className="mono">{page} / {pages}</span>
        <button className="btn" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button></div>}>
        <Table rows={data?.rows ?? []} rowKey={(r: any) => r.trade_id} onRow={setDetail} sort={sort}
          onSort={(k) => setSort({ key: k, order: sort.key === k && sort.order === "desc" ? "asc" : "desc" })}
          empty={<Empty title="No trades">{state.meta?.mode === "MOCK" ? "MOCK mode never authorizes entries." : "No closed simulated trades match the filters."}</Empty>}
          columns={[
            { key: "trade_id", label: "ID", sortable: true, render: (r: any) => <span className="mono">{r.trade_id}</span> },
            { key: "root", label: "Instr.", sortable: true },
            { key: "direction", label: "Dir", sortable: true, render: (r: any) => <Badge tone={r.direction === "LONG" ? "pos" : "neg"}>{r.direction}</Badge> },
            { key: "entry_time", label: "Entry", sortable: true, render: (r: any) => <span className="mono">{time(r.entry_time, true, tz)}</span> },
            { key: "exit_time", label: "Exit", sortable: true, render: (r: any) => <span className="mono">{time(r.exit_time, true, tz)}</span> },
            { key: "entry_price", label: "Entry px", num: true, render: (r: any) => price(r.entry_price) },
            { key: "exit_price", label: "Exit px", num: true, render: (r: any) => price(r.exit_price) },
            { key: "quantity", label: "Qty", num: true, sortable: true, render: (r: any) => int(r.quantity) },
            { key: "gross_pnl", label: "Gross", num: true, sortable: true, render: (r: any) => <span className={pnlClass(r.gross_pnl)}>{money(r.gross_pnl, { sign: true })}</span> },
            { key: "total_costs", label: "Costs", num: true, render: (r: any) => money(r.total_costs) },
            { key: "net_pnl", label: "Net", num: true, sortable: true, render: (r: any) => <span className={pnlClass(r.net_pnl)}>{money(r.net_pnl, { sign: true })}</span> },
            { key: "r_multiple", label: "R", num: true, sortable: true, render: (r: any) => ratio(r.r_multiple) },
            { key: "exit_reason", label: "Exit reason", sortable: true, render: (r: any) => label(r.exit_reason) },
            { key: "strategy", label: "Strategy", render: (r: any) => r.strategy ?? "" },
            { key: "regime", label: "Regime", sortable: true, render: (r: any) => label(r.regime) },
          ]} />
      </Panel>
      <p className="note">Gross P&L is after simulated slippage; costs = commission + slippage. The journal is read-only: the GUI never alters it.</p>
      {detail && (
        <Modal title={`Trade ${detail.trade_id} — PAPER / SIMULATED`} onClose={() => setDetail(null)}>
          <Kv rows={Object.entries(detail).map(([k, v]) => [label(k), typeof v === "number" ? String(Math.round(v * 10000) / 10000) : String(v ?? "")])} />
        </Modal>
      )}
    </div>
  );
}
