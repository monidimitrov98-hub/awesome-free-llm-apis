// Prop Firm Center (Sprint 9): READ-ONLY view of the prop firm compliance engine. Every figure and every
// status comes from the backend (/api/propfirm/*). There is no control to place orders, change an
// account, activate a profile, override a rule or submit a payout.
import { useState, type ReactNode } from "react";
import { downloadCsv } from "../api/client";
import { Badge, Empty, ErrorBox, Kv, Loading, Panel, Seg, Stat, Table, useApi } from "../components/ui";
import { int, isNum, label, money, pct, time, type Tone } from "../lib/format";
import { tzOf } from "../state/prefs";
import { useTerminal } from "../state/store";

export function statusTone(s?: string | null): Tone {
  // SAFE is green only when the backend says SAFE (never when a mandatory rule is unresolved)
  return s === "SAFE" ? "pos" : s === "WARNING" ? "warn" : s === "BLOCKED" ? "neg" : s === "UNVERIFIED" ? "info" : "off";
}
const resultTone = (r: string): Tone => (r === "PASS" ? "pos" : r === "WARNING" ? "warn" : r === "BLOCK" ? "neg" : r === "UNVERIFIED" ? "info" : "off");
const verifTone = (v?: string): Tone => (v === "VERIFIED" ? "pos" : v === "UNVERIFIED" || v === "NOT_FOUND" || v === "CONFLICTING" ? "warn" : "off");
const verifLabel = (v?: string) => (v === "ENGINE" ? "engine check" : v ?? "—");
const payoutTone = (s?: string): Tone => (s === "RULES SATISFIED" ? "pos" : s === "RULES NOT SATISFIED" ? "neg" : "warn");
type View = "status" | "rules" | "library" | "compare" | "history";

export function PropFirmCenter() {
  const { state, prefs } = useTerminal();
  const tz = tzOf(prefs);
  const [view, setView] = useState<View>("status");
  const st = useApi<any>("/api/propfirm/status", state.statusTick, 2000);
  const lib = useApi<any>("/api/propfirm/profiles", 0, 60000);
  const [exportError, setExportError] = useState<string | null>(null);
  const s = st.data;
  const exp = (kind: string, fmt: string) => downloadCsv(`/api/propfirm/export/${kind}.${fmt}`, `prop_firm_${kind}.${fmt}`)
    .catch((e: Error) => setExportError(e.message));
  return (
    <div className="stack">
      <div className="page-head"><h1>Prop Firm Center</h1>
        <Badge tone="warn">READ-ONLY · SIMULATED ACCOUNT</Badge>
        {s?.selected && <Badge tone={statusTone(s.status)} title="Compliance status of the selected profile">{s.status}</Badge>}
        <span className="sub">Rule evaluation is not a firm decision; no software guard guarantees a maximum loss.</span>
        {s?.selected && <div className="tools">
          <button className="btn" onClick={() => exp("evaluations", "csv")}>Export evaluations</button>
          <button className="btn" onClick={() => exp("rules", "json")}>Export rules</button></div>}
      </div>
      {exportError && <ErrorBox error={`Export failed: ${exportError}`} />}
      {st.error && <ErrorBox error={`Prop firm status unavailable: ${st.error}`} />}
      <Seg<View> value={view} onChange={setView} label="Prop firm view" options={[
        { value: "status", label: "Account compliance" }, { value: "rules", label: "Rules & sources" },
        { value: "library", label: "Profiles" }, { value: "compare", label: "Compare versions" }, { value: "history", label: "History" }]} />
      {view === "status" && (st.loading && !s ? <Loading what="Loading compliance" /> : s && !s.selected
        ? <Panel title="No profile selected"><Empty title="Prop firm engine off">{s.note}</Empty></Panel>
        : s && <StatusView s={s} tz={tz} />)}
      {view === "rules" && (s?.selected ? <RulesView pkey={s.summary.profile} /> : <Empty title="No profile selected" />)}
      {view === "library" && <LibraryView lib={lib} />}
      {view === "compare" && <CompareView lib={lib} />}
      {view === "history" && (s?.selected ? <HistoryView tz={tz} tick={state.activityTick} /> : <Empty title="No profile selected" />)}
    </div>
  );
}

