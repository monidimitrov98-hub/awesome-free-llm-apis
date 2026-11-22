// Shapes of the backend's read-only views (src/terminal/views.py). Unknown values are null.
export type Num = number | null;

export interface Meta {
  app: string; version: string; boot_id: string; started_at: string; state: string; outcome: string | null;
  error: string | null; mode: "MOCK" | "REPLAY" | "SHADOW" | "PAPER" | string; data_label: "SYNTHETIC" | "HISTORICAL" | "LIVE" | null;
  description: string | null; run_kind: string | null; instruments: string[]; contracts: Record<string, string>;
  timeframes: string[]; read_only: boolean; order_entry: boolean;
  modes: Record<string, { available: boolean; note?: string }>;
}

export interface InstrumentMarket {
  subscription: string; health: string; entries_allowed_by_data: boolean; last_event_time: string | null;
  freshness_seconds: Num; bid: Num; ask: Num; spread: Num; last: Num; quote_time: string | null; last_time: string | null;
  contract: string | null; contract_expiry: string | null; session_date: string | null; entries_allowed: boolean;
  entry_block_reason: string;
}

export interface Market {
  time: string | null; instruments: Record<string, InstrumentMarket>; feed: Record<string, unknown>;
  latency: Record<string, Num>; validation: { rejected: Record<string, number>; events_processed: number };
  market_session: { session_date: string | null; in_session: boolean };
}

export interface Account {
  initial_balance: number; equity: Num; known_equity: Num; equity_complete: boolean; realized_gross: Num;
  commissions: Num; net_realized: Num; unrealized: Num; slippage: Num; total_costs: Num; high_water: Num;
  max_drawdown: Num; current_drawdown: Num; session_date: string | null; session_realized_net: Num; session_pnl: Num;
  positions: Record<string, number>; trades: number; wins: number; losses: number; win_rate: Num; profit_factor: Num;
  sample_warning: boolean;
}

export interface Status {
  account: Account; news: Record<string, { state: string; safe: boolean }>;
  entries: Record<string, { ok: boolean; reason: string }>;
  guard: { enabled: boolean; breached: boolean; locked_permanently: boolean; remaining_buffer: Num; block: string | null };
  session_date: string | null; daily_loss_used_pct: Num; accepting_entries: boolean; mode_authorizes_entries: boolean;
  runtime: { state: string; outcome: string | null; events: number; stopped_reason: string | null };
}

export interface Activity { seq: number; kind: string; time: string; summary: string; root: string | null; payload: Record<string, unknown> }
export interface Notification { id: number; time: string; severity: "info" | "warning" | "critical" | "success"; category: string;
  title: string; description: string; root: string | null }

export interface Snapshot { meta: Meta; seq: number; market: Market | null; status: Status | null; activity: Activity[]; notifications: Notification[] }
