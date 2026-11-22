import { useEffect, useRef, useState, type ReactNode } from "react";
import { getJSON } from "../api/client";
import { UNAVAILABLE, type Tone } from "../lib/format";

export function Panel({ title, right, children, flush, className }: { title?: ReactNode; right?: ReactNode; children: ReactNode;
  flush?: boolean; className?: string }) {
  return (
    <section className={`panel ${className ?? ""}`}>
      {title !== undefined && (
        <header className="panel-h"><h2>{title}</h2>{right && <div className="right">{right}</div>}</header>
      )}
      <div className={`panel-b ${flush ? "flush" : ""}`}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, tone, title }: { label: ReactNode; value: ReactNode; sub?: ReactNode; tone?: string; title?: string }) {
  const unavailable = value === UNAVAILABLE || value === null || value === undefined;
  return (
    <div className="stat" title={title}>
      <div className="l">{label}</div>
      <div className={`v ${tone ?? ""}`}>{unavailable ? <span className="unavail">Unavailable</span> : value}</div>
      {sub !== undefined && <div className="s">{sub}</div>}
    </div>
  );
}

export function Badge({ tone = "off", children, title, className }: { tone?: Tone; children: ReactNode; title?: string; className?: string }) {
  return <span className={`badge ${tone} ${className ?? ""}`} title={title}>{children}</span>;
}

export function Dot({ tone = "off", title }: { tone?: Tone; title?: string }) {
  return <span className={`dot ${tone}`} title={title} aria-label={title} />;
}

export function Kv({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="kv">
      {rows.map(([k, v], i) => (
        <div key={i} style={{ display: "contents" }}>
          <dt>{k}</dt>
          <dd>{v === UNAVAILABLE || v === null || v === undefined ? <span className="unavail">Unavailable</span> : v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><strong>{title}</strong>{children}</div>;
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <div className="loading" role="status">{what}…</div>;
}

export function ErrorBox({ error }: { error: string }) {
  return <div className="errbox" role="alert">{error}</div>;
}

export function Tip({ text, children }: { text: ReactNode; children: ReactNode }) {
  return <span className="tip" tabIndex={0}>{children}<span className="tip-body" role="tooltip">{text}</span></span>;
}

export function Seg<T extends string>({ value, options, onChange, disabled, label }: { value: T; options: { value: T; label: string; disabled?: boolean }[];
  onChange(v: T): void; disabled?: boolean; label?: string }) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} className={o.value === value ? "on" : ""} disabled={disabled || o.disabled}
          aria-pressed={o.value === value} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

export function Modal({ title, onClose, children }: { title: ReactNode; onClose(): void; children: ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <header className="panel-h"><h2>{title}</h2><div className="right"><button className="iconbtn" onClick={onClose} aria-label="Close">Close</button></div></header>
        <div className="panel-b">{children}</div>
      </div>
    </div>
  );
}

export function HBar({ value, max, tone = "info" }: { value: number | null; max: number | null; tone?: Tone }) {
  if (value === null || max === null || !(max > 0)) return <div className="hbar" />;
  const w = Math.max(0, Math.min(100, (value / max) * 100));
  return <div className="hbar" aria-label={`${w.toFixed(0)}%`}><i style={{ width: `${w}%`, background: `var(--${tone === "off" ? "off" : tone})` }} /></div>;
}

export interface Column<T> { key: string; label: ReactNode; render?(row: T): ReactNode; num?: boolean; sortable?: boolean; title?: string }

export function Table<T>({ rows, columns, rowKey, onRow, sort, onSort, empty }: { rows: T[]; columns: Column<T>[]; rowKey(r: T, i: number): string;
  onRow?(r: T): void; sort?: { key: string; order: "asc" | "desc" }; onSort?(key: string): void; empty?: ReactNode }) {
  if (!rows.length) return <>{empty ?? <Empty title="No data" />}</>;
  return (
    <div className="table-wrap">
      <table className="t">
        <thead><tr>{columns.map((c) => (
          <th key={c.key} title={c.title} className={`${c.sortable && onSort ? "sortable" : ""} ${c.num ? "num" : ""}`}
            onClick={c.sortable && onSort ? () => onSort(c.key) : undefined}
            aria-sort={sort?.key === c.key ? (sort.order === "asc" ? "ascending" : "descending") : undefined}>
            {c.label}{sort?.key === c.key ? (sort.order === "asc" ? " ▲" : " ▼") : ""}
          </th>))}</tr></thead>
        <tbody>{rows.map((r, i) => (
          <tr key={rowKey(r, i)} className={onRow ? "clickable" : ""} onClick={onRow ? () => onRow(r) : undefined}>
            {columns.map((c) => <td key={c.key} className={c.num ? "num" : ""}>{c.render ? c.render(r) : String((r as Record<string, unknown>)[c.key] ?? "")}</td>)}
          </tr>))}</tbody>
      </table>
    </div>
  );
}

/** Fetch a read-only view; re-fetch when ``refresh`` changes (driven by WebSocket status ticks), at most
 * every ``minGapMs`` per path. Responses for a path that is no longer current are ignored. */
export function useApi<T>(path: string | null, refresh: unknown = 0, minGapMs = 1500): { data: T | null; error: string | null; loading: boolean } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const current = useRef<string | null>(null);
  const last = useRef<{ path: string | null; at: number; busy: boolean }>({ path: null, at: 0, busy: false });
  useEffect(() => () => { current.current = null; }, []);
  useEffect(() => {
    current.current = path;
    if (!path) return;
    const now = Date.now();
    const samePath = last.current.path === path;
    if (samePath && (last.current.busy || now - last.current.at < minGapMs)) return;
    if (!samePath) { setLoading(true); setData(null); }
    last.current = { path, at: now, busy: true };
    getJSON<T>(path)
      .then((d) => { if (current.current === path) { setData(d); setError(null); } })
      .catch((e: Error) => { if (current.current === path) setError(e.message); })
      .finally(() => { if (last.current.path === path) last.current.busy = false; if (current.current === path) setLoading(false); });
  }, [path, refresh, minGapMs]);
  return { data, error, loading };
}