function StatusView({ s, tz }: { s: any; tz: string }) {
  const m = s.summary, op = m.operational ?? {}, th = m.official_threshold ?? {}, e = s.entry_check;
  const blocking = (e.results ?? []).filter((r: any) => r.result !== "PASS");
  return (
    <div className="stack">
      <Panel title={`${m.firm} — ${m.product} — ${money(m.account_size)} (${m.stage})`} right={<Badge tone={op.operational ? "pos" : "warn"}>
        {op.operational ? "VERIFIED + ACTIVATED" : `NOT OPERATIONAL: ${op.status}`}</Badge>}>
        <Kv rows={[["Profile", <span className="mono">{m.profile}</span>], ["Account type", m.account_type || "—"],
          ["Rule set", <span className="mono">{String(m.ruleset_sha256).slice(0, 16)}</span>],
          ["Verification", op.status], ["Why not operational", op.not_operational_reason ?? "—"],
          ["Activation", op.activation ? `${op.activation.approved_by} at ${time(op.activation.at, true, tz)}` : "none"],
          ["Unresolved safety-critical rules", (op.safety_critical_unresolved ?? []).join(", ") || "none"]]} />
      </Panel>
      <Panel title="Drawdown protection" flush>
        <div className="stats">
          <Stat label="Balance" value={money(m.balance)} />
          <Stat label="Equity" value={money(m.equity)} sub={isNum(m.unrealized) ? `open ${money(m.unrealized, { sign: true })}` : "open P&L unknown"} />
          <Stat label="High-water" value={money(m.drawdown_model === "trailing_intraday" ? m.intraday_high_water : m.eod_high_water)} sub={label(m.drawdown_model)} />
          <Stat label="Official floor" value={money(th.floor)} sub={`${th.verification ?? "?"}${th.locked ? " · locked" : ""}`} title={`firm rule ${th.rule ?? "—"}`} />
          <Stat label="Official buffer" value={money(m.official_buffer)} tone={isNum(m.official_buffer) && m.official_buffer <= 0 ? "neg" : ""} />
          <Stat label="Uncertainty" value={money(m.uncertainty_reserve)} sub="internal reserve" />
          <Stat label="Est. buffer" value={money(m.estimated_buffer)} sub="official − uncertainty" />
          <Stat label="Safety reserve" value={money(m.internal_safety_reserve)} sub={`flatten at ${money(m.internal_flatten_reserve)}`} />
          <Stat label="Risk budget" value={money(m.available_risk_budget)} sub="est. − safety reserve" />
          <Stat label="Daily loss" value={m.daily_loss?.limit ? pct(m.daily_loss.used_pct) : "No rule"} sub={m.daily_loss?.limit ? `of ${money(m.daily_loss.limit)}` : m.daily_loss?.note} />
          <Stat label="Contracts" value={isNum(m.position_limits?.current_micro_equivalent) ? `${m.position_limits.current_micro_equivalent} / ${m.position_limits.limit_micro_equivalent ?? "—"}` : null} />
          <Stat label="Trading date" value={m.trading_date ?? null} sub={m.timezone} />
        </div>
      </Panel>
      {m.breached && <ErrorBox error={`Official loss limit BREACHED at ${m.breach?.time} (equity ${money(m.breach?.equity)}, floor ${money(m.breach?.floor)}). Entries are blocked permanently.`} />}
      {!m.recovery_ok && <ErrorBox error={`Compliance state untrusted after restart: ${(m.recovery_problems ?? []).join("; ")}. Entries blocked.`} />}
      <div className="grid g2">
        <Panel title={`Entry check now (${e.nominal})`} right={<Badge tone={e.allowed ? "pos" : "neg"}>{e.allowed ? "ALLOWED BY PROFILE" : "BLOCKED"}</Badge>}>
          {e.reasons?.length ? <ul className="notes">{e.reasons.map((r: string) => <li key={r} className="mono">{r}</li>)}</ul> : <p className="note">No blocking rule.</p>}
          <p className="note">State {String(e.account_state_sha256).slice(0, 16)} · evaluated {time(e.evaluated_at, true, tz)}</p>
        </Panel>
        <Panel title="Safety hierarchy (highest first)">
          <Table rows={s.unified.layers} rowKey={(r: any) => r.layer} columns={[
            { key: "layer", label: "Layer", render: (r: any) => label(r.layer) },
            { key: "allowed", label: "Entries", render: (r: any) => <Badge tone={r.allowed === true ? "pos" : r.allowed === false ? "neg" : "off"}>{r.allowed === true ? "allow" : r.allowed === false ? "block" : "n/a"}</Badge> },
            { key: "reason", label: "Reason", render: (r: any) => <span className="mono">{r.reason || "—"}</span> }]} />
          <p className="note">{s.unified.note}</p>
        </Panel>
      </div>
      <Panel title="Rule results (warnings and blocks first)" flush>
        <Table rows={[...blocking, ...(e.results ?? []).filter((r: any) => r.result === "PASS")]} rowKey={(r: any, i: number) => `${r.rule_id}:${i}`} columns={[
          { key: "result", label: "Result", render: (r: any) => <Badge tone={resultTone(r.result)}>{r.result}</Badge> },
          { key: "rule_id", label: "Rule", render: (r: any) => <span className="mono">{r.rule_id}</span> },
          { key: "verification", label: "Verified", render: (r: any) => <Badge tone={verifTone(r.verification)}>{verifLabel(r.verification)}</Badge> },
          { key: "reason", label: "Code", render: (r: any) => <span className="mono">{r.reason || "—"}</span> },
          { key: "explanation", label: "Explanation" }]} />
      </Panel>
      <PayoutView p={s.payout} />
    </div>
  );
}

function PayoutView({ p }: { p: any }) {
  return (
    <Panel title="Payout eligibility (read-only estimate)" right={<Badge tone={p.status === "NOT APPLICABLE" ? "off" : payoutTone(p.status)}>{p.status}</Badge>}>
      {p.requirements?.length ? <Table rows={p.requirements} rowKey={(r: any) => r.rule} columns={[
        { key: "requirement", label: "Requirement" },
        { key: "current", label: "Current", render: (r: any) => String(r.current ?? "—") },
        { key: "required", label: "Required", render: (r: any) => String(r.required ?? "—") },
        { key: "status", label: "Status", render: (r: any) => <Badge tone={payoutTone(r.status)}>{r.status}</Badge> },
        { key: "sources", label: "Source", render: (r: any) => (r.sources ?? []).map((x: any) => x.title ?? x.id).join("; ") },
        { key: "last_verification", label: "Last verified" }]} /> : <p className="note">{p.explanation}</p>}
      <p className="note">{p.disclaimer}</p>
    </Panel>
  );
}

function RulesView({ pkey }: { pkey: string }) {
  const { data, error, loading } = useApi<any>(`/api/propfirm/profile?key=${encodeURIComponent(pkey)}`, 0, 60000);
  if (error) return <ErrorBox error={`Profile unavailable: ${error}`} />;
  if (loading || !data) return <Loading what="Loading rules" />;
  const src = Object.fromEntries((data.sources ?? []).map((s: any) => [s.id, s]));
  return (
    <div className="stack">
      <Panel title="Verification"><Kv rows={[["Retrieved", data.verification?.retrieved], ["Method", data.verification?.method],
        ["Human review", data.verification?.reviewed_by ? `${data.verification.reviewed_by} on ${data.verification.reviewed_on}` : "NOT REVIEWED"],
        ["Versions", (data.versions ?? []).join(", ")], ["Profile SHA-256", <span className="mono">{data.profile_sha256}</span>]]} /></Panel>
      <Panel title="Rules" flush>
        <Table rows={data.rules} rowKey={(r: any) => r.id} columns={[
          { key: "id", label: "Rule", render: (r: any) => <span className="mono">{r.id}</span> },
          { key: "type", label: "Type", render: (r: any) => label(r.type) },
          { key: "description", label: "Description" },
          { key: "applicable", label: "Applies", render: (r: any) => (r.applicable ? "yes" : "no") },
          { key: "verification", label: "Verification", render: (r: any) => <Badge tone={verifTone(r.verification)}>{r.verification}</Badge> },
          { key: "safety_critical", label: "Safety", render: (r: any) => (r.safety_critical ? "critical" : "") },
          { key: "sources", label: "Sources", render: (r: any) => (r.sources ?? []).map((id: string) => src[id]?.title ?? id).join("; ") },
          { key: "ambiguity", label: "Unresolved", render: (r: any) => r.ambiguity || "—" }]} />
      </Panel>
      <Panel title="Internal limits (our own margins — not firm rules)"><Kv rows={Object.entries(data.internal_limits ?? {}).map(([k, v]) => [label(k), String(v)] as [ReactNode, ReactNode])} /></Panel>
      <Panel title="Official sources" flush>
        <Table rows={data.sources} rowKey={(r: any) => r.id} columns={[
          { key: "id", label: "Id" }, { key: "title", label: "Title" },
          { key: "url", label: "URL", render: (r: any) => <span className="mono">{r.url}</span> },
          { key: "retrieved", label: "Retrieved" }, { key: "effective", label: "Effective", render: (r: any) => r.effective || "not published" }]} />
      </Panel>
    </div>
  );
}

function LibraryView({ lib }: { lib: any }) {
  if (lib.error) return <ErrorBox error={`Profiles unavailable: ${lib.error}`} />;
  if (!lib.data) return <Loading what="Loading profiles" />;
  return (
    <Panel title="Profile library" flush>
      <Table rows={lib.data.profiles} rowKey={(r: any) => r.key} empty={<Empty title="No profiles" />} columns={[
        { key: "key", label: "Profile", render: (r: any) => <span className="mono">{r.key}</span> },
        { key: "firm", label: "Firm" }, { key: "product", label: "Product" }, { key: "stage", label: "Stage" },
        { key: "account_size", label: "Size", num: true, render: (r: any) => money(r.account_size) },
        { key: "status", label: "Status", render: (r: any) => <Badge tone={r.status === "VERIFIED" ? "pos" : "warn"}>{r.status}</Badge> },
        { key: "operational", label: "Operational", render: (r: any) => (r.operational ? "yes" : "no") },
        { key: "retrieved", label: "Rules retrieved" },
        { key: "reviewed_on", label: "Human review", render: (r: any) => r.reviewed_on ?? "none" },
        { key: "safety_critical_unresolved", label: "Unresolved critical", render: (r: any) => int((r.safety_critical_unresolved ?? []).length) }]} />
      {lib.data.invalid_files?.length > 0 && <p className="note">Invalid profile files (ignored): {lib.data.invalid_files.join(", ")}</p>}
    </Panel>
  );
}

function CompareView({ lib }: { lib: any }) {
  const keys: string[] = (lib.data?.profiles ?? []).map((p: any) => p.key);
  const [a, setA] = useState(""), [b, setB] = useState("");
  const path = a && b ? `/api/propfirm/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}` : null;
  const { data, error } = useApi<any>(path, 0, 60000);
  const sel = (v: string, set: (x: string) => void, l: string) => (
    <select value={v} onChange={(e) => set(e.target.value)} aria-label={l}><option value="">{l}</option>{keys.map((k) => <option key={k} value={k}>{k}</option>)}</select>);
  return (
    <div className="stack">
      <div className="row">{sel(a, setA, "Older version")}{sel(b, setB, "Newer version")}</div>
      {error && <ErrorBox error={`Comparison unavailable: ${error}`} />}
      {data && <Panel title={`${data.from} → ${data.to}`} right={<Badge tone={data.potentially_unsafe.length ? "neg" : "pos"}>{data.potentially_unsafe.length ? `${data.potentially_unsafe.length} potentially unsafe` : "no unsafe change"}</Badge>}>
        {data.potentially_unsafe.length > 0 && <ul className="notes">{data.potentially_unsafe.map((x: string) => <li key={x}>{x}</li>)}</ul>}
        <Table rows={data.changes} rowKey={(r: any, i: number) => `${r.rule}:${r.change}:${i}`} empty={<Empty title="No differences" />} columns={[
          { key: "rule", label: "Rule" }, { key: "change", label: "Change" },
          { key: "old", label: "Old", render: (r: any) => JSON.stringify(r.old ?? null) },
          { key: "new", label: "New", render: (r: any) => JSON.stringify(r.new ?? null) },
          { key: "direction", label: "Direction", render: (r: any) => r.direction ?? "" }]} />
        <p className="note">{data.note} Activation always requires a human (run_prop_firm.py activate).</p>
      </Panel>}
    </div>
  );
}

function HistoryView({ tz, tick }: { tz: string; tick: unknown }) {
  const { data, error, loading } = useApi<any>("/api/propfirm/history", tick, 10000);
  if (error) return <ErrorBox error={`History unavailable: ${error}`} />;
  if (loading || !data) return <Loading what="Loading history" />;
  return (
    <div className="stack">
      <Panel title="Counts"><Kv rows={Object.entries(data.counts).map(([k, v]) => [label(k), int(v)] as [ReactNode, ReactNode])} /></Panel>
      <div className="grid g2">
        <Panel title="Block reasons" flush><Table rows={Object.entries(data.block_reasons).map(([r, n]) => ({ r, n }))} rowKey={(x: any) => x.r}
          empty={<Empty title="No compliance blocks" />} columns={[{ key: "r", label: "Reason", render: (x: any) => <span className="mono">{x.r}</span> }, { key: "n", label: "Count", num: true }]} /></Panel>
        <Panel title="End-of-day drawdown history" flush><Table rows={[...data.drawdown_history].reverse().slice(0, 60)} rowKey={(x: any) => String(x.seq)}
          empty={<Empty title="No completed trading day yet" />} columns={[
            { key: "trading_date", label: "Trading date" }, { key: "eod_reference", label: "EOD balance", num: true, render: (x: any) => money(x.eod_reference) },
            { key: "eod_high", label: "High-water", num: true, render: (x: any) => money(x.eod_high) },
            { key: "official_floor", label: "Official floor", num: true, render: (x: any) => money(x.official_floor) }]} /></Panel>
      </div>
      <Panel title="Recent compliance evaluations" flush><Table rows={[...data.evaluations].reverse().slice(0, 100)} rowKey={(x: any) => String(x.seq)} columns={[
        { key: "time", label: "Time", render: (x: any) => time(x.time, true, tz) }, { key: "scope", label: "Scope" }, { key: "root", label: "Instr." },
        { key: "allowed", label: "Result", render: (x: any) => <Badge tone={x.allowed ? "pos" : "neg"}>{x.allowed ? "PASS" : "BLOCK"}</Badge> },
        { key: "reason", label: "Reason", render: (x: any) => <span className="mono">{x.reason || "—"}</span> },
        { key: "official_buffer", label: "Official buffer", num: true, render: (x: any) => money(x.official_buffer) },
        { key: "profile", label: "Profile", render: (x: any) => <span className="mono">{x.profile}</span> }]} /></Panel>
      <p className="note">{data.timezone}. Every row keeps its journal sequence number and the profile it was evaluated under.</p>
    </div>
  );
}
